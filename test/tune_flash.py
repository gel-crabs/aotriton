#!/usr/bin/env python
# Copyright © 2023-2024 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

import pytest
import torch
import json
import sys
import subprocess
import argparse
import itertools
import os
import time

from rocm_arch import rocm_get_gpuarch
from attn_torch_function import (
    DEFAULT_PHILOX_SEED,
    DEFAULT_PHILOX_OFFSET,
    attention,
    debug_fill_dropout_rng,
    AttentionExtraArgs
)
from _common_test import SdpaContext, SdpaParams

_DEBUG_SKIP_TUNE_BACKWARD = True

class Tuner(object):
    KERNEL_FAMILY = 'FLASH'

    def __init__(self, args):
        self._args = args
        self._arch = rocm_get_gpuarch()
        dbargs = ['python3', '-m', 'v2python.table_tool', '-v', '-f', self._args.db_file, '-k', self.KERNEL_FAMILY]
        if args.create_table_only:
            dbargs += ['--action', 'createtableonly']
        self._dbp = subprocess.Popen(dbargs,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
        os.set_blocking(self._dbp.stdout.fileno(), False)
        os.set_blocking(self._dbp.stderr.fileno(), False)

    @property
    def verbose(self):
        return self._args.verbose

    def gen(self):
        a = self._args
        yield from itertools.product(a.batch, a.n_heads, a.d_head, a.seqlen_q, a.seqlen_k, a.causal, a.sm_scale, a.dropout_p, a.return_encoded_softmax, a.dtype, a.bias_type)

    def profile_all(self):
        a = self._args
        for i, tup in enumerate(self.gen()):
            print(f"[{i:06d}] Handling {tup}")
            if a.continue_from is not None and i < a.continue_from:
                continue
            if a.stop_at is not None and i > a.stop_at:
                break
            if a.dry_run:
                continue
            self.profile(*tup)

    def profile(self, BATCH, N_HEADS, D_HEAD, seqlen_q, seqlen_k, causal, sm_scale, dropout_p, return_encoded_softmax, dtype, bias_type):
        a = self._args
        '''
        Create SdpaContext for testing
        '''
        ctx = SdpaContext(BATCH, N_HEADS, D_HEAD, seqlen_q, seqlen_k, dtype,
                          bias_type=bias_type, storage_flip=None, device='cuda')
        ctx.create_ref_inputs()
        ctx.set_require_grads(skip_db=True)
        q, k, v, b = ctx.dev_tensors
        '''
        Create reference dropout_mask
        '''
        if dropout_p > 0.0:
            rdims = (BATCH, N_HEADS, seqlen_q, seqlen_k)
            r = torch.empty(rdims, device=q.device, dtype=torch.float32)
            philox_seed = DEFAULT_PHILOX_SEED
            philox_offset = DEFAULT_PHILOX_OFFSET
            debug_fill_dropout_rng(r, philox_seed, philox_offset)
        else:
            r = None
        sdpa_params = SdpaParams(causal=causal, sm_scale=sm_scale, dropout_p=dropout_p, dropout_mask=r)
        ref_out, _ = ctx.compute_ref_forward(sdpa_params)

        '''
        Now, enable autotune (C++ form), enable output validation
        '''
        def fwd_validator(tri_out):
            is_allclose, adiff, _, _ = ctx.validate_with_reference(tri_out, None, no_backward=True)
            return is_allclose

        ext = AttentionExtraArgs(return_encoded_softmax=return_encoded_softmax,
                autotune=True,
                return_autotune=True,
                autotune_validator=fwd_validator)
        tri_out, encoded_softmax, best_configs = attention(q, k, v, b, causal, sm_scale, dropout_p, ext)
        if self.verbose:
            print('Returned best configs')
            for kernel_name, best in best_configs:
                # print(f'{kernel_name=} {best.kwargs=} {best.num_warps=} {best.num_stages=}')
                print(f'{kernel_name=}')
        if not _DEBUG_SKIP_TUNE_BACKWARD:
            dout = torch.randn_like(q)
            tri_out.backward(dout)
            if self.verbose:
                print('Returned best configs after backward')
                for kernel_name, best in best_configs:
                    print(f'{kernel_name=}')
        head_dim_rounded = 2 ** (D_HEAD - 1).bit_length()
        head_dim_rounded = max(16, head_dim_rounded)
        inputs = {
            'Q_dtype': str(dtype),
            'N_HEADS': N_HEADS,
            'D_HEAD': D_HEAD,
            'max_seqlen_q': seqlen_q,
            'max_seqlen_k': seqlen_k,
            'CAUSAL': causal,
            'RETURN_ENCODED_SOFTMAX': return_encoded_softmax,
            'BLOCK_DMODEL': head_dim_rounded,
            'ENABLE_DROPOUT' : dropout_p > 0.0,
            'PADDED_HEAD' : head_dim_rounded != D_HEAD,
            'BIAS_TYPE' : bias_type,
        }
        self.pipe_configs(inputs, best_configs)

    def pipe_configs(self, inputs, best_configs):
        for kernel_name, best in best_configs:
            j = self.translate_config(inputs, kernel_name, best)
            js = json.dumps(j, separators=(',', ':'))
            print(f'Piping to db process {js}')
            print(js, file=self._dbp.stdin, flush=True)
            self.splice_pipes()

    def splice_pipes(self):
        for i in range(10):
            while True:
                line = self._dbp.stdout.readline()
                if line:
                    print(line, end='')
                else:
                    time.sleep(0.1)
                    break

        for i in range(10):
            while True:
                line = self._dbp.stderr.readline()
                if line:
                    print(line, end='', file=sys.stderr)
                else:
                    time.sleep(0.1)
                    break
        sys.stdout.flush()
        sys.stderr.flush()

    def translate_config(self, inputs, kernel_name, best):
        tuning_result = {
            'arch' : self._arch,
            'kernel_name' : kernel_name,
            'inputs' : inputs,
            'tuned_kernel' : best.psels,
            'compiler_options' : best.copts,
        }
        return tuning_result

    def stop(self):
        self._dbp.stdin.close()
        print("Waiting for database process to terminate")
        self._dbp.wait()
        self.splice_pipes()

def parse():
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--batch', type=int, nargs=1, default=[1], help='(Not a functional) Batch size.')
    p.add_argument('--n_heads', type=int, nargs=1, default=[48], help='(Not a functional) Number of heads')
    p.add_argument('--sm_scale', type=float, nargs=1, default=[1.2], help='(Not a functional) Softmax Scale')
    p.add_argument('--return_encoded_softmax', type=bool, default=[False],
                   help="(A functional for debugging) kernel that returns softmax(dropout(QK')) to validate the correctness of dropout")
    p.add_argument('--d_head', type=int, nargs='+', default=[16,32,64,128,256], help='Head dimensions.')
    p.add_argument('--seqlen_q', type=int, nargs='+', default=[256,1024,2048,4096,8192,16384,32768], help='Sequence length of Q.')
    p.add_argument('--seqlen_k', type=int, nargs='+', default=[256,1024,2048,4096,8192,16384,32768], help='Sequence length of K/V.')
    p.add_argument('--causal', type=int, nargs='+', default=[True,False], choices=[0, 1], help='Causal mask. (Use 0/1 for False/True')
    p.add_argument('--dropout_p', type=float, nargs='+', default=[0.5, 0.0], help='Probablity to dropout (0 to disable).')
    p.add_argument('--dtype', type=str, nargs='+',
                   default=['float16', 'bfloat16', 'float32'],
                   choices=['float16', 'bfloat16', 'float32'],
                   help='Datatype to profile.')
    p.add_argument('--bias_type', type=int, nargs='+', default=[0, 1], choices=[0, 1], help='Bias types to profile, 0: None, 1: Matrix.')
    p.add_argument('--verbose', action='store_true', help='Verbose')
    p.add_argument('--validate',
                   action='store_true', help='Validate the correctness of the output to avoid faulty autotune configs')
    p.add_argument('--dry_run', action='store_true', help="Print parameter combinations without running tests")
    p.add_argument('--continue_from', type=int, default=None, help="Continue from n-th functional set")
    p.add_argument('--stop_at', type=int, default=None, help="Stop at n-th functional set")
    p.add_argument('--db_file', type=str, required=True, help="Sqlite Database file")
    p.add_argument('--create_table_only', action='store_true', help="Do not insert data, only create tables. Used for schema updates.")
    args = p.parse_args()
    args.dtype = [ getattr(torch, t) for t in args.dtype ]
    args.causal = [ bool(c) for c in args.causal ]
    # assert args.causal == [False], f'{args.causal=} {args.return_encoded_softmax=}'
    return args

def main():
    args = parse()
    tuner = Tuner(args)
    tuner.profile_all()
    tuner.stop()

if __name__ == '__main__':
    main()