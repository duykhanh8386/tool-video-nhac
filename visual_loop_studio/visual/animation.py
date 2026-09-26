from __future__ import annotations


def background_filter(preset: str, width: int, height: int, fps: int, still: bool) -> str:
    base = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}"
    name = preset.upper()
    if still and name not in {"NONE", "STATIC"}:
        # A 10-second sinusoidal zoom repeats exactly six times in a 60-second visual.
        zoom = "1+0.018*(1-cos(2*PI*on/(10*{fps})))".format(fps=fps)
        x = "iw/2-(iw/zoom/2)+5*sin(2*PI*on/(10*{fps}))".format(fps=fps)
        y = "ih/2-(ih/zoom/2)+3*sin(2*PI*on/(10*{fps}))".format(fps=fps)
        return f"{base},zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={fps}"
    return base


def overlay_position(motion: str, x: str, y: str) -> tuple[str, str]:
    name = motion.upper()
    if name in {"FLOAT", "BREATH", "BEAT_PULSE"}:
        return x, f"{y}+8*sin(2*PI*t/5)"
    return x, y
