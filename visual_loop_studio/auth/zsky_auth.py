from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from utils.config import read_json, write_json
from utils.paths import DATA_DIR, USER_DATA_ROOT, ZSKY_ACCOUNTS_FILE, ZSKY_PROFILES_DIR

ZSKY_START_URL = "https://zsky.ai/create"
ZSKY_MY_CREATIONS_URL = "https://zsky.ai/my-creations"
ZSKY_ALLOWED_HOSTS = frozenset({"zsky.ai", "accounts.google.com", "myaccount.google.com"})

_ZSKY_DRIVER_START_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _formatted_now() -> str:
    return datetime.now().strftime("%d/%m %H:%M")


@dataclass
class ZskyAccount:
    account_id: str
    name: str
    email: str = ""
    profile_dir: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    status: str = "unchecked"  # "active", "need_login", "running", "error", "unchecked"
    status_message: str = "Chưa kiểm tra"
    last_checked: str = ""
    enabled: bool = True
    tasks_completed: int = 0
    created_at: str = ""

    @property
    def status_display(self) -> str:
        time_part = f" • {self.last_checked}" if self.last_checked else ""
        if self.status == "active":
            return f"🟢 Đã đăng nhập Google{time_part}"
        elif self.status == "need_login":
            return f"🟡 Chưa đăng nhập{time_part}"
        elif self.status == "running":
            return "▶ Đang farm video..."
        elif self.status == "error":
            return f"🔴 Lỗi ({self.status_message}){time_part}"
        return f"⚪ Chưa kiểm tra{time_part}"

    @property
    def combo_label(self) -> str:
        if self.email:
            return f"{self.name} ({self.email})"
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZskyAccount:
        return cls(
            account_id=str(data.get("account_id") or ""),
            name=str(data.get("name") or "Zsky Account"),
            email=str(data.get("email") or ""),
            profile_dir=str(data.get("profile_dir") or ""),
            cookies=list(data.get("cookies") or []),
            status=str(data.get("status") or "unchecked"),
            status_message=str(data.get("status_message") or "Chưa kiểm tra"),
            last_checked=str(data.get("last_checked") or ""),
            enabled=bool(data.get("enabled", True)),
            tasks_completed=int(data.get("tasks_completed") or 0),
            created_at=str(data.get("created_at") or ""),
        )


class ZskyAccountStore:
    """Quản lý danh sách tài khoản Zsky AI lưu trong zsky_accounts.json"""

    def __init__(self, path: Path = ZSKY_ACCOUNTS_FILE):
        self.path = path
        self._lock = threading.RLock()

    def all(self) -> list[ZskyAccount]:
        with self._lock:
            data = read_json(self.path, {"accounts": []}) or {}
            items = data.get("accounts", []) if isinstance(data, dict) else []
            return [ZskyAccount.from_dict(item) for item in items if isinstance(item, dict)]

    def get(self, account_id: str) -> ZskyAccount | None:
        with self._lock:
            for acc in self.all():
                if acc.account_id == account_id:
                    return acc
            return None

    def save(self, account: ZskyAccount) -> None:
        with self._lock:
            accounts = [acc for acc in self.all() if acc.account_id != account.account_id]
            accounts.append(account)
            accounts.sort(key=lambda a: a.name.casefold())
            write_json(self.path, {"version": 1, "accounts": [acc.to_dict() for acc in accounts]})

    def create_account(self, name: str, email: str = "") -> ZskyAccount:
        with self._lock:
            clean_name = str(name or "").strip() or "Zsky Account"
            clean_email = str(email or "").strip().lower()
            acc_id = "zsky_" + hashlib.md5(f"{clean_name}_{time.time()}".encode("utf-8")).hexdigest()[:12]
            profile_path = ZSKY_PROFILES_DIR / acc_id
            profile_path.mkdir(parents=True, exist_ok=True)
            account = ZskyAccount(
                account_id=acc_id,
                name=clean_name,
                email=clean_email,
                profile_dir=str(profile_path.resolve()),
                status="need_login",
                status_message="Cần mở trình duyệt đăng nhập Google",
                last_checked="",
                enabled=True,
                tasks_completed=0,
                created_at=_iso_now(),
            )
            self.save(account)
            return account

    def delete(self, account_id: str) -> None:
        with self._lock:
            acc = self.get(account_id)
            accounts = [a for a in self.all() if a.account_id != account_id]
            write_json(self.path, {"version": 1, "accounts": [a.to_dict() for a in accounts]})
            if acc and acc.profile_dir:
                try:
                    shutil.rmtree(acc.profile_dir, ignore_errors=True)
                except Exception:
                    pass

    def delete_all(self) -> None:
        with self._lock:
            for acc in self.all():
                if acc.profile_dir:
                    try:
                        shutil.rmtree(acc.profile_dir, ignore_errors=True)
                    except Exception:
                        pass
            write_json(self.path, {"version": 1, "accounts": []})


def create_zsky_chrome_driver(
    profile: Path,
    download_dir: Path | None = None,
    headless: bool = False,
):
    """Khởi tạo Chrome Driver với profile riêng cho tài khoản Zsky, tránh bot-detection."""
    try:
        from selenium import webdriver
    except ModuleNotFoundError as exc:
        raise RuntimeError("Thiếu Selenium. Hãy cài đặt: pip install selenium") from exc

    profile = Path(profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-session-crashed-bubble")
    options.add_argument("--force-renderer-accessibility")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--disable-features=SignInProfileCreation,ChromeSignInBubble,ProfilePicker,IdentityConsistency"
    )
    options.add_argument("--disable-signin-promo")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")

    prefs: dict[str, Any] = {
        "signin.allowed": True,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }

    if download_dir is not None:
        download_dir = Path(download_dir).resolve()
        download_dir.mkdir(parents=True, exist_ok=True)
        prefs.update(
            {
                "download.default_directory": str(download_dir),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            }
        )

    options.add_experimental_option("prefs", prefs)
    options.page_load_strategy = "eager"

    with _ZSKY_DRIVER_START_LOCK:
        driver = webdriver.Chrome(options=options)

    # Vô hiệu hóa cờ navigator.webdriver
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """
            },
        )
    except Exception:
        pass

    # Kích hoạt tính năng download cho headless mode
    if download_dir is not None:
        try:
            driver.execute_cdp_cmd(
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(download_dir),
                },
            )
        except Exception:
            pass

    return driver


def check_zsky_login_status(driver) -> tuple[bool, str]:
    """Kiểm tra trang zsky.ai hiện tại đã đăng nhập tài khoản Google hay chưa.
    Trả về (is_logged_in, email_or_user_label)
    """
    try:
        # 1. Kiểm tra avatar hoặc profile icon ở góc trên bên phải
        avatar_script = """
            const avatar = document.querySelector(
                'img[src*="googleusercontent"], img[alt*="avatar" i], [class*="avatar" i], [aria-label*="account" i], [aria-label*="profile" i]'
            );
            if (avatar) {
                return avatar.getAttribute('title') || avatar.getAttribute('alt') || avatar.getAttribute('src') || 'logged_in';
            }
            // Kiểm tra nút My Creations hoặc nút Free month Pro / Profile button
            const myCreations = [...document.querySelectorAll('a, button, div')].find(
                el => el.innerText && el.innerText.trim().toLowerCase() === 'my creations'
            );
            if (myCreations) return 'my_creations_found';
            return '';
        """
        avatar_val = driver.execute_script(avatar_script)
        if avatar_val:
            # Tìm email trên trang nếu có
            try:
                email_script = """
                    const match = document.body.innerText.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/);
                    return match ? match[0] : '';
                """
                detected_email = driver.execute_script(email_script) or ""
                return True, detected_email or "Tài khoản Google"
            except Exception:
                return True, "Tài khoản Google"

        # 2. Kiểm tra nếu có nút Sign in / Log in
        login_btn = driver.execute_script("""
            const btns = [...document.querySelectorAll('button, a')];
            return Boolean(btns.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                return t === 'sign in' || t === 'log in' || t === 'login' || t.includes('sign in with google');
            }));
        """)
        if login_btn:
            return False, ""

        # Nếu đang ở URL /create hoặc /my-creations mà không có nút login -> xem như đã đăng nhập
        cur_url = driver.current_url.lower()
        if "zsky.ai" in cur_url:
            return True, "Zsky User"
    except Exception:
        pass
    return False, ""


def parse_cookie_input(raw: str) -> list[dict[str, Any]]:
    """Hỗ trợ phân tích cookie dạng JSON hoặc Text dán vào."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            res = []
            for item in data:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    c = {
                        "name": str(item["name"]),
                        "value": str(item["value"]),
                        "domain": str(item.get("domain", ".zsky.ai")),
                        "path": str(item.get("path", "/")),
                    }
                    if "secure" in item:
                        c["secure"] = bool(item["secure"])
                    if "httpOnly" in item:
                        c["httpOnly"] = bool(item["httpOnly"])
                    res.append(c)
            return res
    except Exception:
        pass
    # Text key=val format
    cookies = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, val = pair.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": val.strip(),
                "domain": ".zsky.ai",
                "path": "/",
            })
    return cookies


def inject_cookies_to_driver(driver, cookies: list[dict[str, Any]], target_url: str = ZSKY_START_URL) -> int:
    """Nạp cookies vào trình duyệt Selenium."""
    if not cookies:
        return 0
    try:
        driver.get(target_url)
        time.sleep(1.0)
    except Exception:
        pass
    added = 0
    for c in cookies:
        try:
            cookie_dict = {
                "name": c["name"],
                "value": c["value"],
                "path": c.get("path", "/"),
            }
            dom = c.get("domain", "")
            if dom and "zsky.ai" in dom:
                cookie_dict["domain"] = dom
            driver.add_cookie(cookie_dict)
            added += 1
        except Exception:
            continue
    return added
