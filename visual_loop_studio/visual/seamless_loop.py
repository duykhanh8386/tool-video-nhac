from __future__ import annotations

import math


def loop_phase(seconds: float, period: float = 10.0) -> float:
    return (seconds % period) / period


def sinusoidal01(seconds: float, period: float = 10.0) -> float:
    return 0.5 - 0.5 * math.cos(math.tau * loop_phase(seconds, period))
