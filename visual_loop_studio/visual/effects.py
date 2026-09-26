from __future__ import annotations

from models.visual_project import EffectItem


EFFECT_NAMES = [
    "SNOW", "RAIN", "DUST", "SPARKLES", "FLOATING_LIGHTS", "FILM_GRAIN",
    "TV_STATIC", "VHS_NOISE", "SCANLINES", "RGB_GLITCH", "CHROMATIC_ABERRATION",
    "LIGHT_LEAK", "BOKEH", "FOG", "OLD_FILM", "FILM_SCRATCHES", "STAR_FIELD",
    "VIGNETTE", "BLOOM", "SOFT_GLOW", "PIXEL_NOISE",
]


def ffmpeg_effect_filters(items: list[EffectItem]) -> list[str]:
    result: list[str] = []
    for item in items:
        if not item.enabled:
            continue
        amount = max(0.0, min(1.0, item.intensity))
        name = item.name.upper()
        if name in {"FILM_GRAIN", "PIXEL_NOISE", "DUST", "TV_STATIC", "VHS_NOISE", "FILM_SCRATCHES"}:
            strength = max(1, round(2 + amount * 18))
            result.append(f"noise=alls={strength}:allf=t+u")
        elif name == "SCANLINES":
            alpha = 0.03 + amount * 0.17
            result.append(f"drawgrid=w=iw:h=4:t=1:c=black@{alpha:.3f}")
        elif name in {"RGB_GLITCH", "CHROMATIC_ABERRATION"}:
            shift = max(1, round(amount * 8))
            result.append(f"rgbashift=rh={shift}:bh=-{shift}")
        elif name == "VIGNETTE":
            result.append(f"vignette=angle={0.15 + amount * 0.55:.3f}")
        elif name in {"BLOOM", "SOFT_GLOW", "FOG", "LIGHT_LEAK", "BOKEH", "FLOATING_LIGHTS"}:
            result.append(f"gblur=sigma={0.2 + amount * 1.8:.2f}")
        elif name == "OLD_FILM":
            result.extend(["curves=preset=vintage", f"noise=alls={round(2 + amount * 10)}:allf=t+u"])
        elif name in {"SNOW", "RAIN", "SPARKLES", "STAR_FIELD"}:
            # Lightweight procedural approximation that remains streamable and loop-safe.
            strength = max(1, round(1 + amount * 7))
            result.append(f"noise=alls={strength}:allf=t")
    return result
