from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "visual_loop_studio" / "version.py"
RESOURCE_FILE = ROOT / "build" / "version_info.txt"


def numeric_version(value: str) -> tuple[int, int, int, int]:
    numbers = [int(item) for item in re.findall(r"\d+", value)[:4]]
    return tuple((numbers + [0, 0, 0, 0])[:4])  # type: ignore[return-value]


def stamp(version: str, commit: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version):
        raise ValueError("Version must use numeric form such as 1.0.42 or 1.0.42.1")
    VERSION_FILE.write_text(
        '"""Build metadata. GitHub Actions stamps this file before packaging."""\n\n'
        f'__version__ = "{version}"\n'
        f'__build_commit__ = "{commit}"\n'
        '__repository__ = "duykhanh8386/tool-video-nhac"\n',
        encoding="utf-8",
    )
    major, minor, patch, build = numeric_version(version)
    RESOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESOURCE_FILE.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({major}, {minor}, {patch}, {build}), prodvers=({major}, {minor}, {patch}, {build}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Visual Loop Studio'),
    StringStruct('FileDescription', 'Visual Loop Studio'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'VisualLoopStudio'),
    StringStruct('OriginalFilename', 'VisualLoopStudio-Windows-x64.exe'),
    StringStruct('ProductName', 'Visual Loop Studio'),
    StringStruct('ProductVersion', '{version}')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)\n""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    stamp(args.version, args.commit)
    print(f"Stamped Visual Loop Studio {args.version} ({args.commit[:10]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
