# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import os
from dataclasses import dataclass
from datetime import timedelta
import itertools

import torch

from distributed.logging_utils import SingletonLogger
import time
from contextlib import contextmanager
from typing import Optional, Union, Generator

logger = SingletonLogger.get_logger()

def _warn_overwrite_env(env, val):
    if env in os.environ:
        logger.warning(
            f"ENV[{env}] = {os.environ[env]} will be overridden to {val} based on job config"
        )
    os.environ[env] = val

TRACE_BUFFER_SIZE = "TORCH_NCCL_TRACE_BUFFER_SIZE"
TRACE_FILE = "TORCH_NCCL_DEBUG_INFO_TEMP_FILE"
DUMP_ON_TIMEOUT = "TORCH_NCCL_DUMP_ON_TIMEOUT"
ASYNC_ERROR_HANDLING = "TORCH_NCCL_ASYNC_ERROR_HANDLING"
SKIP_CLEANUP = "3"


def init_distributed(init_timeout_seconds: int = 120):
    # FlightRecorder is incompatible with =1 mode where watchdog aborts work, must use =3 (skipcleanup)
    # to get flight recorder dumps. See https://github.com/pytorch/pytorch/issues/121055
    # This could be done only when flight recorder is enabled, but its nice to be consistent to avoid subtle
    # behavior differences
    _warn_overwrite_env(ASYNC_ERROR_HANDLING, SKIP_CLEANUP)

    torch.distributed.init_process_group(
        "nccl", timeout=timedelta(seconds=init_timeout_seconds)
    )

    # to mitigate the memory issue that collectives using
    # async_op=True hold memory longer than they should
    # such as those in tensor parallelism
    os.environ["TORCH_NCCL_AVOID_RECORD_STREAMS"] = "1"


def get_num_params(model: torch.nn.Module, exclude_embedding: bool = False) -> int:
    num_params = sum(p.numel() for p in model.parameters())
    if exclude_embedding:
        num_params -= model.tok_embeddings.weight.numel()
    readable_num_params = format_model_params(num_params)
    return readable_num_params

def get_stage_size(stage):
    model_size = sum(
            [
                p.numel() * p.dtype.itemsize
                for p in itertools.chain(stage.parameters(), stage.buffers())
            ]
        )
    readable_model_size = bytes_to_readable(model_size)
    return model_size, readable_model_size

@dataclass(frozen=True)
class Color:
    black = "\033[30m"
    red = "\033[31m"
    green = "\033[32m"
    yellow = "\033[33m"
    blue = "\033[34m"
    magenta = "\033[35m"
    cyan = "\033[36m"
    white = "\033[37m"
    reset = "\033[39m"


@dataclass(frozen=True)
class NoColor:
    black = ""
    red = ""
    green = ""
    yellow = ""
    blue = ""
    magenta = ""
    cyan = ""
    white = ""
    reset = ""


class GPUMemoryMonitor:
    def __init__(self, device: str = "cuda:0"):
        self.device = torch.device(device)  # device object
        self.device_name = torch.cuda.get_device_name(self.device)
        self.device_index = torch.cuda.current_device()
        self.device_capacity = torch.cuda.get_device_properties(
            self.device
        ).total_memory
        self.device_capacity_gib = self._to_gib(self.device_capacity)

        torch.cuda.reset_peak_memory_stats()
        torch.cuda.empty_cache()

    def _to_gib(self, memory_in_bytes):
        # NOTE: GiB (gibibyte) is 1024, vs GB is 1000
        _gib_in_bytes = 1024 * 1024 * 1024
        memory_in_gib = memory_in_bytes / _gib_in_bytes
        return memory_in_gib

    def _to_pct(self, memory):
        return 100 * memory / self.device_capacity

    def get_peak_stats(self):
        cuda_info = torch.cuda.memory_stats(self.device)

        # max_active = cuda_info["active_bytes.all.peak"]
        # max_active_gib = self._to_gib(max_active)
        # max_active_pct = self._to_pct(max_active)

        max_reserved = cuda_info["reserved_bytes.all.peak"]
        max_reserved_gib = self._to_gib(max_reserved)
        max_reserved_pct = self._to_pct(max_reserved)

        return max_reserved_gib, max_reserved_pct 
        

    def reset_peak_stats(self):
        torch.cuda.reset_peak_memory_stats()


def build_gpu_memory_monitor():
    gpu_memory_monitor = GPUMemoryMonitor("cuda")
    device_info = (
        f"GPU capacity: {gpu_memory_monitor.device_name} ({gpu_memory_monitor.device_index}) "
        f"with {gpu_memory_monitor.device_capacity_gib:.2f}GiB memory"
    )

    return gpu_memory_monitor, device_info

def format_model_params(params):
    """ turn the num_params into a readable formatted number """
    if params >= 1_000_000_000:
        return f"{params / 1_000_000_000:.2f}B"
    elif params >= 1_000_000:
        return f"{params / 1_000_000:.2f}M"
    else:
        return f"{params:,}"

import time
from typing import Optional

class TrackTime:
    def __init__(self, use_ms: bool = False, round_to: Optional[int] = 4):
        self.use_ms = use_ms
        self.round_to = round_to
        self.start_time = 0.0
        self.elapsed_time = 0.0
        self.unit="seconds" if not use_ms else "milliseconds"

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        end_time = time.perf_counter()
        self.elapsed_time = end_time - self.start_time
        
        if self.use_ms:
            self.elapsed_time *= 1000  # Convert to milliseconds
        
        if self.round_to is not None:
            self.elapsed_time = round(self.elapsed_time, self.round_to)

    def get_time(self) -> float:
        return self.elapsed_time


def bytes_to_readable(bytes_value: int) -> str:
    """ formatting function to make reading model (stage) sizes easy """
    GiB = 1024 ** 3  # 1 GiB in bytes
    MiB = 1024 ** 2  # 1 MiB in bytes
    
    if bytes_value >= GiB:
        value = bytes_value / GiB
        unit = "GiB"
    else:
        value = bytes_value / MiB
        unit = "MiB"
    
    # Round to 2 decimal places
    rounded_value = round(value, 2)
    
    return f"{rounded_value} {unit}"
