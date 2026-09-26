from __future__ import annotations

import math
import random


def particle_positions(count: int, width: int, height: int, phase: float, seed: int = 704) -> list[tuple[float, float, float]]:
    rng = random.Random(seed)
    result = []
    for _ in range(count):
        x = rng.random() * width
        y0 = rng.random() * height
        speed = 0.15 + rng.random() * 0.5
        y = (y0 + height * speed * phase) % height
        size = 1.0 + rng.random() * 3.0
        result.append((x + math.sin(phase * math.tau + y0) * 4, y, size))
    return result
