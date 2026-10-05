from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from auth.muse_login import (
    GOOGLE_CONTINUE_TEXT,
    GOOGLE_MUSE_LOGIN_URL,
    MUSE_ALLOWED_HOSTS,
    MUSE_CONTINUE_TEXT,
    MUSE_GOOGLE_TEXT,
    MUSE_LOGIN_TEXT,
    MUSE_START_URL,
    MuseAccountStore,
    MuseLoginService,
    MuseUnsafeNavigationError,
    _attribute,
    _element_text,
    _find_elements,
    _hostname,
    _is_clickable,
    _is_visible,
    _safe_url,
    _utc_now,
    create_muse_chrome_driver,
    validate_muse_start_url,
)
from utils.config import read_json, write_json
from utils.paths import CACHE_DIR, DATA_DIR


MUSE_SESSION_COUNT = 3
MUSE_SESSION_PROFILES_DIR = DATA_DIR / "muse_profiles"
MUSE_SESSION_CHECKPOINT = DATA_DIR / "muse_sessions.json"
MUSE_SESSION_DOWNLOADS_DIR = CACHE_DIR / "MuseDownloads" / "sessions"

PROMPT_SELECTORS = (
    "[data-testid='chat-input']",
    "[data-testid='prompt-input']",
    "[data-testid*='composer' i] textarea",
    "textarea[aria-label*='message' i]",
    "textarea[aria-label*='prompt' i]",
    "textarea[placeholder*='message' i]",
    "textarea[placeholder*='prompt' i]",
    "[contenteditable='true'][role='textbox'][aria-label*='message' i]",
    "[contenteditable='true'][role='textbox'][aria-label*='prompt' i]",
    "textarea",
    "[contenteditable='true'][role='textbox']",
)
SEND_SELECTORS = (
    "button[data-testid='send-button']",
    "button[data-testid*='send' i]",
    "button[aria-label='Send']",
    "button[aria-label*='send message' i]",
    "button[type='submit']",
)
RESPONSE_SELECTORS = (
    "[data-testid='assistant-message']",
    "[data-testid*='assistant-message' i]",
    "[data-message-author-role='assistant']",
    "[data-role='assistant']",
    "[role='article'][aria-label*='Muse' i]",
    "[aria-label*='Muse response' i]",
)
GENERATION_SELECTORS = (
    "[data-testid*='stop' i]",
    "button[aria-label*='stop generating' i]",
    "[aria-busy='true']",
)
QUOTA_MARKERS = (
    "usage limit reached",
    "you've reached your limit",
    "you have reached your limit",
    "not enough credits",
    "insufficient credits",
    "out of credits",
    "weekly limit reached",
    "rate limit",
    "too many requests",
    "quota exceeded",
    "upgrade to continue",
)
TRANSIENT_MARKERS = (
    "network error",
    "connection lost",
    "temporarily unavailable",
    "please try again",
    "something went wrong",
)


class MuseSessionState(str, Enum):
    IDLE = "IDLE"
    OPENING = "OPENING"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    READY = "READY"
    SENDING = "SENDING"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


class MuseSessionError(RuntimeError):
    """A safe, user-facing error that never includes browser session data."""


class MuseSessionBusyError(MuseSessionError):
    pass


class MuseSessionStopped(MuseSessionError):
    pass


class MuseSessionTimeout(MuseSessionError):
    pass


class MuseSessionAuthenticationError(MuseSessionError):
    pass


class MuseSessionQuotaError(MuseSessionError):
    pass


class MuseSessionTransientError(MuseSessionError):
    pass


@dataclass(frozen=True)
class MuseSessionSnapshot:
    session_id: int
    account_id: str
    email: str
    profile_dir: str
    state: MuseSessionState
    prompt: str
    progress: int
    status_message: str
    result: str
    error: str
    task_running: bool
    driver_open: bool


@dataclass
class MuseSession:
    session_id: int
    profile_dir: Path
    account_id: str = ""
    email: str = ""
    driver: Any = None
    task: asyncio.Task[Any] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    state: MuseSessionState = MuseSessionState.IDLE
    prompt: str = ""
    progress: int = 0
    status_message: str = "Sẵn sàng cấu hình tài khoản."
    result: str = ""
    error: str = ""
    active: bool = False
    driver_open: bool = False
    profile_acquired: bool = False
    owned_handles: set[str] = field(default_factory=set)
    known_handles: set[str] = field(default_factory=set)
    executor: ThreadPoolExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"muse-session-{self.session_id}",
        )


DriverFactory = Callable[[Path], Any]
_UNSET = object()


class MuseSessionManager:
    """Own exactly three isolated Muse browser sessions and their asyncio tasks."""

    def __init__(
        self,
        *,
        start_url: str = MUSE_START_URL,
        profile_root: str | Path = MUSE_SESSION_PROFILES_DIR,
        checkpoint_path: str | Path = MUSE_SESSION_CHECKPOINT,
        account_store: MuseAccountStore | None = None,
        driver_factory: DriverFactory | None = None,
        login_timeout: float = 15 * 60,
        generation_timeout: float = 10 * 60,
        poll_interval: float = 0.35,
        stable_seconds: float = 1.5,
        retry_limit: int = 2,
        backoff_base: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.start_url = validate_muse_start_url(start_url)
        self.profile_root = Path(profile_root).resolve()
        self.checkpoint_path = Path(checkpoint_path)
        self.account_store = account_store or MuseAccountStore()
        self._driver_factory = driver_factory or _create_session_chrome_driver
        self.login_timeout = max(1.0, float(login_timeout))
        self.generation_timeout = max(0.05, float(generation_timeout))
        self.poll_interval = max(0.02, float(poll_interval))
        self.stable_seconds = max(0.0, float(stable_seconds))
        self.retry_limit = max(0, int(retry_limit))
        self.backoff_base = max(0.0, float(backoff_base))
        self._clock = clock
        self._state_lock = threading.RLock()
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(target=self._run_loop, name="muse-async-runtime", daemon=True)
        checkpoint = self._read_checkpoint()
        self.sessions: dict[int, MuseSession] = {}
        for session_id in range(1, MUSE_SESSION_COUNT + 1):
            session = MuseSession(
                session_id=session_id,
                profile_dir=self.profile_root / f"account_{session_id}",
            )
            self._restore_session(session, checkpoint.get(session_id, {}))
            self.sessions[session_id] = session
        self._manager_lock: asyncio.Lock | None = None
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5)
        if not self._loop_ready.is_set():
            raise RuntimeError("Không thể khởi động runtime Muse.")
        self._persist()

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return any(session.active for session in self.sessions.values())

    def snapshots(self) -> tuple[MuseSessionSnapshot, ...]:
        with self._state_lock:
            return tuple(self._snapshot(session) for session in self.sessions.values())

    def snapshot(self, session_id: int) -> MuseSessionSnapshot:
        with self._state_lock:
            return self._snapshot(self._require_session(session_id))

    def open_session(
        self,
        session_id: int,
        email: str,
        *,
        password: str = "",
        force_relogin: bool = False,
    ) -> Future[Any]:
        normalized = str(email or "").strip().casefold()
        if not normalized or "@" not in normalized:
            raise ValueError("Hãy nhập email Google hợp lệ cho phiên Muse.")
        secret = bytearray(str(password or "").encode("utf-8"))
        try:
            return self._schedule(
                self._launch_open(int(session_id), normalized, bool(force_relogin), secret)
            )
        except Exception:
            _wipe_secret(secret)
            raise

    def open_all_sessions(
        self,
        credentials: dict[int, tuple[str, str]],
        *,
        force_relogin: bool = False,
    ) -> Future[Any]:
        if set(credentials) != set(range(1, MUSE_SESSION_COUNT + 1)):
            raise ValueError("Cần nhập đủ email Google cho cả 3 tài khoản Muse.")
        prepared: dict[int, tuple[str, bytearray]] = {}
        for session_id in range(1, MUSE_SESSION_COUNT + 1):
            email, password = credentials[session_id]
            normalized = str(email or "").strip().casefold()
            if not normalized or "@" not in normalized:
                for _email, secret in prepared.values():
                    _wipe_secret(secret)
                raise ValueError(f"Email Google của tài khoản {session_id} không hợp lệ.")
            prepared[session_id] = (
                normalized,
                bytearray(str(password or "").encode("utf-8")),
            )
        emails = [email for email, _password in prepared.values()]
        if len(set(emails)) != MUSE_SESSION_COUNT:
            for _email, secret in prepared.values():
                _wipe_secret(secret)
            raise ValueError("Ba phiên Muse phải sử dụng ba tài khoản Google khác nhau.")
        try:
            return self._schedule(
                self._launch_open_all(prepared, bool(force_relogin))
            )
        except Exception:
            for _email, secret in prepared.values():
                _wipe_secret(secret)
            raise

    def send_prompt(self, session_id: int, prompt: str) -> Future[Any]:
        text = str(prompt or "").strip()
        if not text:
            raise ValueError("Prompt Muse không được để trống.")
        return self._schedule(self._launch_send(int(session_id), text))

    def send_all(self, prompts: dict[int, str]) -> Future[Any]:
        cleaned = {int(key): str(value or "").strip() for key, value in prompts.items()}
        return self._schedule(self._launch_send_all(cleaned))

    def update_prompt(self, session_id: int, prompt: str) -> None:
        with self._state_lock:
            session = self._require_session(session_id)
            session.prompt = str(prompt or "")
            if session.state == MuseSessionState.COMPLETED and not session.active:
                session.state = MuseSessionState.READY
                session.status_message = "Sẵn sàng gửi prompt tiếp theo."
                session.progress = 0
            self._persist_locked()

    def stop_session(self, session_id: int) -> bool:
        with self._state_lock:
            session = self._require_session(session_id)
            if not session.active:
                return False
            session.stop_event.set()
            session.state = MuseSessionState.STOPPING
            session.status_message = "Đang dừng riêng phiên Muse này…"
            self._persist_locked()
            return True

    def stop_all(self) -> int:
        stopped = 0
        for session_id in range(1, MUSE_SESSION_COUNT + 1):
            if self.stop_session(session_id):
                stopped += 1
        return stopped

    def shutdown(self, timeout: float = 15.0) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        for session in self.sessions.values():
            session.stop_event.set()
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown_async(timeout), self._loop)
            future.result(timeout=max(1.0, timeout) + 3.0)
        except Exception:
            # Browser errors can contain session identifiers. Shutdown remains best-effort.
            pass
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=3)
            for session in self.sessions.values():
                session.executor.shutdown(wait=False, cancel_futures=True)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._manager_lock = asyncio.Lock()
        self._loop_ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def _schedule(self, coroutine) -> Future[Any]:
        with self._state_lock:
            if self._closed:
                coroutine.close()
                raise RuntimeError("MuseSessionManager đã đóng.")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def _launch_open(
        self,
        session_id: int,
        email: str,
        force_relogin: bool,
        password: bytearray,
    ) -> None:
        session = self._require_session(session_id)
        assert self._manager_lock is not None
        task: asyncio.Task[Any] | None = None
        try:
            async with self._manager_lock:
                self._ensure_available(session)
                self._ensure_unique_account(session_id, email)
                self._bind_account(session, email)
                session.stop_event.clear()
                task = asyncio.create_task(
                    self._open(session, force_relogin, password),
                    name=f"muse-open-{session_id}",
                )
                self._claim_task(session, task)
            await task
        finally:
            if task is not None:
                self._release_task(session, task)
            _wipe_secret(password)

    async def _launch_open_all(
        self,
        credentials: dict[int, tuple[str, bytearray]],
        force_relogin: bool,
    ) -> dict[int, Any]:
        ordered = [
            (
                session_id,
                self._launch_open(
                    session_id,
                    credentials[session_id][0],
                    force_relogin,
                    credentials[session_id][1],
                ),
            )
            for session_id in range(1, MUSE_SESSION_COUNT + 1)
        ]
        results = await asyncio.gather(
            *(coroutine for _session_id, coroutine in ordered),
            return_exceptions=True,
        )
        failures = [
            f"Tài khoản {session_id}: {result}"
            for (session_id, _coroutine), result in zip(ordered, results)
            if isinstance(result, BaseException)
        ]
        if failures:
            raise MuseSessionError("; ".join(failures))
        return {
            session_id: result
            for (session_id, _coroutine), result in zip(ordered, results)
        }

    async def _launch_send(self, session_id: int, prompt: str) -> str | None:
        session = self._require_session(session_id)
        assert self._manager_lock is not None
        async with self._manager_lock:
            self._ensure_available(session)
            self._ensure_can_send(session)
            session.stop_event.clear()
            task = asyncio.create_task(self._send(session, prompt), name=f"muse-send-{session_id}")
            self._claim_task(session, task)
        try:
            return await task
        finally:
            self._release_task(session, task)

    async def _launch_send_all(self, prompts: dict[int, str]) -> dict[int, Any]:
        assert self._manager_lock is not None
        claimed: list[tuple[MuseSession, asyncio.Task[Any]]] = []
        async with self._manager_lock:
            for session_id in range(1, MUSE_SESSION_COUNT + 1):
                session = self.sessions[session_id]
                prompt = prompts.get(session_id, "")
                # Deliberately target only READY sessions, as promised by the UI.
                if session.active or session.state != MuseSessionState.READY or not prompt:
                    continue
                session.stop_event.clear()
                task = asyncio.create_task(self._send(session, prompt), name=f"muse-send-{session_id}")
                self._claim_task(session, task)
                claimed.append((session, task))
        if not claimed:
            return {}
        try:
            results = await asyncio.gather(*(task for _session, task in claimed), return_exceptions=True)
        finally:
            for session, task in claimed:
                self._release_task(session, task)
        return {session.session_id: result for (session, _task), result in zip(claimed, results)}

    async def _open(self, session: MuseSession, force_relogin: bool, password: bytearray) -> None:
        async with session.lock:
            self._set_state(
                session,
                MuseSessionState.OPENING,
                progress=5,
                status_message=f"Đang mở Chrome profile riêng cho {session.email}…",
                error="",
            )
            try:
                await self._loop.run_in_executor(
                    session.executor,
                    self._open_blocking,
                    session,
                    force_relogin,
                    password,
                )
            except MuseSessionStopped:
                await self._loop.run_in_executor(session.executor, self._quit_driver, session)
                self._set_state(
                    session,
                    MuseSessionState.IDLE,
                    progress=0,
                    status_message="Đã dừng phiên đăng nhập Muse.",
                )
            except MuseSessionAuthenticationError as exc:
                self._set_state(
                    session,
                    MuseSessionState.LOGIN_REQUIRED,
                    status_message=str(exc),
                    error="",
                )
                self._set_account_status(session, "manual_required")
                raise
            except Exception as exc:
                await self._loop.run_in_executor(session.executor, self._quit_driver, session)
                message = str(exc) if isinstance(exc, MuseSessionError) else (
                    "Chrome/driver của phiên Muse đã dừng hoặc không phản hồi; có thể khởi động lại riêng phiên này."
                )
                self._set_state(
                    session,
                    MuseSessionState.FAILED,
                    progress=0,
                    status_message="Mở phiên Muse thất bại.",
                    error=message,
                )
                self._set_account_status(session, "error")
                raise MuseSessionError(message) from None

    async def _send(self, session: MuseSession, prompt: str) -> str | None:
        async with session.lock:
            self._set_state(
                session,
                MuseSessionState.SENDING,
                prompt=prompt,
                progress=10,
                status_message="Đang điền và gửi prompt…",
                error="",
            )
            try:
                result = await self._loop.run_in_executor(
                    session.executor,
                    self._send_blocking,
                    session,
                    prompt,
                )
            except MuseSessionStopped:
                next_state = MuseSessionState.READY if session.driver_open else MuseSessionState.IDLE
                self._set_state(
                    session,
                    next_state,
                    progress=0,
                    status_message="Đã dừng tác vụ; phiên có thể chạy lại.",
                )
                return None
            except MuseSessionAuthenticationError as exc:
                self._set_state(
                    session,
                    MuseSessionState.LOGIN_REQUIRED,
                    progress=0,
                    status_message="Phiên đăng nhập cần người dùng xử lý trong Chrome.",
                    error=str(exc),
                )
                self._set_account_status(session, "manual_required")
                raise
            except MuseSessionQuotaError as exc:
                self._set_state(
                    session,
                    MuseSessionState.FAILED,
                    progress=0,
                    status_message="Tài khoản này đã chạm rate limit/quota.",
                    error=str(exc),
                )
                raise
            except Exception as exc:
                message = str(exc) if isinstance(exc, MuseSessionError) else (
                    "Driver Muse không phản hồi; không có cookie/token nào được ghi log."
                )
                if not self._driver_looks_open(session):
                    await self._loop.run_in_executor(session.executor, self._quit_driver, session)
                self._set_state(
                    session,
                    MuseSessionState.FAILED,
                    progress=0,
                    status_message="Tác vụ Muse thất bại.",
                    error=message,
                )
                raise MuseSessionError(message) from None
            else:
                self._set_state(
                    session,
                    MuseSessionState.COMPLETED,
                    progress=100,
                    status_message="Muse đã trả lời hoàn tất.",
                    result=result,
                    error="",
                )
                return result

    def _open_blocking(
        self,
        session: MuseSession,
        force_relogin: bool,
        password: bytearray,
    ) -> None:
        self._check_stopped(session)
        if force_relogin and session.driver is not None:
            self._quit_driver(session)
        if session.driver is None:
            session.profile_dir.mkdir(parents=True, exist_ok=True)
            profile_key = str(session.profile_dir.resolve()).casefold()
            try:
                MuseLoginService._acquire_profile(profile_key)
                session.profile_acquired = True
                session.driver = self._driver_factory(session.profile_dir)
                session.driver_open = True
                session.known_handles = {str(handle) for handle in session.driver.window_handles}
                session.owned_handles = set(session.known_handles)
                try:
                    session.driver.set_page_load_timeout(30)
                except Exception:
                    pass
            except Exception:
                if session.profile_acquired:
                    MuseLoginService._release_profile(profile_key)
                    session.profile_acquired = False
                session.driver = None
                session.driver_open = False
                raise MuseSessionError(
                    "Không thể mở Chrome profile Muse riêng; hãy đóng cửa sổ đang dùng cùng profile rồi thử lại."
                ) from None
        helper = MuseLoginService(start_url=self.start_url, store=self.account_store)
        if not (
            _hostname(_safe_url(session.driver)) == "muse.ai"
            and not helper._is_muse_waitlist(session.driver)
            and helper._is_muse_logged_in(session.driver)
        ):
            self._navigate_with_retry(session, GOOGLE_MUSE_LOGIN_URL)
        actions: set[tuple[str, str]] = set()
        manual_mode = False
        idle_polls = 0
        started = self._clock()

        def inspect_login() -> bool:
            nonlocal manual_mode, idle_polls
            self._check_stopped(session)
            driver = session.driver
            try:
                handles = {str(handle) for handle in driver.window_handles}
            except Exception:
                raise MuseSessionError("Chrome/driver của phiên Muse đã đóng bất ngờ.") from None
            new_handles = handles - session.known_handles
            session.known_handles.update(new_handles)
            session.owned_handles.update(new_handles)
            handle, url, host = helper._switch_to_relevant_window(driver, session.owned_handles)
            if not handle:
                raise MuseSessionError("Không còn tab Muse nào trong Chrome profile của phiên này.")
            if host == "muse.ai" and helper._is_muse_waitlist(driver):
                reset_key = ("muse.ai", "waitlist_reset")
                if reset_key not in actions:
                    actions.add(reset_key)
                    helper._clear_muse_site_session(driver)
                    self._set_state(
                        session,
                        MuseSessionState.OPENING,
                        progress=15,
                        status_message=(
                            "Đã xóa trạng thái waitlist cũ của riêng Muse; đang đăng nhập lại Google một lần…"
                        ),
                    )
                    self._navigate_with_retry(session, GOOGLE_MUSE_LOGIN_URL)
                    return False
                raise MuseSessionError(
                    "Tài khoản này đang ở waitlist hoặc Muse chưa hỗ trợ khu vực của tài khoản. "
                    "Tool không thể vượt giới hạn quyền truy cập; hãy dùng tài khoản đã được Muse cấp quyền."
                )
            if host == "muse.ai" and helper._is_muse_logged_in(driver):
                self._set_state(
                    session,
                    MuseSessionState.READY,
                    progress=100,
                    status_message=f"Đã đăng nhập Muse: {session.email}",
                    error="",
                )
                self._set_account_status(session, "connected")
                return True
            if host == "muse.ai":
                if helper._requires_manual_muse_step(driver):
                    manual_mode = True
                    self._login_required(
                        session,
                        message=(
                            "Muse đang yêu cầu mã email, CAPTCHA, 2FA hoặc bước xác minh riêng. "
                            "Hãy hoàn tất thủ công trong Chrome; phiên sẽ tự tiếp tục."
                        ),
                    )
                else:
                    manual_mode = False
                    login_key = (url, "muse_login")
                    if login_key not in actions and helper._click_by_text(driver, MUSE_LOGIN_TEXT):
                        actions.add(login_key)
                        idle_polls = 0
                        self._set_state(
                            session,
                            MuseSessionState.OPENING,
                            progress=20,
                            status_message="Đã bấm Log in trên Muse; đang chờ form đăng nhập…",
                        )
                    else:
                        google_key = (url, "muse_google")
                        if google_key not in actions and helper._click_by_text(driver, MUSE_GOOGLE_TEXT):
                            actions.add(google_key)
                            idle_polls = 0
                            self._set_state(
                                session,
                                MuseSessionState.OPENING,
                                progress=45,
                                status_message="Đã chọn Continue with Google trên Muse…",
                            )
                            return False
                        identifier_key = (url, "muse_identifier")
                        if identifier_key not in actions and helper._fill_muse_identifier(driver, session.email):
                            actions.add(identifier_key)
                            idle_polls = 0
                            self._set_state(
                                session,
                                MuseSessionState.OPENING,
                                progress=30,
                                status_message=f"Đã điền email {session.email} trên Muse; đang bấm Continue…",
                            )
                        continue_key = (url, "muse_continue")
                        if identifier_key in actions and continue_key not in actions:
                            if helper._click_by_text(driver, MUSE_CONTINUE_TEXT):
                                actions.add(continue_key)
                                idle_polls = 0
                                self._set_state(
                                    session,
                                    MuseSessionState.OPENING,
                                    progress=35,
                                    status_message="Đã bấm Continue; đang chờ Muse xác thực…",
                                )
                        elif continue_key in actions:
                            idle_polls += 1
                        else:
                            idle_polls += 1
            elif host == "auth.muse.ai":
                google_key = (url, "auth_google")
                if google_key not in actions and helper._click_by_text(driver, MUSE_GOOGLE_TEXT):
                    actions.add(google_key)
                    manual_mode = False
                    idle_polls = 0
                    self._set_state(
                        session,
                        MuseSessionState.OPENING,
                        progress=50,
                        status_message="Đã tiếp tục Muse bằng tài khoản Google trong profile…",
                    )
                else:
                    key = (url, "auth_login")
                    if key not in actions and helper._click_by_text(driver, MUSE_LOGIN_TEXT):
                        actions.add(key)
                        manual_mode = False
                        idle_polls = 0
                    else:
                        manual_mode = True
                        self._login_required(
                            session,
                            message="Muse cần thao tác xác minh bổ sung trên auth.muse.ai; hãy hoàn tất trong Chrome.",
                        )
            elif host == "accounts.google.com":
                password_key = (url, "google_password")
                password_submitted = password_key in actions
                if helper._requires_manual_google_step(
                    driver,
                    allow_email=True,
                    allow_password=bool(password) or password_submitted,
                ):
                    manual_mode = True
                    self._login_required(
                        session,
                        message=(
                            "Google yêu cầu CAPTCHA, 2FA, passkey hoặc xác minh thiết bị. "
                            "Hãy hoàn tất thủ công; tool sẽ mở Muse sau khi Google xác thực xong."
                        ),
                    )
                else:
                    manual_mode = False
                    email_key = (url, "google_email")
                    if email_key not in actions and helper._fill_google_email(driver, session.email):
                        actions.add(email_key)
                        idle_polls = 0
                        self._set_state(
                            session,
                            MuseSessionState.OPENING,
                            progress=25,
                            status_message=f"Đã điền email Google {session.email}…",
                        )
                    elif password_key not in actions and password and helper._fill_google_password(driver, password):
                        actions.add(password_key)
                        _wipe_secret(password)
                        idle_polls = 0
                        self._set_state(
                            session,
                            MuseSessionState.OPENING,
                            progress=40,
                            status_message="Đã gửi đăng nhập Google; đang chờ mở Muse…",
                        )
                    else:
                        account_key = (url, "google_account")
                        if account_key not in actions and helper._select_google_account(driver, session.email):
                            actions.add(account_key)
                            idle_polls = 0
                            self._set_state(
                                session,
                                MuseSessionState.OPENING,
                                progress=35,
                                status_message=f"Đã chọn đúng tài khoản Google {session.email}…",
                            )
                        else:
                            continue_key = (url, "google_continue")
                            if continue_key not in actions and helper._click_by_text(driver, GOOGLE_CONTINUE_TEXT):
                                actions.add(continue_key)
                                idle_polls = 0
                            else:
                                idle_polls += 1
                                if idle_polls >= 4:
                                    manual_mode = True
                                    self._login_required(
                                        session,
                                        message=(
                                            "Google chưa chuyển về Muse hoặc không thấy đúng tài khoản. "
                                            "Hãy chọn đúng tài khoản trong Chrome; tool sẽ tự tiếp tục."
                                        ),
                                    )
            return False

        while True:
            self._check_stopped(session)
            if inspect_login():
                return
            elapsed = self._clock() - started
            # Security steps remain open until the user completes them or presses Stop.
            if not manual_mode and elapsed >= self.login_timeout:
                raise MuseSessionTimeout("Đăng nhập Muse đã hết thời gian chờ.")
            try:
                self._wait_until(session.driver, inspect_login, min(5.0, max(0.1, self.login_timeout)))
                return
            except MuseSessionTimeout:
                # Poll in bounded WebDriverWait chunks so Stop stays responsive;
                # A manual Google/Muse security step remains open until user action.
                if not manual_mode and self._clock() - started >= self.login_timeout:
                    raise MuseSessionTimeout("Đăng nhập Muse đã hết thời gian chờ.") from None

    def _send_blocking(self, session: MuseSession, prompt: str) -> str:
        self._check_stopped(session)
        driver = session.driver
        if driver is None:
            raise MuseSessionAuthenticationError("Phiên Chrome chưa mở. Hãy bấm Đăng nhập lại.")
        host = _hostname(_safe_url(driver))
        if host in {"auth.muse.ai", "accounts.google.com"}:
            raise MuseSessionAuthenticationError("Phiên Muse đã hết hạn; hãy hoàn tất đăng nhập trong Chrome.")
        if host != "muse.ai":
            raise MuseSessionError("Muse chuyển tới hostname không được phép; phiên đã dừng tương tác.")

        baseline = self._response_elements(driver)
        baseline_count = len(baseline)
        field = self._wait_for_prompt(session)
        self._fill_prompt(field, prompt)
        self._check_stopped(session)
        self._click_send(driver)
        self._set_state(
            session,
            MuseSessionState.GENERATING,
            progress=20,
            status_message="Muse đang tạo câu trả lời mới…",
        )

        started = self._clock()
        last_text = ""
        last_change = started
        transient_retries = 0

        def inspect_response() -> str | bool:
            nonlocal last_text, last_change, transient_retries
            self._check_stopped(session)
            current_host = _hostname(_safe_url(driver))
            if current_host in {"auth.muse.ai", "accounts.google.com"}:
                raise MuseSessionAuthenticationError("Phiên Muse hết hạn trong lúc chờ kết quả.")
            if current_host != "muse.ai":
                raise MuseSessionError("Muse chuyển tới hostname không được phép; phiên đã dừng tương tác.")
            body = self._body_text(driver).casefold()
            if any(marker in body for marker in QUOTA_MARKERS):
                raise MuseSessionQuotaError(
                    "Tài khoản này đã chạm rate limit/quota; tool không tự chuyển sang tài khoản khác."
                )
            if any(marker in body for marker in TRANSIENT_MARKERS):
                if transient_retries >= self.retry_limit:
                    raise MuseSessionTransientError("Muse liên tục báo lỗi mạng tạm thời sau số lần thử giới hạn.")
                delay = self.backoff_base * (2 ** transient_retries)
                transient_retries += 1
                if session.stop_event.wait(delay):
                    raise MuseSessionStopped("Đã dừng tác vụ Muse.")
                return False
            responses = self._response_elements(driver)
            if len(responses) <= baseline_count:
                self._update_generation_progress(session, started)
                return False
            new_responses = responses[baseline_count:]
            text = next(
                (
                    _element_text(element).strip()
                    for element in reversed(new_responses)
                    if _is_visible(element) and _element_text(element).strip()
                ),
                "",
            )
            if not text:
                self._update_generation_progress(session, started)
                return False
            now = self._clock()
            if text != last_text:
                last_text = text
                last_change = now
                self._set_state(
                    session,
                    MuseSessionState.GENERATING,
                    status_message="Muse đang cập nhật câu trả lời…",
                )
                return False
            still_generating = any(
                _is_visible(element)
                for selector in GENERATION_SELECTORS
                for element in _find_elements(driver, "css selector", selector)
            )
            if now - last_change >= self.stable_seconds and not still_generating:
                return text
            self._update_generation_progress(session, started)
            return False

        try:
            return str(self._wait_until(driver, inspect_response, self.generation_timeout))
        except MuseSessionTimeout:
            raise
        except (MuseSessionError, MuseUnsafeNavigationError):
            raise

    def _wait_for_prompt(self, session: MuseSession) -> Any:
        def locate() -> Any:
            self._check_stopped(session)
            for selector in PROMPT_SELECTORS:
                for element in reversed(_find_elements(session.driver, "css selector", selector)):
                    if _is_clickable(element):
                        return element
            return False

        last_error: Exception | None = None
        for attempt in range(self.retry_limit + 1):
            try:
                return self._wait_until(session.driver, locate, min(20.0, self.generation_timeout))
            except MuseSessionTimeout as exc:
                last_error = exc
                if attempt >= self.retry_limit:
                    break
                delay = self.backoff_base * (2 ** attempt)
                if session.stop_event.wait(delay):
                    raise MuseSessionStopped("Đã dừng tác vụ Muse.")
        raise MuseSessionError("Không tìm thấy ô nhập prompt ổn định trên Muse.") from last_error

    @staticmethod
    def _fill_prompt(field: Any, prompt: str) -> None:
        try:
            field.click()
            tag = str(getattr(field, "tag_name", "") or "").casefold()
            if tag in {"textarea", "input"}:
                field.clear()
            else:
                field.send_keys("\ue009", "a")
            field.send_keys(prompt)
        except Exception:
            raise MuseSessionError("Không thể điền prompt vào giao diện Muse hiện tại.") from None

    @staticmethod
    def _click_send(driver: Any) -> None:
        for selector in SEND_SELECTORS:
            for element in reversed(_find_elements(driver, "css selector", selector)):
                if _is_clickable(element):
                    try:
                        element.click()
                        return
                    except Exception:
                        continue
        accepted = {"send", "generate", "gửi", "tạo"}
        for element in reversed(_find_elements(driver, "css selector", "button, [role='button']")):
            if not _is_clickable(element):
                continue
            labels = {
                " ".join(value.split()).casefold()
                for value in (
                    _element_text(element),
                    _attribute(element, "aria-label"),
                    _attribute(element, "title"),
                )
                if value
            }
            if any(label == value or label.startswith(value + " ") for label in labels for value in accepted):
                try:
                    element.click()
                    return
                except Exception:
                    continue
        raise MuseSessionError("Không tìm thấy nút Send ổn định trên Muse.")

    def _navigate_with_retry(self, session: MuseSession, url: str) -> None:
        for attempt in range(self.retry_limit + 1):
            self._check_stopped(session)
            try:
                session.driver.get(url)
                return
            except Exception:
                if attempt >= self.retry_limit:
                    raise MuseSessionTransientError("Không thể tải Muse sau số lần thử mạng giới hạn.") from None
                delay = self.backoff_base * (2 ** attempt)
                if session.stop_event.wait(delay):
                    raise MuseSessionStopped("Đã dừng tác vụ Muse.")

    def _wait_until(self, driver: Any, condition: Callable[[], Any], timeout: float) -> Any:
        try:
            from selenium.common.exceptions import TimeoutException
            from selenium.webdriver.support.ui import WebDriverWait
        except ModuleNotFoundError:
            raise MuseSessionError("Thiếu Selenium. Hãy cài lại ứng dụng.") from None
        try:
            return WebDriverWait(
                driver,
                max(0.05, float(timeout)),
                poll_frequency=self.poll_interval,
            ).until(lambda _driver: condition())
        except TimeoutException:
            raise MuseSessionTimeout("Muse không hoàn tất trước thời hạn chờ.") from None

    def _login_required(self, session: MuseSession, *, message: str | None = None) -> None:
        self._set_state(
            session,
            MuseSessionState.LOGIN_REQUIRED,
            progress=50,
            status_message=message
            or (
                "Google/Muse cần mã xác minh, CAPTCHA, 2FA, xác minh thiết bị hoặc thao tác bổ sung. "
                "Hãy hoàn tất thủ công trong Chrome; phiên sẽ tự tiếp tục."
            ),
            error="",
        )
        self._set_account_status(session, "manual_required")

    def _response_elements(self, driver: Any) -> list[Any]:
        values: list[Any] = []
        seen: set[str] = set()
        for selector in RESPONSE_SELECTORS:
            for element in _find_elements(driver, "css selector", selector):
                key = str(getattr(element, "id", "") or f"object:{id(element)}")
                if key not in seen:
                    seen.add(key)
                    values.append(element)
        return values

    @staticmethod
    def _body_text(driver: Any) -> str:
        return " ".join(_element_text(element) for element in _find_elements(driver, "tag name", "body"))

    def _update_generation_progress(self, session: MuseSession, started: float) -> None:
        elapsed = self._clock() - started
        progress = min(95, 20 + round(elapsed / max(1.0, self.generation_timeout) * 75))
        self._set_state(session, MuseSessionState.GENERATING, progress=progress)

    @staticmethod
    def _check_stopped(session: MuseSession) -> None:
        if session.stop_event.is_set():
            raise MuseSessionStopped("Đã dừng tác vụ Muse.")

    def _driver_looks_open(self, session: MuseSession) -> bool:
        return bool(session.driver_open and session.driver is not None)

    def _quit_driver(self, session: MuseSession) -> None:
        driver = session.driver
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        session.driver = None
        session.driver_open = False
        session.owned_handles.clear()
        session.known_handles.clear()
        if session.profile_acquired:
            MuseLoginService._release_profile(str(session.profile_dir.resolve()).casefold())
            session.profile_acquired = False

    async def _shutdown_async(self, timeout: float) -> None:
        tasks = [session.task for session in self.sessions.values() if session.task and not session.task.done()]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=max(0.5, timeout))
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
        await asyncio.gather(
            *(
                self._loop.run_in_executor(session.executor, self._quit_driver, session)
                for session in self.sessions.values()
            ),
            return_exceptions=True,
        )
        with self._state_lock:
            for session in self.sessions.values():
                session.active = False
                session.task = None
                session.state = MuseSessionState.IDLE
                session.progress = 0
                session.status_message = "Ứng dụng đã đóng phiên Muse."
            self._persist_locked()

    def _bind_account(self, session: MuseSession, email: str) -> None:
        account = self.account_store.ensure(email)
        with self._state_lock:
            session.email = email
            session.account_id = account.account_id or hashlib.sha256(email.encode("utf-8")).hexdigest()[:24]
            target_profile = str(session.profile_dir.resolve()).casefold()
            for previous in self.account_store.all():
                try:
                    previous_profile = str(Path(previous.profile_dir).resolve()).casefold()
                except OSError:
                    previous_profile = str(previous.profile_dir).casefold()
                if previous.account_id != account.account_id and previous_profile == target_profile:
                    previous.status = "not_checked"
                    previous.updated_at = _utc_now()
                    self.account_store.save(previous)
            account.profile_dir = str(session.profile_dir)
            account.status = "opening"
            account.updated_at = _utc_now()
            self.account_store.save(account)
            self._persist_locked()

    def _set_account_status(self, session: MuseSession, status: str) -> None:
        if not session.account_id:
            return
        account = self.account_store.get(session.account_id)
        if account is None:
            return
        account.profile_dir = str(session.profile_dir)
        account.status = status
        account.updated_at = _utc_now()
        self.account_store.save(account)

    def _ensure_unique_account(self, session_id: int, email: str) -> None:
        with self._state_lock:
            for other in self.sessions.values():
                if other.session_id == session_id:
                    continue
                if other.email.casefold() == email.casefold() and (
                    other.driver_open or other.active or other.state not in {MuseSessionState.IDLE, MuseSessionState.FAILED}
                ):
                    raise ValueError(f"Tài khoản {email} đang được dùng ở Muse {other.session_id}.")

    @staticmethod
    def _ensure_available(session: MuseSession) -> None:
        if session.active:
            raise MuseSessionBusyError(f"Muse {session.session_id} đang có tác vụ chạy.")

    @staticmethod
    def _ensure_can_send(session: MuseSession) -> None:
        if session.state not in {MuseSessionState.READY, MuseSessionState.COMPLETED} or not session.driver_open:
            raise MuseSessionError(f"Muse {session.session_id} chưa sẵn sàng; hãy đăng nhập lại trước.")

    def _claim_task(self, session: MuseSession, task: asyncio.Task[Any]) -> None:
        with self._state_lock:
            session.task = task
            session.active = True
            self._persist_locked()

    def _release_task(self, session: MuseSession, task: asyncio.Task[Any]) -> None:
        with self._state_lock:
            if session.task is task:
                session.task = None
                session.active = False
                session.stop_event.clear()
                self._persist_locked()

    def _set_state(
        self,
        session: MuseSession,
        state: MuseSessionState,
        *,
        prompt: str | object = _UNSET,
        progress: int | object = _UNSET,
        status_message: str | object = _UNSET,
        result: str | object = _UNSET,
        error: str | object = _UNSET,
    ) -> None:
        with self._state_lock:
            session.state = state
            if prompt is not _UNSET:
                session.prompt = str(prompt)
            if progress is not _UNSET:
                session.progress = max(0, min(100, int(progress)))
            if status_message is not _UNSET:
                session.status_message = str(status_message)
            if result is not _UNSET:
                session.result = str(result)
            if error is not _UNSET:
                session.error = str(error)
            self._persist_locked()

    def _snapshot(self, session: MuseSession) -> MuseSessionSnapshot:
        return MuseSessionSnapshot(
            session_id=session.session_id,
            account_id=session.account_id,
            email=session.email,
            profile_dir=str(session.profile_dir),
            state=session.state,
            prompt=session.prompt,
            progress=session.progress,
            status_message=session.status_message,
            result=session.result,
            error=session.error,
            task_running=session.active,
            driver_open=session.driver_open,
        )

    def _require_session(self, session_id: int) -> MuseSession:
        try:
            return self.sessions[int(session_id)]
        except (KeyError, TypeError, ValueError):
            raise ValueError("Session Muse phải là 1, 2 hoặc 3.") from None

    def _read_checkpoint(self) -> dict[int, dict[str, Any]]:
        raw = read_json(self.checkpoint_path, {}) or {}
        values = raw.get("sessions", []) if isinstance(raw, dict) else []
        return {
            int(item.get("session_id")): item
            for item in values
            if isinstance(item, dict) and str(item.get("session_id", "")).isdigit()
        }

    @staticmethod
    def _restore_session(session: MuseSession, value: dict[str, Any]) -> None:
        session.account_id = str(value.get("account_id") or "")
        session.email = str(value.get("email") or "")
        session.prompt = str(value.get("prompt") or "")
        session.result = str(value.get("result") or "")
        session.error = str(value.get("error") or "")
        previous = str(value.get("state") or MuseSessionState.IDLE.value)
        if previous in {MuseSessionState.COMPLETED.value, MuseSessionState.FAILED.value}:
            session.state = MuseSessionState(previous)
            session.status_message = str(value.get("status_message") or "")
            session.progress = int(value.get("progress") or 0)
        else:
            session.state = MuseSessionState.IDLE
            session.status_message = "Đã khôi phục checkpoint; bấm Đăng nhập lại để mở Chrome."
            session.progress = 0

    def _persist(self) -> None:
        with self._state_lock:
            self._persist_locked()

    def _persist_locked(self) -> None:
        write_json(
            self.checkpoint_path,
            {
                "version": 1,
                "sessions": [
                    {
                        "session_id": session.session_id,
                        "account_id": session.account_id,
                        "email": session.email,
                        "profile_dir": str(session.profile_dir),
                        "state": session.state.value,
                        "prompt": session.prompt,
                        "progress": session.progress,
                        "status_message": session.status_message,
                        "result": session.result,
                        "error": session.error,
                    }
                    for session in self.sessions.values()
                ],
            },
        )


_manager_singleton: MuseSessionManager | None = None
_manager_singleton_lock = threading.Lock()


def get_muse_session_manager(*, start_url: str = MUSE_START_URL) -> MuseSessionManager:
    global _manager_singleton
    with _manager_singleton_lock:
        if _manager_singleton is None or _manager_singleton._closed:
            _manager_singleton = MuseSessionManager(start_url=start_url)
        return _manager_singleton


def _create_session_chrome_driver(profile: Path):
    """Create a persistent session driver with an isolated download folder."""
    return create_muse_chrome_driver(profile, MUSE_SESSION_DOWNLOADS_DIR / profile.name)


def _wipe_secret(secret: bytearray) -> None:
    for index in range(len(secret)):
        secret[index] = 0
    secret.clear()
