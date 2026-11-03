"""Prepare DLL search paths before importing binary Python packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path


if os.name == "nt" and hasattr(os, "add_dll_directory"):
    bundle_root = Path(getattr(sys, "_MEIPASS", ""))
    handles = getattr(sys, "_visual_loop_dll_directory_handles", [])
    for relative_path in ("numpy.libs",):
        dll_directory = bundle_root / relative_path
        if dll_directory.is_dir():
            handles.append(os.add_dll_directory(str(dll_directory)))
    # Keep the handles alive for the lifetime of the frozen application.
    sys._visual_loop_dll_directory_handles = handles

# Configure SSL certificate paths in the bundled environment
try:
    import certifi
    ca_path = certifi.where()
    if ca_path and os.path.isfile(ca_path):
        os.environ.setdefault("SSL_CERT_FILE", ca_path)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_path)
except Exception:
    pass
