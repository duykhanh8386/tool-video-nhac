from __future__ import annotations


def build_mix_filter(volumes: list[float], normalize: bool = False) -> tuple[str, str]:
    if not volumes:
        raise ValueError("Không có track audio để mix.")
    chains = [f"[{index}:a]volume={max(0, volume):.4f}[a{index}]" for index, volume in enumerate(volumes)]
    labels = "".join(f"[a{index}]" for index in range(len(volumes)))
    tail = f"{labels}amix=inputs={len(volumes)}:duration=first:dropout_transition=2"
    if normalize:
        tail += ",loudnorm=I=-14:LRA=11:TP=-1.5"
    tail += "[aout]"
    return ";".join(chains + [tail]), "[aout]"
