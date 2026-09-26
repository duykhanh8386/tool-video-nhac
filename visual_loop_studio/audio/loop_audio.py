from __future__ import annotations


def loop_input_args(path: str, enabled: bool = True) -> list[str]:
    return (["-stream_loop", "-1"] if enabled else []) + ["-i", path]
