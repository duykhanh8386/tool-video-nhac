"""Entry point for the standalone YouTube MP3 Downloader executable."""

from __future__ import annotations

import sys
from downloader_app.downloader import standalone_smoke_test


def main() -> int:
    if "--smoke-test" in sys.argv:
        problems = standalone_smoke_test()
        if problems:
            return 1
        from downloader_app.app import native_ui_smoke_test
        native_ui_smoke_test()
        return 0
    from downloader_app.app import run_app
    run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

