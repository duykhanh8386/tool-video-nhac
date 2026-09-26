from __future__ import annotations


WAVEFORM_MODES = [
    "NONE", "Sine wave", "Neon waveform", "Spectrum bars", "Mirror waveform",
    "Radial spectrum", "Circular spectrum", "Equalizer bars", "Smooth line waveform",
    "Energy wave", "Particle wave",
]


def waveform_filter(mode: str, width: int, height: int) -> str:
    wave_width = max(64, int(width))
    wave_height = max(32, int(height))
    lowered = mode.lower()
    if "spectrum" in lowered or "equalizer" in lowered:
        return f"showspectrum=s={wave_width}x{wave_height}:mode=combined:color=intensity:scale=sqrt:slide=scroll:fps=30,format=rgba,colorchannelmixer=aa=.78"
    mode_name = "cline" if "smooth" in lowered or "neon" in lowered else "line"
    return f"showwaves=s={wave_width}x{wave_height}:mode={mode_name}:colors=0x63E6FF@0.92:scale=sqrt:rate=30,format=rgba"
