from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


@lru_cache(maxsize=256)
def windows_font_file(family: str, bold: bool = False, italic: bool = False) -> str:
    """Return the best Windows font file for a family selected by QFontComboBox."""
    family_key = _normalized(family)
    if not family_key:
        return ""
    fonts_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates: list[tuple[int, Path]] = []

    try:
        import winreg

        locations = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        )
        for hive, key_name in locations:
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    index = 0
                    while True:
                        try:
                            display_name, filename, _kind = winreg.EnumValue(key, index)
                            index += 1
                        except OSError:
                            break
                        path = Path(str(filename))
                        if not path.is_absolute():
                            path = fonts_dir / path
                        score = _font_score(family_key, display_name, path, bold, italic)
                        if score >= 0 and path.is_file():
                            candidates.append((score, path))
            except OSError:
                continue
    except ImportError:
        pass

    if not candidates and fonts_dir.is_dir():
        for path in fonts_dir.iterdir():
            if path.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                continue
            score = _font_score(family_key, path.stem, path, bold, italic)
            if score >= 0:
                candidates.append((score, path))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], len(item[1].name), item[1].name.lower()))
    return str(candidates[0][1])


def _font_score(family_key: str, display_name: str, path: Path, bold: bool, italic: bool) -> int:
    searchable = _normalized(f"{display_name} {path.stem}")
    if family_key not in searchable:
        return -1
    score = 1000 - max(0, len(searchable) - len(family_key))
    has_bold = "bold" in searchable or "demi" in searchable or "semibold" in searchable
    has_italic = "italic" in searchable or "oblique" in searchable
    score += 120 if has_bold == bold else -80
    score += 120 if has_italic == italic else -80
    if searchable.startswith(family_key):
        score += 60
    return score
