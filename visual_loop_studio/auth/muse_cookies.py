from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from utils.config import read_json, write_json
from utils.paths import DATA_DIR, USER_DATA_ROOT


MUSE_COOKIE_ACCOUNTS_FILE = DATA_DIR / "muse_cookie_accounts.json"
MUSE_COOKIE_PROFILES_DIR = USER_DATA_ROOT / "MuseCookieProfiles"
MUSE_DEFAULT_URL = "https://muse.ai/"

EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _utc_now_formatted() -> str:
    now = datetime.now()
    return now.strftime("%d/%m %H:%M")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MuseCookieAccount:
    account_id: str
    name: str
    email: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    cookie_raw: str = ""
    status: str = "unchecked"  # "active", "expired", "unchecked", "error"
    status_message: str = "Chưa kiểm tra"
    last_checked: str = ""
    enabled: bool = True
    tasks_completed: int = 0
    created_at: str = ""

    @property
    def status_display(self) -> str:
        time_part = f" • {self.last_checked}" if self.last_checked else ""
        if self.status == "active":
            return f"🟢 Đang hoạt động{time_part}"
        elif self.status == "expired":
            return f"🔴 Hết hạn{time_part}"
        elif self.status == "error":
            return f"🔴 Lỗi{time_part}"
        return f"⚪ Chưa kiểm tra{time_part}"

    @property
    def combo_label(self) -> str:
        if self.email:
            return f"{self.name} - {self.email}"
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MuseCookieAccount":
        return cls(
            account_id=str(data.get("account_id") or ""),
            name=str(data.get("name") or "Muse"),
            email=str(data.get("email") or ""),
            cookies=list(data.get("cookies") or []),
            cookie_raw=str(data.get("cookie_raw") or ""),
            status=str(data.get("status") or "unchecked"),
            status_message=str(data.get("status_message") or "Chưa kiểm tra"),
            last_checked=str(data.get("last_checked") or ""),
            enabled=bool(data.get("enabled", True)),
            tasks_completed=int(data.get("tasks_completed") or 0),
            created_at=str(data.get("created_at") or ""),
        )


def _try_decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.strip().split(".")
    if len(parts) == 3:
        try:
            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += "=" * (4 - rem)
            decoded = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8", errors="ignore")
            data = json.loads(decoded)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def extract_email_from_text_or_cookies(raw_text: str, cookies: list[dict[str, Any]], filename: str = "") -> str:
    """Best-effort extraction of user email from filename, cookie values, or raw text."""
    # 1. Check filename
    if filename:
        clean_stem = Path(filename).stem
        match = EMAIL_REGEX.search(clean_stem)
        if match:
            return match.group(0).lower()

    # 2. Check JWT tokens in cookie values
    for item in cookies:
        val = str(item.get("value") or "")
        if "." in val and len(val) > 40:
            payload = _try_decode_jwt_payload(val)
            if payload:
                for email_key in ("email", "user_email", "mail", "sub"):
                    candidate = str(payload.get(email_key) or "")
                    if candidate and "@" in candidate and "." in candidate:
                        return candidate.lower()

    # 3. Check cookie values directly
    for item in cookies:
        name = str(item.get("name") or "").lower()
        val = str(item.get("value") or "")
        if "email" in name or "user" in name:
            match = EMAIL_REGEX.search(val)
            if match:
                return match.group(0).lower()

    # 4. Search raw text
    match = EMAIL_REGEX.search(raw_text)
    if match:
        return match.group(0).lower()

    return ""


def parse_muse_cookies(raw: str) -> list[dict[str, Any]]:
    """Parse cookies from JSON (Cookie-Editor, J2Team), Netscape format, or Header string."""
    text = str(raw or "").strip()
    if not text:
        return []

    parsed_raw_cookies: list[dict[str, Any]] = []

    # 1. Try parsing JSON
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    parsed_raw_cookies.append(item)
        elif isinstance(data, dict):
            if "cookies" in data and isinstance(data["cookies"], list):
                for item in data["cookies"]:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        parsed_raw_cookies.append(item)
            else:
                for k, v in data.items():
                    if isinstance(v, (str, int, float, bool)):
                        parsed_raw_cookies.append({"name": str(k), "value": str(v)})
    except Exception:
        pass

    # 2. Try parsing Netscape HTTP Cookie format or header string if JSON didn't work
    if not parsed_raw_cookies:
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 7:
                # Netscape: domain, flag, path, secure, expiration, name, value
                parsed_raw_cookies.append({
                    "domain": parts[0],
                    "path": parts[2],
                    "secure": parts[3].upper() == "TRUE",
                    "expirationDate": parts[4],
                    "name": parts[5],
                    "value": parts[6],
                })
            elif ";" in line or "=" in line:
                # Key-value header format: name=val; name2=val2
                pairs = line.split(";")
                for pair in pairs:
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        if k.strip():
                            parsed_raw_cookies.append({"name": k.strip(), "value": v.strip()})

    # 3. Normalize for Selenium add_cookie
    normalized: list[dict[str, Any]] = []
    now_ts = datetime.now().timestamp()
    for item in parsed_raw_cookies:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name:
            continue

        raw_domain = str(item.get("domain") or "").strip().lower()
        if not raw_domain or "muse.ai" not in raw_domain:
            domain = ".muse.ai"
        else:
            domain = raw_domain if raw_domain.startswith(".") else f".{raw_domain}"

        cookie_dict: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", True)),
        }

        if "httpOnly" in item:
            cookie_dict["httpOnly"] = bool(item["httpOnly"])

        # Handle expiration
        exp_raw = item.get("expiry") or item.get("expirationDate")
        if exp_raw is not None:
            try:
                exp_int = int(float(exp_raw))
                if exp_int > now_ts:
                    cookie_dict["expiry"] = exp_int
            except Exception:
                pass

        normalized.append(cookie_dict)

    return normalized


def inject_cookies_to_driver(driver: Any, cookies: list[dict[str, Any]], target_url: str = MUSE_DEFAULT_URL) -> None:
    """Inject a list of cookies into a Selenium WebDriver session."""
    if not cookies:
        return
    try:
        driver.get(target_url)
    except Exception:
        pass

    try:
        driver.delete_all_cookies()
    except Exception:
        pass

    allowed_keys = {"name", "value", "path", "domain", "secure", "httpOnly", "expiry"}
    for cookie in cookies:
        clean = {k: v for k, v in cookie.items() if k in allowed_keys}
        try:
            driver.add_cookie(clean)
        except Exception:
            # Fallback without explicit domain if domain mismatch
            if "domain" in clean:
                clean_no_domain = dict(clean)
                del clean_no_domain["domain"]
                try:
                    driver.add_cookie(clean_no_domain)
                except Exception:
                    pass

    try:
        driver.refresh()
    except Exception:
        try:
            driver.get(target_url)
        except Exception:
            pass


class MuseCookieAccountStore:
    """Thread-safe persistent store for Muse Cookie Accounts."""

    def __init__(self, path: Path | str = MUSE_COOKIE_ACCOUNTS_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def all(self) -> list[MuseCookieAccount]:
        with self._lock:
            data = read_json(self.path, {"version": 1, "accounts": []}) or {}
            raw_list = data.get("accounts", []) if isinstance(data, dict) else []
            return [MuseCookieAccount.from_dict(item) for item in raw_list if isinstance(item, dict)]

    def get(self, account_id: str) -> MuseCookieAccount | None:
        with self._lock:
            for acc in self.all():
                if acc.account_id == account_id:
                    return acc
            return None

    def save(self, account: MuseCookieAccount) -> None:
        with self._lock:
            accounts = [a for a in self.all() if a.account_id != account.account_id]
            accounts.append(account)
            write_json(self.path, {"version": 1, "accounts": [a.to_dict() for a in accounts]})

    def save_all(self, accounts: list[MuseCookieAccount]) -> None:
        with self._lock:
            write_json(self.path, {"version": 1, "accounts": [a.to_dict() for a in accounts]})

    def delete(self, account_id: str) -> bool:
        with self._lock:
            accounts = self.all()
            filtered = [a for a in accounts if a.account_id != account_id]
            if len(filtered) != len(accounts):
                write_json(self.path, {"version": 1, "accounts": [a.to_dict() for a in filtered]})
                return True
            return False

    def clear_all(self) -> None:
        with self._lock:
            write_json(self.path, {"version": 1, "accounts": []})

    def next_name(self) -> str:
        with self._lock:
            existing_names = {a.name for a in self.all()}
            idx = 1
            while f"Muse {idx}" in existing_names:
                idx += 1
            return f"Muse {idx}"

    def update_email(self, account_id: str, email: str) -> bool:
        with self._lock:
            account = self.get(account_id)
            if account:
                account.email = email.strip()
                self.save(account)
                return True
            return False

    def update_status(self, account_id: str, status: str, message: str, last_checked: str = "") -> bool:
        with self._lock:
            account = self.get(account_id)
            if account:
                account.status = status
                account.status_message = message
                account.last_checked = last_checked or _utc_now_formatted()
                self.save(account)
                return True
            return False

    def set_enabled(self, account_id: str, enabled: bool) -> bool:
        with self._lock:
            account = self.get(account_id)
            if account:
                account.enabled = enabled
                self.save(account)
                return True
            return False

    def import_from_json(self, raw_text: str, default_name: str = "", default_email: str = "") -> MuseCookieAccount:
        cookies = parse_muse_cookies(raw_text)
        if not cookies:
            raise ValueError("Không tìm thấy dữ liệu cookie hợp lệ trong chuỗi JSON/text.")

        detected_email = default_email or extract_email_from_text_or_cookies(raw_text, cookies)
        name = default_name or self.next_name()
        account_id = hashlib.sha256(f"{name}-{detected_email}-{_iso_now()}".encode("utf-8")).hexdigest()[:16]

        account = MuseCookieAccount(
            account_id=account_id,
            name=name,
            email=detected_email,
            cookies=cookies,
            cookie_raw=raw_text,
            status="unchecked",
            status_message="Chưa kiểm tra",
            last_checked="",
            enabled=True,
            tasks_completed=0,
            created_at=_iso_now(),
        )
        self.save(account)
        return account

    def import_from_folder(self, folder_path: Path | str) -> list[MuseCookieAccount]:
        folder = Path(folder_path).resolve()
        if not folder.is_dir():
            raise ValueError(f"Thư mục không tồn tại: {folder}")

        added: list[MuseCookieAccount] = []
        files = sorted(
            [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in {".json", ".txt"}],
            key=lambda x: x.name.lower(),
        )

        for f in files:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                cookies = parse_muse_cookies(content)
                if not cookies:
                    continue
                detected_email = extract_email_from_text_or_cookies(content, cookies, filename=f.name)
                name = self.next_name()
                account_id = hashlib.sha256(f"{name}-{detected_email}-{f.name}-{_iso_now()}".encode("utf-8")).hexdigest()[:16]
                account = MuseCookieAccount(
                    account_id=account_id,
                    name=name,
                    email=detected_email,
                    cookies=cookies,
                    cookie_raw=content,
                    status="unchecked",
                    status_message="Chưa kiểm tra",
                    last_checked="",
                    enabled=True,
                    tasks_completed=0,
                    created_at=_iso_now(),
                )
                self.save(account)
                added.append(account)
            except Exception:
                continue

        return added


def check_single_cookie_account(
    account: MuseCookieAccount,
    *,
    headless: bool = True,
    driver_factory: Any = None,
) -> tuple[bool, str, str]:
    """Check whether a Muse cookie account is active or expired.

    Returns (is_active, detected_email, status_message).
    """
    if not account.cookies:
        return False, "", "Không có cookie để kiểm tra"

    try:
        from auth.muse_login import (
            MUSE_APP_SELECTORS,
            _find_elements,
            _hostname,
            _is_visible,
            _safe_url,
            create_muse_chrome_driver,
        )
    except ImportError:
        return False, "", "Thiếu module auth.muse_login"

    profile_dir = MUSE_COOKIE_PROFILES_DIR / f"test_{account.account_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    driver = None
    try:
        if driver_factory:
            driver = driver_factory(profile_dir)
        else:
            driver = create_muse_chrome_driver(profile_dir, headless=headless)

        inject_cookies_to_driver(driver, account.cookies, target_url=MUSE_DEFAULT_URL)

        import time
        started = time.monotonic()
        detected_email = account.email
        is_active = False

        while time.monotonic() - started < 8.0:
            current_host = _hostname(_safe_url(driver))
            if current_host == "muse.ai":
                page_lower = (driver.page_source or "").lower()
                has_login_button = any(
                    btn in page_lower
                    for btn in ("log in with google", "sign in with google", ">log in<", ">sign in<")
                )
                if not has_login_button:
                    for sel in MUSE_APP_SELECTORS:
                        elems = _find_elements(driver, "css selector", sel)
                        if any(_is_visible(el) for el in elems):
                            is_active = True
                            break
                    if is_active:
                        break
            time.sleep(0.5)

        if not is_active:
            current_host = _hostname(_safe_url(driver))
            if current_host == "muse.ai":
                page_lower = (driver.page_source or "").lower()
                if "dashboard" in page_lower or "videos" in page_lower or "workspace" in page_lower:
                    is_active = True

        if is_active:
            return True, detected_email, "Đang hoạt động"
        else:
            return False, detected_email, "Hết hạn hoặc chưa đăng nhập"
    except Exception as exc:
        return False, account.email, f"Lỗi kiểm tra: {exc}"
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
