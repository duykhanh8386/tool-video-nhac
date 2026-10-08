from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.paths import DATA_DIR, ensure_app_dirs

LICENSE_FILE = DATA_DIR / "license.json"
KEYGEN_CONFIG_FILE = DATA_DIR / "license_config.json"
KEYGEN_API_URL = "https://api.keygen.sh/v1"

# Người dùng có thể điền sẵn Account ID vào đây hoặc qua UI
DEFAULT_ACCOUNT_ID = ""


def get_hardware_fingerprint() -> str:
    """
    Sinh mã phần cứng duy nhất (Machine Fingerprint / HWID) cho máy tính Windows/Mac/Linux.
    Sử dụng Windows MachineGUID (từ Registry), MAC address, và Hostname.
    """
    machine_guid = ""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        except Exception:
            machine_guid = ""

    hostname = socket.gethostname()
    try:
        import uuid
        mac = str(uuid.getnode())
    except Exception:
        mac = "0"

    raw = f"hwid:{machine_guid}:{hostname}:{mac}:{platform.machine()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    return f"HWID-{digest[:4]}-{digest[4:8]}-{digest[8:12]}-{digest[12:16]}"


class LicenseManager:
    def __init__(self):
        ensure_app_dirs()
        self.fingerprint = get_hardware_fingerprint()
        self._cached_data: dict[str, Any] = {}
        self._load_cache()

    def get_account_id(self) -> str:
        """Lấy Account ID đã lưu từ file cấu hình hoặc giá trị mặc định."""
        if KEYGEN_CONFIG_FILE.exists():
            try:
                cfg = json.loads(KEYGEN_CONFIG_FILE.read_text(encoding="utf-8"))
                val = cfg.get("account_id", "").strip()
                if val:
                    return val
            except Exception:
                pass
        return DEFAULT_ACCOUNT_ID.strip()

    def set_account_id(self, account_id: str) -> None:
        """Lưu Account ID cấu hình."""
        data = {"account_id": account_id.strip()}
        KEYGEN_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_cache(self) -> None:
        if LICENSE_FILE.exists():
            try:
                self._cached_data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._cached_data = {}
        else:
            self._cached_data = {}

    def get_license_info(self) -> dict[str, Any]:
        return dict(self._cached_data)

    def is_cached_valid(self) -> bool:
        return bool(self._cached_data.get("valid", False))

    def check_license(self, allow_offline_hours: int = 72) -> tuple[bool, str]:
        """
        Kiểm tra trạng thái bản quyền của máy tính.
        1. Thử xác thực trực tuyến qua Keygen API nếu có mạng.
        2. Nếu mất mạng, cho phép dùng ngoại tuyến nếu lần kích hoạt gần nhất chưa quá `allow_offline_hours`.
        """
        account_id = self.get_account_id()
        if not account_id:
            return False, "Chưa cấu hình Account ID của Keygen."

        cached_key = self._cached_data.get("key", "").strip()
        if not cached_key:
            return False, "Phần mềm chưa được kích hoạt bản quyền."

        # Thử validate trực tuyến
        try:
            ok, msg, data = self.validate_online(cached_key)
            if ok:
                return True, msg
            else:
                # Nếu server phản hồi rõ ràng là invalid/expired/suspended
                return False, msg
        except Exception:
            # Lỗi mạng / offline: kiểm tra thời gian ân hạn offline
            last_check_str = self._cached_data.get("last_validated", "")
            if self._cached_data.get("valid", False) and last_check_str:
                try:
                    last_check = datetime.fromisoformat(last_check_str)
                    diff_hours = (datetime.now() - last_check).total_seconds() / 3600.0
                    if diff_hours <= allow_offline_hours:
                        remaining = max(0, int(allow_offline_hours - diff_hours))
                        return True, f"Bản quyền ngoại tuyến hợp lệ (còn {remaining} giờ cần kết nối mạng)."
                except Exception:
                    pass
            return False, "Không thể kết nối máy chủ xác thực bản quyền và đã hết hạn ngoại tuyến."

    def validate_online(self, license_key: str) -> tuple[bool, str, dict[str, Any]]:
        """
        Gửi yêu cầu validate-key tới Keygen.sh.
        Nếu máy tính chưa được đăng ký trong Keygen nhưng còn slot, tự động kích hoạt máy (activate machine).
        """
        account_id = self.get_account_id()
        if not account_id:
            return False, "Vui lòng nhập Keygen Account ID trước khi kích hoạt.", {}

        license_key = license_key.strip()
        if not license_key:
            return False, "Vui lòng nhập License Key.", {}

        # 1. Gọi API validate-key với scope fingerprint
        url = f"{KEYGEN_API_URL}/accounts/{account_id}/licenses/actions/validate-key"
        payload = {
            "meta": {
                "key": license_key,
                "scope": {
                    "fingerprint": self.fingerprint,
                },
            }
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/vnd.api+json",
                "Accept": "application/vnd.api+json",
                "User-Agent": "VisualLoopStudio-Client/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                body = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            try:
                err_body = json.loads(err.read().decode("utf-8"))
                errors = err_body.get("errors", [])
                if errors:
                    err_detail = errors[0].get("detail", "")
                    err_code = errors[0].get("code", "")
                    if err.code == 404 or "not found" in err_detail.lower():
                        return False, "License Key hoặc Account ID không tồn tại!", {}
                    return False, f"Lỗi ({err_code}): {err_detail}", {}
            except Exception:
                pass
            return False, f"Máy chủ Keygen phản hồi lỗi HTTP {err.code}.", {}
        except Exception as exc:
            raise ConnectionError(f"Không thể kết nối tới Keygen: {exc}") from exc

        meta = body.get("meta", {})
        code = meta.get("code", "")
        constant = meta.get("constant", "")
        data_block = body.get("data") or {}
        license_id = data_block.get("id", "")
        attrs = data_block.get("attributes", {})
        expiry = attrs.get("expiry")

        # Trường hợp 1: Key đã hợp lệ cho chính máy này
        if meta.get("valid", False) or constant == "VALID":
            self._save_valid_license(license_key, license_id, expiry, attrs)
            return True, "Kích hoạt bản quyền thành công!", body

        # Trường hợp 2: Key hợp lệ nhưng máy này chưa được activate (FINGERPRINT_SCOPE_MISMATCH hoặc NO_MACHINES)
        if constant in {"FINGERPRINT_SCOPE_MISMATCH", "NO_MACHINES", "NO_MACHINE"}:
            # Thử tự động đăng ký máy tính này vào license
            act_ok, act_msg, machine_data = self._activate_machine(license_key, license_id)
            if act_ok:
                machine_id = machine_data.get("data", {}).get("id", "")
                self._save_valid_license(license_key, license_id, expiry, attrs, machine_id)
                return True, "Kích hoạt máy tính này thành công!", body
            else:
                return False, act_msg, body

        # Các lỗi khác
        detail = meta.get("detail", "License không hợp lệ.")
        if constant == "EXPIRED":
            return False, "License đã hết hạn sử dụng. Vui lòng liên hệ Admin để gia hạn.", body
        if constant == "SUSPENDED":
            return False, "License đã bị tạm khóa bởi Admin.", body
        if constant == "BANNED":
            return False, "License đã bị cấm/thu hồi.", body

        return False, f"Không thể kích hoạt ({constant}): {detail}", body

    def _activate_machine(self, license_key: str, license_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Đăng ký máy tính này (Machine) vào License trên Keygen."""
        account_id = self.get_account_id()
        url = f"{KEYGEN_API_URL}/accounts/{account_id}/machines"
        payload = {
            "data": {
                "type": "machines",
                "attributes": {
                    "fingerprint": self.fingerprint,
                    "platform": "windows",
                    "name": platform.node() or "Windows PC",
                },
                "relationships": {
                    "license": {
                        "data": {
                            "type": "licenses",
                            "id": license_id,
                        }
                    }
                },
            }
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {license_key}",
                "Content-Type": "application/vnd.api+json",
                "Accept": "application/vnd.api+json",
                "User-Agent": "VisualLoopStudio-Client/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                res_data = json.loads(res.read().decode("utf-8"))
                return True, "Thêm máy thành công", res_data
        except urllib.error.HTTPError as err:
            try:
                err_data = json.loads(err.read().decode("utf-8"))
                errors = err_data.get("errors", [])
                if errors:
                    code = errors[0].get("code", "")
                    detail = errors[0].get("detail", "")
                    if code == "MACHINE_LIMIT_EXCEEDED" or "limit" in detail.lower():
                        return False, "Key này đã đạt giới hạn số lượng máy tính cho phép! Không thể kích hoạt thêm máy mới.", {}
                    return False, f"Không thể kích hoạt máy: {detail}", {}
            except Exception:
                pass
            return False, f"Lỗi kích hoạt máy tính: HTTP {err.code}", {}
        except Exception as exc:
            return False, f"Lỗi kết nối khi kích hoạt máy: {exc}", {}

    def deactivate_current_machine(self) -> tuple[bool, str]:
        """Hủy kích hoạt máy tính này khỏi Keygen để giải phóng slot máy."""
        account_id = self.get_account_id()
        cached_key = self._cached_data.get("key", "").strip()
        machine_id = self._cached_data.get("machine_id", "").strip()

        if machine_id and cached_key and account_id:
            url = f"{KEYGEN_API_URL}/accounts/{account_id}/machines/{machine_id}"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {cached_key}",
                    "Accept": "application/vnd.api+json",
                    "User-Agent": "VisualLoopStudio-Client/1.0",
                },
                method="DELETE",
            )
            try:
                with urllib.request.urlopen(req, timeout=15):
                    pass
            except Exception:
                pass

        # Xóa file license cục bộ
        if LICENSE_FILE.exists():
            try:
                LICENSE_FILE.unlink()
            except Exception:
                pass
        self._cached_data = {}
        return True, "Đã hủy kích hoạt bản quyền trên máy tính này thành công."

    def _save_valid_license(
        self,
        key: str,
        license_id: str,
        expiry: str | None,
        attrs: dict[str, Any],
        machine_id: str = "",
    ) -> None:
        self._cached_data = {
            "valid": True,
            "key": key,
            "license_id": license_id,
            "machine_id": machine_id or self._cached_data.get("machine_id", ""),
            "fingerprint": self.fingerprint,
            "expiry": expiry,
            "customer_name": attrs.get("name", "") or attrs.get("metadata", {}).get("customer", ""),
            "last_validated": datetime.now().isoformat(),
        }
        LICENSE_FILE.write_text(json.dumps(self._cached_data, indent=2, ensure_ascii=False), encoding="utf-8")


_manager_instance: LicenseManager | None = None


def get_license_manager() -> LicenseManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = LicenseManager()
    return _manager_instance
