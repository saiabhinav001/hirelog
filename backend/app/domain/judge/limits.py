"""Server-owned execution limits for every judge submission (ADR 0024).

A student chooses a language key and source text; nothing here, nor any field of `JudgeSubmitRequest`, can be influenced by
them. The limits are sent to the provider with each test, and `NETWORK_ENABLED` is a constant, not a setting, so no
configuration or request can turn the sandbox's network on. Enforcement itself (cgroups, seccomp, egress and metadata
blocking, read-only mounts) belongs to the isolated judge and is NOT proven by anything in this repository.
"""

from __future__ import annotations

from dataclasses import dataclass

NETWORK_ENABLED = False

# Per test, as sent to the provider.
CPU_TIME_LIMIT_SECONDS = 5.0
WALL_TIME_LIMIT_SECONDS = 10.0
MEMORY_LIMIT_KB = 262_144  # 256 MB
STACK_LIMIT_KB = 65_536  # 64 MB
MAX_PROCESSES_AND_THREADS = 32
MAX_FILE_SIZE_KB = 1_024  # bounds what a program may write, including stdout redirected to a file

# Per test, as stored here: a flooding program must not be able to bloat or break the grading record.
MAX_STORED_STREAM_CHARS = 16_384
TRUNCATION_MARK = "\n[output truncated]"


@dataclass(frozen=True)
class JudgeLimits:
    cpu_time_seconds: float = CPU_TIME_LIMIT_SECONDS
    wall_time_seconds: float = WALL_TIME_LIMIT_SECONDS
    memory_kb: int = MEMORY_LIMIT_KB
    stack_kb: int = STACK_LIMIT_KB
    max_processes: int = MAX_PROCESSES_AND_THREADS
    max_file_kb: int = MAX_FILE_SIZE_KB

    def judge0_fields(self) -> dict:
        """The Judge0 submission attributes for these limits. `enable_network` is always False and there is no
        callback or image field: results are polled, and the runtime comes from the server-side registry."""
        return {
            "cpu_time_limit": self.cpu_time_seconds,
            "wall_time_limit": self.wall_time_seconds,
            "memory_limit": self.memory_kb,
            "stack_limit": self.stack_kb,
            "max_processes_and_or_threads": self.max_processes,
            "max_file_size": self.max_file_kb,
            "enable_per_process_and_thread_time_limit": True,
            "enable_per_process_and_thread_memory_limit": True,
            "number_of_runs": 1,
            "enable_network": NETWORK_ENABLED,
        }


def truncate_stream(text: str) -> str:
    if len(text) <= MAX_STORED_STREAM_CHARS:
        return text
    return text[:MAX_STORED_STREAM_CHARS] + TRUNCATION_MARK
