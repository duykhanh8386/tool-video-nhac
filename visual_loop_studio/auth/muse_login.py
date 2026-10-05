from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from utils.config import read_json, write_json
from utils.paths import DATA_DIR, USER_DATA_ROOT


MUSE_START_URL = "https://muse.ai/"
GOOGLE_MUSE_LOGIN_URL = (
    "https://accounts.google.com/AccountChooser?"
    "continue=https%3A%2F%2Faccounts.google.com%2FManageAccount&hl=en"
)
MUSE_ALLOWED_HOSTS = frozenset({"muse.ai", "auth.muse.ai", "accounts.google.com"})
MUSE_PROFILES_DIR = USER_DATA_ROOT / "MuseChromeProfiles"
MUSE_ACCOUNTS_FILE = DATA_DIR / "muse_accounts.json"

CLICKABLE_SELECTOR = "button, [role='button'], a[role='button'], a"
ACCOUNT_SELECTOR = "[data-identifier], [data-email], [role='link'], [role='button']"
GOOGLE_EMAIL_SELECTORS = (
    "#identifierId",
    "input[type='email']",
    "input[autocomplete='username']",
)
GOOGLE_PASSWORD_SELECTORS = (
    "input[name='Passwd']",
    "input[type='password']",
    "input[autocomplete='current-password']",
)
GOOGLE_EMAIL_NEXT_SELECTORS = (
    "#identifierNext",
    "button#identifierNext",
    "#identifierNext [role='button']",
)
GOOGLE_PASSWORD_NEXT_SELECTORS = (
    "#passwordNext",
    "button#passwordNext",
    "#passwordNext [role='button']",
)
MUSE_IDENTIFIER_SELECTORS = (
    "input[aria-label='Mobile number or email']",
    "input[placeholder='Mobile number or email']",
    "input[autocomplete='username']",
)
MUSE_PASSWORD_SELECTORS = (
    "input[aria-label='Password']",
    "input[placeholder='Password']",
    "input[type='password']",
    "input[autocomplete='current-password']",
)
MUSE_SECURITY_SELECTORS = (
    "input[type='tel']",
    "input[autocomplete='one-time-code']",
    "input[aria-label*='security code' i]",
    "input[aria-label*='verification code' i]",
    "input[name*='captcha' i]",
    "iframe[src*='recaptcha' i]",
)
GOOGLE_ENTER_KEY = "\ue007"
MUSE_APP_SELECTORS = (
    "[data-testid*='account' i]",
    "[data-testid*='avatar' i]",
    "[aria-label*='account' i]",
    "[aria-label*='profile' i]",
    "img[alt*='avatar' i]",
    "[data-testid*='composer' i]",
    "[aria-label*='chat' i]",
    "textarea",
    "[contenteditable='true'][role='textbox']",
)
MUSE_LOGIN_TEXT = ("log in", "sign in")
MUSE_GOOGLE_TEXT = ("sign in with google", "continue with google", "log in with google")
MUSE_CONTINUE_TEXT = ("continue",)
GOOGLE_CONTINUE_TEXT = ("continue", "allow")
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def google_muse_login_url(email: str) -> str:
    """Choose one exact saved Google account without forcing a new sign-in."""
    query = urlencode(
        {
            "continue": "https://accounts.google.com/ManageAccount",
            "hl": "en",
            "Email": _normalize_email(email),
        }
    )
    return f"https://accounts.google.com/AccountChooser?{query}"


class MuseLoginError(RuntimeError):
    """A safe user-facing Muse login error."""


class MuseLoginTimeout(MuseLoginError):
    pass


class MuseLoginCancelled(MuseLoginError):
    pass


class MuseUnsafeNavigationError(MuseLoginError):
    pass


@dataclass
class MuseAccount:
    account_id: str
    email_label: str
    profile_dir: str
    status: str
    updated_at: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MuseAccount":
        return cls(
            account_id=str(value.get("account_id") or ""),
            email_label=str(value.get("email_label") or ""),
            profile_dir=str(value.get("profile_dir") or ""),
            status=str(value.get("status") or "not_checked"),
            updated_at=str(value.get("updated_at") or ""),
        )


@dataclass(frozen=True)
class MuseLoginResult:
    account: MuseAccount
    reused_session: bool


class MuseAccountStore:
    """Store only account labels/profile paths; credentials are never persisted here."""

    def __init__(self, path: str | Path = MUSE_ACCOUNTS_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()

    def all(self) -> list[MuseAccount]:
        with self._lock:
            raw = read_json(self.path, {"accounts": []}) or {}
            values = raw.get("accounts", []) if isinstance(raw, dict) else []
            return [MuseAccount.from_dict(value) for value in values if isinstance(value, dict)]

    def get(self, account_id: str) -> MuseAccount | None:
        return next((account for account in self.all() if account.account_id == account_id), None)

    def get_by_email(self, email: str) -> MuseAccount | None:
        normalized = _normalize_email(email)
        return next((account for account in self.all() if _normalize_email(account.email_label) == normalized), None)

    def ensure(self, email: str) -> MuseAccount:
        normalized = _normalize_email(email)
        if not normalized:
            raise ValueError("Hãy nhập email tài khoản Google cần dùng với Muse.")
        existing = self.get_by_email(normalized)
        if existing:
            return existing
        account_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        account = MuseAccount(
            account_id=account_id,
            email_label=normalized,
            profile_dir=str(muse_profile_dir(account_id)),
            status="not_checked",
            updated_at=_utc_now(),
        )
        self.save(account)
        return account

    def save(self, account: MuseAccount) -> None:
        with self._lock:
            accounts = [item for item in self.all() if item.account_id != account.account_id]
            accounts.append(account)
            accounts.sort(key=lambda item: item.email_label.casefold())
            write_json(self.path, {"version": 1, "accounts": [asdict(item) for item in accounts]})


DriverFactory = Callable[[Path], Any]
StatusCallback = Callable[[str, str], None]


class MuseLoginService:
    _profiles_lock = threading.RLock()
    _active_profiles: set[str] = set()

    def __init__(
        self,
        *,
        start_url: str = MUSE_START_URL,
        store: MuseAccountStore | None = None,
        driver_factory: DriverFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval: float = 0.5,
    ):
        self.start_url = validate_muse_start_url(start_url)
        self.store = store or MuseAccountStore()
        self._driver_factory = driver_factory or _create_chrome_driver
        self._clock = clock
        self._sleep = sleeper
        self.poll_interval = max(0.05, float(poll_interval))

    def login(
        self,
        email: str,
        *,
        timeout_seconds: float = 300,
        cancelled: Callable[[], bool] | None = None,
        status: StatusCallback | None = None,
    ) -> MuseLoginResult:
        cancelled = cancelled or (lambda: False)
        status = status or (lambda _state, _message: None)
        account = self.store.ensure(email)
        profile = Path(account.profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        profile_key = str(profile).casefold()
        self._acquire_profile(profile_key)
        driver = None
        owned_handles: set[str] = set()
        known_handles: set[str] = set()
        started = self._clock()
        manual_mode = False
        google_idle_polls = 0
        actions: set[tuple[str, str]] = set()
        reused_session = False
        try:
            self._set_status(account, "opening")
            status("opening", "Đang mở Chrome bằng profile Muse riêng của tài khoản…")
            try:
                driver = self._driver_factory(profile)
            except Exception:
                raise MuseLoginError(
                    "Không thể mở Google Chrome cho Muse. Hãy cài/cập nhật Chrome và đóng cửa sổ đang dùng cùng profile Muse."
                ) from None
            try:
                handles = set(driver.window_handles)
                current = str(driver.current_window_handle)
            except Exception:
                raise MuseLoginError("Chrome đã mở nhưng Selenium không nhận được tab điều khiển.") from None
            known_handles.update(handles)
            owned_handles.add(current)
            try:
                driver.set_page_load_timeout(min(30.0, max(5.0, float(timeout_seconds))))
            except Exception:
                pass
            try:
                driver.get(self.start_url)
            except Exception:
                # Single-page apps may exceed page-load timeout while still becoming usable.
                pass
            while True:
                self._check_stop(started, timeout_seconds, cancelled)
                current_handles = self._window_handles(driver)
                new_handles = current_handles - known_handles
                if new_handles:
                    owned_handles.update(new_handles)
                    known_handles.update(new_handles)
                handle, url, host = self._switch_to_relevant_window(driver, owned_handles)
                if not handle:
                    raise MuseLoginError("Tab đăng nhập Muse đã bị đóng trước khi hoàn tất.")
                if host == "muse.ai" and self._is_muse_logged_in(driver):
                    reused_session = not actions
                    self._set_status(account, "connected")
                    status("connected", "Muse đã đăng nhập thành công bằng đúng Chrome profile.")
                    return MuseLoginResult(account=account, reused_session=reused_session)
                if host == "muse.ai":
                    manual_mode = False
                    action_key = (url, "muse_login")
                    if action_key not in actions and self._click_by_text(driver, MUSE_LOGIN_TEXT):
                        actions.add(action_key)
                        status("opening_auth", "Đã mở trang đăng nhập Muse…")
                elif host == "auth.muse.ai":
                    manual_mode = False
                    action_key = (url, "google_login")
                    if action_key not in actions and self._click_by_text(driver, MUSE_GOOGLE_TEXT):
                        actions.add(action_key)
                        status("google", "Đã chọn đăng nhập Muse bằng Google…")
                    else:
                        action_key = (url, "auth_login")
                        if action_key not in actions and self._click_by_text(driver, MUSE_LOGIN_TEXT):
                            actions.add(action_key)
                            status("opening_auth", "Đã mở hộp thoại đăng nhập Muse…")
                elif host == "accounts.google.com":
                    if self._requires_manual_google_step(driver):
                        if not manual_mode:
                            manual_mode = True
                            self._set_status(account, "manual_required")
                            status(
                                "manual_required",
                                "Google yêu cầu mật khẩu, 2FA, CAPTCHA, xác minh thiết bị hoặc quyền mới. "
                                "Hãy hoàn tất thủ công trong Chrome; tool đang chờ và sẽ tự tiếp tục.",
                            )
                    elif not manual_mode:
                        action_key = (url, "account")
                        if action_key not in actions and self._select_google_account(driver, account.email_label):
                            actions.add(action_key)
                            google_idle_polls = 0
                            status("google_account", "Đã chọn đúng tài khoản Google có sẵn trong profile…")
                        else:
                            action_key = (url, "continue")
                            if action_key not in actions and self._click_by_text(driver, GOOGLE_CONTINUE_TEXT):
                                actions.add(action_key)
                                google_idle_polls = 0
                                status("google_continue", "Đã tiếp tục bước đăng nhập Google cơ bản…")
                            else:
                                google_idle_polls += 1
                                if google_idle_polls >= 4:
                                    manual_mode = True
                                    self._set_status(account, "manual_required")
                                    status(
                                        "manual_required",
                                        "Không thấy tài khoản mục tiêu hoặc Google cần thao tác bổ sung. "
                                        "Hãy hoàn tất thủ công trong Chrome; tool không tự nhập thông tin xác thực.",
                                    )
                self._sleep(self.poll_interval)
        except MuseLoginCancelled:
            self._set_status(account, "cancelled")
            status("cancelled", "Đã hủy đăng nhập Muse.")
            raise
        except MuseLoginTimeout:
            self._set_status(account, "timeout")
            status("timeout", "Đăng nhập Muse đã hết thời gian chờ.")
            raise
        except MuseLoginError:
            self._set_status(account, "error")
            raise
        except Exception:
            # WebDriver errors can contain session details or the current URL.
            # Keep them out of UI/log output and return only a fixed message.
            self._set_status(account, "error")
            raise MuseLoginError(
                "Chrome hoặc trang Muse không phản hồi như mong đợi. Không có dữ liệu phiên nào được ghi log."
            ) from None
        finally:
            if driver is not None:
                _close_owned_tabs(driver, owned_handles)
            self._release_profile(profile_key)

    def _switch_to_relevant_window(self, driver: Any, owned_handles: set[str]) -> tuple[str, str, str]:
        available = self._window_handles(driver)
        candidates: list[tuple[str, str, str]] = []
        for handle in list(owned_handles):
            if handle not in available:
                owned_handles.discard(handle)
                continue
            try:
                driver.switch_to.window(handle)
                url = str(driver.current_url or "")
            except Exception:
                continue
            host = _hostname(url)
            if not host and (not url or url.casefold().startswith(("about:blank", "data:,"))):
                candidates.append((handle, url, "pending"))
                continue
            if host not in MUSE_ALLOWED_HOSTS:
                raise MuseUnsafeNavigationError(
                    "Đăng nhập đã chuyển tới hostname không được phép; tool đã dừng mà không tương tác với trang đó."
                )
            candidates.append((handle, url, host))
        for candidate in candidates:
            handle, _url, host = candidate
            if host == "muse.ai":
                try:
                    driver.switch_to.window(handle)
                    if self._is_muse_logged_in(driver):
                        return candidate
                except Exception:
                    continue
        priority = {"accounts.google.com": 0, "auth.muse.ai": 1, "muse.ai": 2, "pending": 3}
        if candidates:
            selected = min(candidates, key=lambda item: priority[item[2]])
            driver.switch_to.window(selected[0])
            return selected
        return "", "", ""

    def _is_muse_logged_in(self, driver: Any) -> bool:
        if _hostname(_safe_url(driver)) != "muse.ai":
            return False
        if self._has_clickable_text(driver, MUSE_LOGIN_TEXT):
            return False
        for selector in MUSE_APP_SELECTORS:
            if any(_is_visible(element) for element in _find_elements(driver, "css selector", selector)):
                return True
        return False

    def _select_google_account(self, driver: Any, email: str) -> bool:
        if _hostname(_safe_url(driver)) != "accounts.google.com":
            raise MuseUnsafeNavigationError("Tool từ chối chọn tài khoản ngoài accounts.google.com.")
        target = _normalize_email(email)
        for element in _find_elements(driver, "css selector", ACCOUNT_SELECTOR):
            if not _is_clickable(element):
                continue
            values = [
                _element_text(element),
                _attribute(element, "data-identifier"),
                _attribute(element, "data-email"),
                _attribute(element, "aria-label"),
            ]
            emails = {match.casefold() for value in values for match in EMAIL_PATTERN.findall(value or "")}
            if target in emails:
                element.click()
                return True
        return False

    def _google_page_has_email(self, driver: Any, email: str) -> bool:
        """Check the visible Google page for the configured account without logging it."""
        if _hostname(_safe_url(driver)) != "accounts.google.com":
            return False
        target = _normalize_email(email)
        if not target:
            return False
        values: list[str] = []
        for element in _find_elements(driver, "css selector", ACCOUNT_SELECTOR):
            values.extend(
                (
                    _element_text(element),
                    _attribute(element, "data-identifier"),
                    _attribute(element, "data-email"),
                    _attribute(element, "aria-label"),
                )
            )
        values.extend(_element_text(element) for element in _find_elements(driver, "tag name", "body"))
        return any(target in {match.casefold() for match in EMAIL_PATTERN.findall(value or "")} for value in values)

    def _requires_manual_google_step(
        self,
        driver: Any,
        *,
        allow_email: bool = False,
        allow_password: bool = False,
    ) -> bool:
        if _hostname(_safe_url(driver)) != "accounts.google.com":
            return False
        url = _safe_url(driver).casefold()
        password_challenge = "/challenge/pwd" in url
        if (
            any(marker in url for marker in ("/challenge/", "/signin/challenge", "/speedbump/"))
            and not (password_challenge and allow_password)
        ):
            return True
        security_selectors = (
            "input[type='tel']",
            "input[autocomplete='one-time-code']",
            "input[name*='captcha' i]",
            "iframe[src*='recaptcha' i]",
        )
        if any(
            _is_visible(element)
            for selector in security_selectors
            for element in _find_elements(driver, "css selector", selector)
        ):
            return True
        body = " ".join(_element_text(item) for item in _find_elements(driver, "tag name", "body")).casefold()
        manual_markers = [
            "2-step verification",
            "2 factor authentication",
            "verify it’s you",
            "verify it's you",
            "enter a verification code",
            "check your phone",
            "confirm your recovery",
            "captcha",
            "choose what",
            "wants access to",
            "additional access",
            "verify your identity",
            "use your passkey",
            "use your security key",
            "scan the qr code",
            "confirm it’s you",
            "confirm it's you",
            "couldn't sign you in",
            "couldn’t sign you in",
            "browser or app may not be secure",
            "try using a different browser",
        ]
        if not allow_password:
            manual_markers.append("enter your password")
        if not allow_email and any(
            _is_visible(element)
            for selector in GOOGLE_EMAIL_SELECTORS
            for element in _find_elements(driver, "css selector", selector)
        ):
            return True
        if not allow_password and any(
            _is_visible(element)
            for selector in GOOGLE_PASSWORD_SELECTORS
            for element in _find_elements(driver, "css selector", selector)
        ):
            return True
        return any(marker in body for marker in manual_markers)

    def _google_automation_blocked(self, driver: Any) -> bool:
        if _hostname(_safe_url(driver)) != "accounts.google.com":
            return False
        body = " ".join(
            _element_text(item)
            for item in _find_elements(driver, "tag name", "body")
        ).casefold()
        return any(
            marker in body
            for marker in (
                "couldn't sign you in",
                "couldn’t sign you in",
                "browser or app may not be secure",
                "try using a different browser",
            )
        )

    def _fill_google_email(self, driver: Any, email: str) -> bool:
        return self._fill_google_field(
            driver,
            GOOGLE_EMAIL_SELECTORS,
            _normalize_email(email),
            GOOGLE_EMAIL_NEXT_SELECTORS,
        )

    def _fill_muse_identifier(self, driver: Any, email: str) -> bool:
        """Fill only Muse's public identifier step; never place a password there."""
        if _hostname(_safe_url(driver)) != "muse.ai":
            raise MuseUnsafeNavigationError("Tool từ chối điền email Muse ngoài muse.ai.")
        value = _normalize_email(email)
        if not value:
            return False
        for selector in MUSE_IDENTIFIER_SELECTORS:
            for element in _find_elements(driver, "css selector", selector):
                if not _is_clickable(element):
                    continue
                try:
                    element.clear()
                    element.send_keys(value)
                    return True
                except Exception:
                    continue
        return False

    def _has_muse_password_field(self, driver: Any) -> bool:
        if _hostname(_safe_url(driver)) != "muse.ai":
            return False
        return any(
            _is_visible(element)
            for selector in MUSE_PASSWORD_SELECTORS
            for element in _find_elements(driver, "css selector", selector)
        )

    def _is_muse_waitlist(self, driver: Any) -> bool:
        if _hostname(_safe_url(driver)) != "muse.ai":
            return False
        body = " ".join(_element_text(item) for item in _find_elements(driver, "tag name", "body")).casefold()
        return (
            "you're on the waitlist" in body
            or "you’re on the waitlist" in body
            or ("muse isn't available" in body and "country or region" in body)
            or ("muse isn’t available" in body and "country or region" in body)
        )

    def _clear_muse_site_session(self, driver: Any) -> None:
        """Clear only Muse site state so an old accidental waitlist flow can be retried safely."""
        if _hostname(_safe_url(driver)) != "muse.ai":
            raise MuseUnsafeNavigationError("Tool từ chối xóa dữ liệu site ngoài muse.ai.")
        try:
            driver.delete_all_cookies()
        except Exception:
            pass
        try:
            driver.execute_script("window.localStorage.clear();window.sessionStorage.clear();")
        except Exception:
            pass

    def _requires_manual_muse_step(self, driver: Any) -> bool:
        """Keep Muse password and all Muse security challenges manual."""
        if _hostname(_safe_url(driver)) != "muse.ai":
            return False
        if any(
            _is_visible(element)
            for selector in MUSE_SECURITY_SELECTORS
            for element in _find_elements(driver, "css selector", selector)
        ):
            return True
        if self._has_muse_password_field(driver):
            return True
        body = " ".join(_element_text(item) for item in _find_elements(driver, "tag name", "body")).casefold()
        return any(
            marker in body
            for marker in (
                "captcha",
                "enter the code",
                "security code",
                "verification code",
                "two-factor authentication",
                "2fa",
                "approve this login",
                "confirm your identity",
                "use your passkey",
            )
        )

    def _fill_google_password(self, driver: Any, password: bytearray) -> bool:
        if not password:
            return False
        value = ""
        try:
            value = password.decode("utf-8")
            return self._fill_google_field(
                driver,
                GOOGLE_PASSWORD_SELECTORS,
                value,
                GOOGLE_PASSWORD_NEXT_SELECTORS,
            )
        finally:
            value = ""

    @staticmethod
    def _fill_google_field(
        driver: Any,
        selectors: tuple[str, ...],
        value: str,
        next_selectors: tuple[str, ...] = (),
    ) -> bool:
        if _hostname(_safe_url(driver)) != "accounts.google.com":
            raise MuseUnsafeNavigationError("Tool từ chối điền thông tin ngoài accounts.google.com.")
        if not value:
            return False
        for selector in selectors:
            for element in _find_elements(driver, "css selector", selector):
                if not _is_clickable(element):
                    continue
                try:
                    element.clear()
                    element.send_keys(value)
                    for next_selector in next_selectors:
                        next_button = next(
                            (
                                candidate
                                for candidate in _find_elements(driver, "css selector", next_selector)
                                if _is_clickable(candidate)
                            ),
                            None,
                        )
                        if next_button is not None:
                            next_button.click()
                            return True
                    element.send_keys(GOOGLE_ENTER_KEY)
                    return True
                except Exception:
                    continue
        return False

    def _click_by_text(self, driver: Any, accepted: tuple[str, ...]) -> bool:
        accepted_values = {value.casefold() for value in accepted}
        for element in _find_elements(driver, "css selector", CLICKABLE_SELECTOR):
            if not _is_clickable(element):
                continue
            labels = (_element_text(element), _attribute(element, "aria-label"), _attribute(element, "title"))
            normalized = {_normalize_text(value) for value in labels if value}
            if any(_label_matches(label, accepted_values) for label in normalized):
                element.click()
                return True
        return False

    def _has_clickable_text(self, driver: Any, accepted: tuple[str, ...]) -> bool:
        accepted_values = {value.casefold() for value in accepted}
        for element in _find_elements(driver, "css selector", CLICKABLE_SELECTOR):
            if not _is_visible(element):
                continue
            labels = (_element_text(element), _attribute(element, "aria-label"), _attribute(element, "title"))
            if any(_label_matches(_normalize_text(label), accepted_values) for label in labels if label):
                return True
        return False

    def _check_stop(self, started: float, timeout_seconds: float, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise MuseLoginCancelled("Đã hủy đăng nhập Muse.")
        if self._clock() - started >= max(1.0, float(timeout_seconds)):
            raise MuseLoginTimeout("Đăng nhập Muse đã hết thời gian chờ.")

    def _set_status(self, account: MuseAccount, value: str) -> None:
        account.status = value
        account.updated_at = _utc_now()
        self.store.save(account)

    @classmethod
    def _acquire_profile(cls, key: str) -> None:
        with cls._profiles_lock:
            if key in cls._active_profiles:
                raise MuseLoginError("Chrome profile Muse của tài khoản này đang được một tác vụ khác sử dụng.")
            cls._active_profiles.add(key)

    @classmethod
    def _release_profile(cls, key: str) -> None:
        with cls._profiles_lock:
            cls._active_profiles.discard(key)

    @staticmethod
    def _window_handles(driver: Any) -> set[str]:
        try:
            return {str(handle) for handle in driver.window_handles}
        except Exception:
            return set()


def validate_muse_start_url(value: str) -> str:
    url = str(value or MUSE_START_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold().rstrip(".") != "muse.ai":
        raise ValueError("MUSE_URL phải dùng HTTPS và hostname chính xác muse.ai.")
    return url


def muse_profile_dir(account_id: str) -> Path:
    safe_id = "".join(character for character in str(account_id) if character.isalnum() or character in "-_")
    if not safe_id:
        raise ValueError("Account ID Muse không hợp lệ.")
    return MUSE_PROFILES_DIR / safe_id


def create_muse_chrome_driver(profile: Path, download_dir: Path | None = None):
    try:
        from selenium import webdriver
    except ModuleNotFoundError as exc:
        raise MuseLoginError("Thiếu Selenium. Hãy cài lại ứng dụng từ requirements.txt.") from exc
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-session-crashed-bubble")
    if download_dir is not None:
        download_dir.mkdir(parents=True, exist_ok=True)
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(download_dir.resolve()),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            },
        )
    options.page_load_strategy = "eager"
    return webdriver.Chrome(options=options)


def _create_chrome_driver(profile: Path):
    return create_muse_chrome_driver(profile)


def _close_owned_tabs(driver: Any, owned_handles: set[str]) -> None:
    for handle in list(owned_handles):
        try:
            if handle not in set(driver.window_handles):
                continue
            driver.switch_to.window(handle)
            driver.close()
        except Exception:
            continue
    try:
        if not driver.window_handles:
            driver.quit()
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass


def _find_elements(driver: Any, by: str, selector: str) -> list[Any]:
    try:
        return list(driver.find_elements(by, selector))
    except Exception:
        return []


def _element_text(element: Any) -> str:
    try:
        return str(element.text or "")
    except Exception:
        return ""


def _attribute(element: Any, name: str) -> str:
    try:
        return str(element.get_attribute(name) or "")
    except Exception:
        return ""


def _is_visible(element: Any) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:
        return False


def _is_clickable(element: Any) -> bool:
    try:
        return bool(element.is_displayed() and element.is_enabled())
    except Exception:
        return False


def _safe_url(driver: Any) -> str:
    try:
        return str(driver.current_url or "")
    except Exception:
        return ""


def _hostname(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").casefold().rstrip(".")


def _normalize_email(value: str) -> str:
    return str(value or "").strip().casefold()


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def _label_matches(label: str, accepted: set[str]) -> bool:
    return any(label == value or label.startswith(value + " ") for value in accepted)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
