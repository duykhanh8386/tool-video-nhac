from __future__ import annotations


COLOR_FILTERS = [
    "NONE", "WARM", "COOL", "CINEMATIC", "VINTAGE", "DREAMY", "PURPLE", "BLUE",
    "HEALING", "CHRISTMAS", "NEON", "RETRO", "LOFI", "BLACK_AND_WHITE",
    "SOFT_PASTEL", "HIGH_CONTRAST",
]


def ffmpeg_color_filters(name: str) -> list[str]:
    return {
        "NONE": [],
        "WARM": ["colorbalance=rs=.08:gs=.025:bs=-.06"],
        "COOL": ["colorbalance=rs=-.05:bs=.09"],
        "CINEMATIC": ["eq=contrast=1.12:saturation=.88", "colorbalance=bs=.04:rh=.04"],
        "VINTAGE": ["curves=preset=vintage", "eq=saturation=.82"],
        "DREAMY": ["eq=brightness=.035:contrast=.94:saturation=.9", "gblur=sigma=.35"],
        "PURPLE": ["colorbalance=rs=.06:bs=.1"],
        "BLUE": ["colorbalance=bs=.13:rs=-.04"],
        "HEALING": ["eq=brightness=.025:saturation=.88", "colorbalance=gs=.035:bs=.025"],
        "CHRISTMAS": ["eq=saturation=1.14:contrast=1.05", "colorbalance=rs=.04:gs=.025"],
        "NEON": ["eq=contrast=1.16:saturation=1.32"],
        "RETRO": ["curves=preset=vintage", "eq=contrast=1.04:saturation=.92"],
        "LOFI": ["eq=contrast=.96:saturation=.78:gamma=.96"],
        "BLACK_AND_WHITE": ["hue=s=0"],
        "SOFT_PASTEL": ["eq=brightness=.04:contrast=.88:saturation=.78"],
        "HIGH_CONTRAST": ["eq=contrast=1.3:saturation=1.06"],
    }.get(name.upper(), [])


def ffmpeg_manual_filters(values: dict[str, float] | None) -> list[str]:
    values = values or {}
    brightness = max(-100, min(100, float(values.get("brightness", 0)))) / 200
    contrast = max(0, min(300, float(values.get("contrast", 100)))) / 100
    saturation = max(0, min(300, float(values.get("saturation", 100)))) / 100
    gamma = max(10, min(300, float(values.get("gamma", 100)))) / 100
    temperature = max(-100, min(100, float(values.get("temperature", 0)))) / 1000
    tint = max(-100, min(100, float(values.get("tint", 0)))) / 1200
    shadows = max(-100, min(100, float(values.get("shadows", 0)))) / 1000
    highlights = max(-100, min(100, float(values.get("highlights", 0)))) / 1000
    result: list[str] = []
    if any(abs(value) > .0001 for value in (brightness, contrast - 1, saturation - 1, gamma - 1)):
        result.append(f"eq=brightness={brightness:.4f}:contrast={contrast:.4f}:saturation={saturation:.4f}:gamma={gamma:.4f}")
    if any(abs(value) > .0001 for value in (temperature, tint, shadows, highlights)):
        result.append(
            "colorbalance=rs={:.4f}:gs={:.4f}:bs={:.4f}:rm={:.4f}:gm={:.4f}:bm={:.4f}:rh={:.4f}:gh={:.4f}:bh={:.4f}".format(
                temperature + shadows, tint + shadows, -temperature + shadows,
                temperature / 2, tint, -temperature / 2,
                temperature + highlights, tint + highlights, -temperature + highlights,
            )
        )
    sharpness = max(0, min(100, float(values.get("sharpness", 0)))) / 50
    if sharpness:
        result.append(f"unsharp=5:5:{sharpness:.3f}:5:5:0")
    vignette = max(0, min(100, float(values.get("vignette", 0)))) / 100
    if vignette:
        result.append(f"vignette=angle={0.15 + vignette * .65:.3f}")
    bloom = max(0, min(100, float(values.get("bloom", 0)))) / 100
    if bloom:
        result.append(f"gblur=sigma={bloom * .9:.3f}")
    return result
