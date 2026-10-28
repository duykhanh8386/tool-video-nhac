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

from utils.config import read_json, write_json
from utils.paths import DATA_DIR, MOVOA_ACCOUNTS_FILE, MOVOA_PROFILES_DIR, USER_DATA_ROOT

MOVOA_START_URL = "https://movoa.ai/video/text-to-video"
MOVOA_MY_CLIPS_URL = "https://movoa.ai/my-clips"
MOVOA_ALLOWED_HOSTS = frozenset({"movoa.ai", "accounts.google.com", "myaccount.google.com"})

_MOVOA_DRIVER_START_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _formatted_now() -> str:
    return datetime.now().strftime("%d/%m %H:%M")


@dataclass
class MovoaAccount:
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
    def from_dict(cls, data: dict[str, Any]) -> MovoaAccount:
        return cls(
            account_id=str(data.get("account_id") or ""),
            name=str(data.get("name") or "Movoa Account"),
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


class MovoaAccountStore:
    """Quản lý danh sách tài khoản Movoa AI lưu trong movoa_accounts.json"""

    def __init__(self, path: Path = MOVOA_ACCOUNTS_FILE):
        self.path = path
        self._lock = threading.RLock()

    def all(self) -> list[MovoaAccount]:
        with self._lock:
            data = read_json(self.path, {"accounts": []}) or {}
            items = data.get("accounts", []) if isinstance(data, dict) else []
            return [MovoaAccount.from_dict(item) for item in items if isinstance(item, dict)]

    def get(self, account_id: str) -> MovoaAccount | None:
        with self._lock:
            for acc in self.all():
                if acc.account_id == account_id:
                    return acc
            return None

    def save(self, account: MovoaAccount) -> None:
        with self._lock:
            accounts = [acc for acc in self.all() if acc.account_id != account.account_id]
            accounts.append(account)
            accounts.sort(key=lambda a: a.name.casefold())
            write_json(self.path, {"version": 1, "accounts": [acc.to_dict() for acc in accounts]})

    def create_account(self, name: str, email: str = "") -> MovoaAccount:
        with self._lock:
            clean_name = str(name or "").strip() or "Movoa Account"
            clean_email = str(email or "").strip().lower()
            acc_id = "movoa_" + hashlib.md5(f"{clean_name}_{time.time()}".encode("utf-8")).hexdigest()[:12]
            profile_path = MOVOA_PROFILES_DIR / acc_id
            profile_path.mkdir(parents=True, exist_ok=True)
            account = MovoaAccount(
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

    def next_name(self) -> str:
        with self._lock:
            existing = {a.name.strip().casefold() for a in self.all()}
            idx = 1
            while f"movoa {idx}".casefold() in existing:
                idx += 1
            return f"Movoa {idx}"

    def import_from_json(self, raw_text: str, default_name: str = "", default_email: str = "") -> MovoaAccount:
        """Thêm trực tiếp tài khoản mới từ chuỗi cookie JSON hoặc text (giống Muse)."""
        with self._lock:
            cookies = parse_cookie_input(raw_text)
            if not cookies:
                raise ValueError("Không tìm thấy dữ liệu cookie hợp lệ trong chuỗi JSON/text.")

            detected_email = default_email.strip() or extract_email_from_text(raw_text, cookies)
            name = default_name.strip() or self.next_name()
            acc_id = "movoa_" + hashlib.md5(f"{name}_{detected_email}_{time.time()}".encode("utf-8")).hexdigest()[:12]
            profile_path = MOVOA_PROFILES_DIR / acc_id
            profile_path.mkdir(parents=True, exist_ok=True)
            clean_chrome_profile_locks(profile_path)

            account = MovoaAccount(
                account_id=acc_id,
                name=name,
                email=detected_email,
                profile_dir=str(profile_path.resolve()),
                cookies=cookies,
                status="active",
                status_message=f"Đã nạp {len(cookies)} cookies",
                last_checked=_formatted_now(),
                enabled=True,
                tasks_completed=0,
                created_at=_iso_now(),
            )
            self.save(account)
            return account


EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


def extract_email_from_text(text: str, cookies: list[dict[str, Any]] | None = None) -> str:
    """Trích xuất địa chỉ email từ nội dung chuỗi hoặc cookies."""
    if cookies:
        for c in cookies:
            for key in ("value", "name"):
                val = str(c.get(key) or "")
                m = EMAIL_REGEX.search(val)
                if m:
                    em = m.group(0).lower()
                    if not any(skip in em for skip in ("support@", "help@", "info@", "contact@", "w3.org", "schema.org")):
                        return em
    if text:
        for m in EMAIL_REGEX.finditer(text):
            em = m.group(0).lower()
            if not any(skip in em for skip in ("support@", "help@", "info@", "contact@", "w3.org", "schema.org")):
                return em
    return ""


def kill_chrome_on_profile(profile_dir: Path | str) -> None:
    """Tắt các tiến trình chrome.exe cũ đang chiếm dụng thư mục profile này."""
    p_str = str(Path(profile_dir).resolve())
    if not p_str or len(p_str) < 5:
        return
    try:
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{p_str}*' }} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        )
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5, **kwargs)
        time.sleep(0.3)
    except Exception:
        pass


def clean_chrome_profile_locks(profile: Path | str) -> None:
    """Xóa các file lock của Chrome trong thư mục profile để tránh lỗi crash / DevToolsActivePort."""
    p = Path(profile).resolve()
    if not p.is_dir():
        return
    kill_chrome_on_profile(p)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort", "lockfile"):
        try:
            target = p / name
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
        except Exception:
            pass


def create_movoa_chrome_driver(
    profile: Path,
    download_dir: Path | None = None,
    headless: bool = False,
):
    """Khởi tạo Chrome Driver với profile riêng cho tài khoản Movoa, tránh bot-detection."""
    try:
        from selenium import webdriver
    except ModuleNotFoundError as exc:
        raise RuntimeError("Thiếu Selenium. Hãy cài đặt: pip install selenium") from exc

    profile = Path(profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    clean_chrome_profile_locks(profile)

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
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

    with _MOVOA_DRIVER_START_LOCK:
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


def check_movoa_login_status(driver) -> tuple[bool, str]:
    """Kiểm tra trang movoa.ai hiện tại đã đăng nhập tài khoản Google hay chưa.
    Trả về (is_logged_in, email_or_user_label)
    """
    try:
        check_script = """
            // 1. Kiểm tra góc dưới bên trái có avatar hoặc user info
            const userImg = document.querySelector('img[src*="googleusercontent"], img[alt*="avatar" i], [class*="avatar" i]');
            const bodyText = (document.body.innerText || '');

            // Tìm email trong phần user bar (thường ở sidebar dưới cùng)
            const emailMatch = bodyText.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/);
            const hasFreeAccount = bodyText.includes('Free account') || bodyText.includes('MiniMax H3 · 480p');

            // Kiểm tra nút Sign in ở góc trên hoặc Guest ở góc dưới
            const signinBtn = Array.from(document.querySelectorAll('button, a')).find(
                b => (b.innerText || '').trim().toLowerCase() === 'sign in'
            );
            const isGuest = bodyText.includes('Guest') && bodyText.includes('Not signed in');

            if (emailMatch && (hasFreeAccount || userImg)) {
                return { loggedIn: true, label: emailMatch[0] };
            }

            if (!signinBtn && !isGuest && (hasFreeAccount || userImg)) {
                return { loggedIn: true, label: 'Đã đăng nhập Movoa' };
            }

            if (signinBtn || isGuest) {
                return { loggedIn: false, label: '' };
            }

            // Nếu đang trong dashboard movoa.ai và không có nút Sign in
            if (window.location.hostname.includes('movoa.ai')) {
                return { loggedIn: true, label: emailMatch ? emailMatch[0] : 'Tài khoản Google' };
            }
            return { loggedIn: false, label: '' };
        """
        res = driver.execute_script(check_script)
        if isinstance(res, dict) and res.get("loggedIn"):
            return True, str(res.get("label") or "Đã đăng nhập")
    except Exception:
        pass
    return False, ""


def parse_cookie_input(raw: str) -> list[dict[str, Any]]:
    """Phân tích cookie JSON (Cookie-Editor, J2Team), Netscape format, hoặc Header string."""
    text = (raw or "").strip()
    if not text:
        return []

    parsed_raw: list[dict[str, Any]] = []
    # 1. Thử phân tích JSON
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    parsed_raw.append(item)
        elif isinstance(data, dict):
            if "cookies" in data and isinstance(data["cookies"], list):
                for item in data["cookies"]:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        parsed_raw.append(item)
            else:
                for k, v in data.items():
                    if isinstance(v, (str, int, float, bool)):
                        parsed_raw.append({"name": str(k), "value": str(v)})
    except Exception:
        pass

    # 2. Thử phân tích Netscape hoặc Key=Value nếu không phải JSON
    if not parsed_raw:
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 7:
                parsed_raw.append({
                    "domain": parts[0],
                    "path": parts[2],
                    "secure": parts[3].upper() == "TRUE",
                    "expirationDate": parts[4],
                    "name": parts[5],
                    "value": parts[6],
                })
            elif ";" in line or "=" in line:
                for pair in line.split(";"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        if k.strip():
                            parsed_raw.append({"name": k.strip(), "value": v.strip()})

    # 3. Chuẩn hóa cho Selenium
    normalized: list[dict[str, Any]] = []
    for item in parsed_raw:
        name = str(item.get("name") or "").strip()
        val = str(item.get("value") or "").strip()
        if not name:
            continue
        raw_dom = str(item.get("domain") or "").strip().lower()
        if not raw_dom or "movoa.ai" not in raw_dom:
            dom = ".movoa.ai"
        else:
            dom = raw_dom if raw_dom.startswith(".") else f".{raw_dom}"

        c: dict[str, Any] = {
            "name": name,
            "value": val,
            "domain": dom,
            "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", True)),
        }
        if "httpOnly" in item:
            c["httpOnly"] = bool(item["httpOnly"])
        exp = item.get("expiry") or item.get("expirationDate")
        if exp is not None:
            try:
                c["expiry"] = int(float(exp))
            except Exception:
                pass
        normalized.append(c)

    return normalized


def inject_cookies_to_driver(driver, cookies: list[dict[str, Any]], target_url: str = MOVOA_START_URL) -> int:
    """Nạp cookies vào phiên Chrome Selenium và làm mới trang để nhận phiên đăng nhập."""
    if not cookies:
        return 0
    try:
        driver.get(target_url)
        time.sleep(1.0)
    except Exception:
        pass

    try:
        driver.delete_all_cookies()
    except Exception:
        pass

    allowed_keys = {"name", "value", "path", "domain", "secure", "httpOnly", "expiry"}
    added = 0
    for cookie in cookies:
        clean = {k: v for k, v in cookie.items() if k in allowed_keys}
        try:
            driver.add_cookie(clean)
            added += 1
        except Exception:
            # Nếu domain không khớp, thử bỏ trường domain để Selenium tự gán theo host hiện tại
            if "domain" in clean:
                clean_no_dom = dict(clean)
                del clean_no_dom["domain"]
                try:
                    driver.add_cookie(clean_no_dom)
                    added += 1
                except Exception:
                    pass

    try:
        driver.refresh()
        time.sleep(1.5)
    except Exception:
        try:
            driver.get(target_url)
        except Exception:
            pass

    return added

