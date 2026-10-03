from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes


APP_SECRET_PREFIX = "VisualLoopStudio"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _target(name: str) -> str:
    normalized = "".join(character for character in str(name or "") if character.isalnum() or character in "-_.")
    if not normalized:
        raise ValueError("Tên secret không hợp lệ.")
    return f"{APP_SECRET_PREFIX}/{normalized}"


def _advapi32():
    if sys.platform != "win32":
        return None
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
    library.CredReadW.restype = wintypes.BOOL
    library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    library.CredDeleteW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def set_secret(name: str, value: str) -> bool:
    library = _advapi32()
    if library is None:
        return False
    text = str(value or "")
    if not text:
        return delete_secret(name)
    encoded = text.encode("utf-8")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = _Credential()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = _target(name)
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = os.environ.get("USERNAME", "VisualLoopStudio")
    if not library.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return True


def get_secret(name: str) -> str:
    library = _advapi32()
    if library is None:
        return ""
    pointer = ctypes.POINTER(_Credential)()
    if not library.CredReadW(_target(name), CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return ""
        raise ctypes.WinError(error)
    try:
        size = int(pointer.contents.CredentialBlobSize)
        if not size or not pointer.contents.CredentialBlob:
            return ""
        raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
        return raw.decode("utf-8")
    finally:
        library.CredFree(pointer)


def delete_secret(name: str) -> bool:
    library = _advapi32()
    if library is None:
        return False
    if library.CredDeleteW(_target(name), CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return True
    raise ctypes.WinError(error)


def resolve_secret(name: str, environment_variable: str = "", legacy_value: str = "") -> str:
    if environment_variable:
        environment = os.environ.get(environment_variable, "").strip()
        if environment:
            return environment
    try:
        stored = get_secret(name).strip()
    except OSError:
        stored = ""
    return stored or str(legacy_value or "").strip()
