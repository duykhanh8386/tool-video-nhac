from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from selenium.common.exceptions import TimeoutException

from auth.muse_login import (
    ACCOUNT_SELECTOR,
    CLICKABLE_SELECTOR,
    GOOGLE_ENTER_KEY,
    GOOGLE_PASSWORD_SELECTORS,
    MUSE_APP_SELECTORS,
    MUSE_CONTINUE_TEXT,
    MUSE_IDENTIFIER_SELECTORS,
    MUSE_PASSWORD_SELECTORS,
    MUSE_SECURITY_SELECTORS,
    MuseAccountStore,
    google_muse_login_url,
    google_youtube_login_url,
)
from auth.muse_sessions import (
    GENERATION_SELECTORS,
    PROMPT_SELECTORS,
    RESPONSE_SELECTORS,
    SEND_SELECTORS,
    MuseSessionError,
    MuseSessionManager,
    MuseSessionQuotaError,
    MuseSessionState,
    get_muse_session_manager,
)


class FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        element_id: str = "",
        tag_name: str = "div",
        attrs: dict[str, str] | None = None,
        on_click=None,
        on_send=None,
    ) -> None:
        self.text = text
        self.id = element_id
        self.tag_name = tag_name
        self.attrs = attrs or {}
        self.on_click = on_click
        self.on_send = on_send
        self.value = ""

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self.attrs.get(name, "")

    def click(self):
        if self.on_click:
            self.on_click()

    def clear(self):
        self.value = ""

    def send_keys(self, *values):
        self.value += "".join(str(value) for value in values)
        if self.on_send:
            self.on_send(*values)


class FakeSwitchTo:
    def __init__(self, driver) -> None:
        self.driver = driver

    def window(self, handle):
        if handle != "main" or self.driver.closed:
            raise RuntimeError("closed")


class FakeDriver:
    def __init__(
        self,
        profile: Path,
        *,
        response: str = "done",
        response_barrier: threading.Barrier | None = None,
        login_required: bool = False,
        never_respond: bool = False,
        quota: bool = False,
        login_barrier: threading.Barrier | None = None,
        muse_identifier_login: bool = False,
        muse_security_code: bool = False,
        waitlist: bool = False,
        unverified_manage_account: bool = False,
        muse_page_load_timeout: bool = False,
        google_two_factor: bool = False,
        transient_window_handle_errors: int = 0,
        youtube_channel_picker: bool = False,
        google_myaccount_redirect: bool = False,
    ) -> None:
        self.profile = Path(profile)
        self.response = response
        self.response_barrier = response_barrier
        self.never_respond = never_respond
        self.quota = quota
        self.login_barrier = login_barrier
        self.muse_identifier_login = muse_identifier_login
        self.muse_security_code = muse_security_code
        self.waitlist = waitlist
        self.unverified_manage_account = unverified_manage_account
        self.muse_page_load_timeout = muse_page_load_timeout
        self.google_two_factor = google_two_factor
        self.transient_window_handle_errors = max(0, int(transient_window_handle_errors))
        self.window_handle_reads = 0
        self.youtube_channel_picker = youtube_channel_picker
        self.google_myaccount_redirect = google_myaccount_redirect
        self.google_authenticated = not login_required
        self.google_email = ""
        self.muse_stage = ""
        self.closed = False
        self.quit_called = False
        self.logged_in = not login_required
        if waitlist:
            self._current_url = "https://muse.ai/access"
        elif login_required:
            self._current_url = "https://accounts.google.com/signin/v2/challenge/pwd"
        elif muse_identifier_login:
            self._current_url = "https://muse.ai/?aymh_complete=1"
            self.muse_stage = "landing"
        else:
            self._current_url = "https://muse.ai/chat"
        self.switch_to = FakeSwitchTo(self)
        self.responses: list[FakeElement] = []
        self.body = FakeElement("", element_id="body")
        self.prompt = FakeElement(element_id="prompt", tag_name="textarea")
        self.send = FakeElement("Send", element_id="send", on_click=self._send)
        self.identifier = FakeElement(
            element_id="muse-identifier",
            tag_name="input",
            attrs={"aria-label": "Mobile number or email", "autocomplete": "username"},
        )
        self.login_button = FakeElement("Log in", element_id="login", on_click=self._open_muse_login)
        self.continue_button = FakeElement("Continue", element_id="continue", on_click=self._continue_login)
        self.password = FakeElement(
            element_id="password",
            tag_name="input",
            attrs={"type": "password", "name": "Passwd"},
            on_send=self._password_send,
        )
        self.security_code = FakeElement(
            element_id="security-code",
            tag_name="input",
            attrs={"aria-label": "6-digit security code", "autocomplete": "one-time-code"},
        )
        self.thread_ids: set[int] = set()
        self.requested_urls: list[str] = []
        self.cookies_cleared = 0
        self.site_storage_cleared = 0

    def _record_thread(self):
        self.thread_ids.add(threading.get_ident())

    @property
    def window_handles(self):
        self._record_thread()
        self.window_handle_reads += 1
        if self.closed:
            raise RuntimeError("driver died")
        if self.window_handle_reads > 1 and self.transient_window_handle_errors:
            self.transient_window_handle_errors -= 1
            raise RuntimeError("temporary redirect")
        return ["main"]

    @property
    def current_window_handle(self):
        self._record_thread()
        return "main"

    @property
    def current_url(self):
        self._record_thread()
        if self.closed:
            raise RuntimeError("driver died")
        return self._current_url

    def set_page_load_timeout(self, _seconds):
        self._record_thread()

    def get(self, _url):
        self._record_thread()
        url = str(_url)
        self.requested_urls.append(url)
        if url.startswith("https://accounts.google.com/"):
            if "Email=" in url:
                self.google_email = parse_qs(urlparse(url).query).get("Email", [""])[0]
            if self.unverified_manage_account:
                self._current_url = "https://accounts.google.com/ManageAccount"
            elif self.google_authenticated and parse_qs(urlparse(url).query).get("service") == ["youtube"]:
                self._current_url = "https://studio.youtube.com/"
            else:
                self._current_url = (
                    "https://myaccount.google.com/?pli=1"
                    if self.google_authenticated and self.google_myaccount_redirect
                    else "https://accounts.google.com/ManageAccount"
                    if self.google_authenticated
                    else "https://accounts.google.com/signin/v2/challenge/pwd"
                )
        elif url.startswith("https://muse.ai/"):
            if self.waitlist:
                self._current_url = "https://muse.ai/access"
                self.logged_in = False
            elif self.muse_identifier_login:
                self._current_url = "https://muse.ai/"
                self.muse_stage = "landing"
                self.logged_in = False
            else:
                self._current_url = "https://muse.ai/chat"
                self.logged_in = True
            if self.muse_page_load_timeout:
                raise TimeoutException("page load timed out after Muse became reachable")

    def delete_all_cookies(self):
        self._record_thread()
        self.cookies_cleared += 1

    def execute_script(self, script, *_args):
        self._record_thread()
        if "localStorage.clear" in str(script):
            self.site_storage_cleared += 1
        return True

    def find_elements(self, by, selector):
        self._record_thread()
        if self.closed:
            raise RuntimeError("driver died")
        if by == "css selector" and selector == CLICKABLE_SELECTOR:
            if self.muse_identifier_login and self._current_url.startswith("https://muse.ai/"):
                if self.muse_stage == "landing":
                    return [self.login_button]
                if self.muse_stage == "identifier":
                    return [self.continue_button]
            return []
        if by == "css selector" and selector == ACCOUNT_SELECTOR:
            if self._current_url.startswith(("https://accounts.google.com/", "https://myaccount.google.com/")) and self.google_email:
                if self.unverified_manage_account:
                    return [FakeElement("other@example.com", attrs={"data-email": "other@example.com"})]
                return [FakeElement(self.google_email, attrs={"data-email": self.google_email})]
            return []
        if by == "css selector" and selector in MUSE_APP_SELECTORS:
            return [FakeElement("Muse", element_id="app")] if self.logged_in else []
        if by == "css selector" and selector in MUSE_IDENTIFIER_SELECTORS:
            if self.muse_identifier_login and self.muse_stage == "identifier":
                return [self.identifier]
            return []
        if (
            by == "css selector"
            and selector in MUSE_PASSWORD_SELECTORS
            and self._current_url.startswith("https://muse.ai/")
        ):
            if self.muse_identifier_login and self.muse_stage == "password":
                return [self.password]
            return []
        if (
            by == "css selector"
            and selector in MUSE_SECURITY_SELECTORS
            and self._current_url.startswith("https://muse.ai/")
        ):
            if self.muse_identifier_login and self.muse_stage == "security":
                return [self.security_code]
            return []
        if by == "css selector" and selector in GOOGLE_PASSWORD_SELECTORS:
            on_google = self._current_url.startswith("https://accounts.google.com/")
            return [self.password] if not self.logged_in and on_google else []
        if by == "css selector" and selector in PROMPT_SELECTORS:
            return [self.prompt] if self.logged_in else []
        if by == "css selector" and selector in SEND_SELECTORS:
            return [self.send] if self.logged_in else []
        if by == "css selector" and selector in RESPONSE_SELECTORS:
            return list(self.responses)
        if by == "css selector" and selector in GENERATION_SELECTORS:
            return []
        if by == "tag name" and selector == "body":
            if self._current_url.startswith(("https://accounts.google.com/", "https://myaccount.google.com/")):
                if self.google_two_factor and "/challenge/totp" in self._current_url:
                    self.body.text = "2-Step Verification"
                else:
                    self.body.text = "other@example.com" if self.unverified_manage_account else self.google_email
            elif self.waitlist:
                self.body.text = "You're on the waitlist. Muse isn't available in your country or region yet."
            else:
                self.body.text = "rate limit reached" if self.quota else ""
            return [self.body]
        return []

    def _open_muse_login(self):
        if self.muse_identifier_login:
            self.muse_stage = "identifier"

    def _continue_login(self):
        if self.muse_identifier_login and self.identifier.value:
            if self.muse_security_code:
                self.muse_stage = "security"
            else:
                self.complete_login()

    def _password_send(self, *values):
        if GOOGLE_ENTER_KEY in {str(value) for value in values}:
            if self.login_barrier is not None:
                self.login_barrier.wait(timeout=2)
            if self._current_url.startswith("https://accounts.google.com/"):
                if self.google_two_factor:
                    self._current_url = "https://accounts.google.com/signin/challenge/totp"
                else:
                    self.google_authenticated = True
                    self._current_url = (
                        "https://www.youtube.com/signin_prompt?app=desktop&next=https://studio.youtube.com/"
                        if self.youtube_channel_picker
                        else "https://studio.youtube.com/"
                    )
            else:
                self.complete_login()

    def _send(self):
        self._record_thread()
        if self.response_barrier is not None:
            self.response_barrier.wait(timeout=2)
        if not self.never_respond and not self.quota:
            self.responses.append(
                FakeElement(self.response, element_id=f"response-{len(self.responses) + 1}")
            )

    def complete_login(self):
        if self._current_url.startswith("https://accounts.google.com/"):
            self.google_authenticated = True
            self.google_two_factor = False
            self._current_url = (
                "https://www.youtube.com/signin_prompt?app=desktop&next=https://studio.youtube.com/"
                if self.youtube_channel_picker
                else "https://studio.youtube.com/"
            )
        else:
            self.logged_in = True
            self.muse_stage = ""
            self._current_url = "https://muse.ai/chat"

    def quit(self):
        self._record_thread()
        self.quit_called = True
        self.closed = True


class FakeNativeBrowserProcess:
    def __init__(self):
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.running = False


class MuseSessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.drivers: dict[int, FakeDriver] = {}
        self.barrier: threading.Barrier | None = None
        self.login_barrier: threading.Barrier | None = None
        self.driver_options: dict[int, dict] = {}
        self.native_browser_launches: list[tuple[Path, str]] = []
        self.attached_profiles: list[Path] = []
        self.chrome_profile_confirmations: list[int] = []
        self.manager = self._manager()

    def tearDown(self):
        self.manager.shutdown(timeout=2)
        self.temp.cleanup()

    def _manager(self, **overrides) -> MuseSessionManager:
        profiles = self.root / "data" / "muse_profiles"

        def factory(profile: Path):
            session_id = int(profile.name.rsplit("_", 1)[1])
            driver = FakeDriver(
                profile,
                response=f"result-{session_id}",
                response_barrier=self.barrier,
                login_barrier=self.login_barrier,
                **self.driver_options.get(session_id, {}),
            )
            self.drivers[session_id] = driver
            return driver

        def native_factory(profile: Path, url: str):
            self.native_browser_launches.append((Path(profile), str(url)))
            return FakeNativeBrowserProcess()

        def attached_factory(profile: Path, _download: Path):
            self.attached_profiles.append(Path(profile))
            return factory(profile)

        def confirm_chrome_profile(_driver, **kwargs):
            self.chrome_profile_confirmations.append(len(self.chrome_profile_confirmations) + 1)
            return True

        values = {
            "profile_root": profiles,
            "checkpoint_path": self.root / "data" / "muse_sessions.json",
            "account_store": MuseAccountStore(self.root / "data" / "muse_accounts.json"),
            "driver_factory": factory,
            "native_browser_factory": native_factory,
            "attached_driver_factory": attached_factory,
            "chrome_profile_confirmer": confirm_chrome_profile,
            "poll_interval": 0.01,
            "stable_seconds": 0,
            "generation_timeout": 0.35,
            "login_timeout": 3.0,
            "retry_limit": 0,
            "backoff_base": 0,
        }
        values.update(overrides)
        return MuseSessionManager(**values)

    def _open_three(self):
        futures = [
            self.manager.open_session(session_id, f"owner{session_id}@example.com")
            for session_id in range(1, 4)
        ]
        for future in futures:
            future.result(timeout=2)

    def _wait_state(self, session_id: int, state: MuseSessionState, timeout: float = 2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.manager.snapshot(session_id).state == state:
                return
            time.sleep(0.01)
        self.fail(f"Muse {session_id} did not reach {state}")

    def test_three_sessions_use_three_fixed_isolated_profiles_and_threads(self):
        self._open_three()

        profiles = [Path(self.manager.snapshot(index).profile_dir) for index in range(1, 4)]
        self.assertEqual([path.name for path in profiles], ["account_1", "account_2", "account_3"])
        self.assertEqual(len(set(profiles)), 3)
        self.assertEqual(len({next(iter(driver.thread_ids)) for driver in self.drivers.values()}), 3)
        self.assertTrue(all(len(driver.thread_ids) == 1 for driver in self.drivers.values()))

    def test_send_all_runs_three_prompts_concurrently(self):
        self.barrier = threading.Barrier(3)
        self._open_three()

        results = self.manager.send_all({1: "one", 2: "two", 3: "three"}).result(timeout=3)

        self.assertEqual(results, {1: "result-1", 2: "result-2", 3: "result-3"})
        self.assertEqual(
            [self.manager.snapshot(index).state for index in range(1, 4)],
            [MuseSessionState.COMPLETED] * 3,
        )
        self.assertEqual([self.manager.snapshot(index).prompt for index in range(1, 4)], ["one", "two", "three"])

    def test_one_session_quota_error_does_not_stop_other_sessions(self):
        self.driver_options[2] = {"quota": True}
        self._open_three()

        results = self.manager.send_all({1: "one", 2: "two", 3: "three"}).result(timeout=3)

        self.assertEqual(results[1], "result-1")
        self.assertIsInstance(results[2], MuseSessionQuotaError)
        self.assertEqual(results[3], "result-3")
        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.COMPLETED)
        self.assertEqual(self.manager.snapshot(2).state, MuseSessionState.FAILED)
        self.assertEqual(self.manager.snapshot(3).state, MuseSessionState.COMPLETED)

    def test_login_required_keeps_chrome_open_and_auto_continues(self):
        self.driver_options[1] = {"login_required": True}
        future = self.manager.open_session(1, "owner@example.com")
        self._wait_state(1, MuseSessionState.LOGIN_REQUIRED)

        self.assertTrue(self.manager.snapshot(1).driver_open)
        self.drivers[1].complete_login()
        future.result(timeout=2)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.assertFalse(self.drivers[1].password.value)

    def test_manual_browser_login_never_types_google_credentials(self):
        self.driver_options[1] = {"login_required": True}

        future = self.manager.open_session(
            1,
            "owner@example.com",
            password="",
            manual_browser=True,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if (
                self.manager.snapshot(1).state == MuseSessionState.LOGIN_REQUIRED
                and 1 in self.drivers
            ):
                break
            time.sleep(0.01)
        else:
            self.fail("Manual browser did not attach its profile driver")
        driver = self.drivers[1]

        self.assertEqual(driver.password.value, "")
        self.assertEqual(driver.google_email, "")
        self.assertEqual(driver.requested_urls, [])
        self.assertEqual([item[0].name for item in self.native_browser_launches], ["account_1"])
        self.assertEqual([item.name for item in self.attached_profiles], ["account_1"])

        driver._current_url = "https://muse.ai/chat"
        driver.logged_in = True
        future.result(timeout=2)

        snapshot = self.manager.snapshot(1)
        self.assertEqual(snapshot.state, MuseSessionState.READY)
        self.assertTrue(snapshot.driver_open)

    def test_manual_browser_myaccount_page_continues_to_muse_without_closing_chrome(self):
        self.driver_options[1] = {"login_required": True}

        future = self.manager.open_session(
            1,
            "owner@example.com",
            password="",
            manual_browser=True,
        )
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            driver = self.drivers.get(1)
            if (
                driver is not None
                and self.manager.snapshot(1).state == MuseSessionState.LOGIN_REQUIRED
            ):
                break
            time.sleep(0.01)
        else:
            self.fail("Manual browser did not attach before the myaccount redirect test")
        driver.google_email = "owner@example.com"
        driver.google_authenticated = True
        driver._current_url = "https://myaccount.google.com/?pli=1"

        future.result(timeout=6)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.assertEqual(driver.current_url, "https://muse.ai/chat")
        self.assertFalse(driver.quit_called)

    def test_assisted_login_uses_its_own_webdriver_profile_and_opens_muse(self):
        self.driver_options[1] = {"login_required": True}

        future = self.manager.open_session(
            1,
            "owner@example.com",
            password="temporary-secret",
            manual_browser=True,
        )
        future.result(timeout=6)

        driver = self.drivers[1]
        snapshot = self.manager.snapshot(1)
        self.assertEqual(snapshot.state, MuseSessionState.READY)
        self.assertTrue(snapshot.driver_open)
        self.assertEqual(driver.google_email, "owner@example.com")
        self.assertTrue(driver.google_authenticated)
        self.assertEqual(driver.current_url, "https://muse.ai/chat")
        self.assertIn("temporary-secret", driver.password.value)
        self.assertEqual(self.native_browser_launches, [])
        self.assertEqual(self.attached_profiles, [])
        self.assertEqual(len(self.chrome_profile_confirmations), 1)

    def test_assisted_login_waits_for_manual_two_factor_then_continues(self):
        self.driver_options[1] = {"login_required": True, "google_two_factor": True}

        future = self.manager.open_session(
            1,
            "owner@example.com",
            password="temporary-secret",
            manual_browser=True,
        )
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            driver = self.drivers.get(1)
            if (
                driver is not None
                and "/challenge/totp" in driver.current_url
                and self.manager.snapshot(1).state == MuseSessionState.LOGIN_REQUIRED
            ):
                break
            time.sleep(0.01)
        else:
            self.fail("Assisted login did not reach the manual 2FA challenge")

        driver.complete_login()
        future.result(timeout=6)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.assertEqual(driver.current_url, "https://muse.ai/chat")

    def test_two_factor_youtube_channel_picker_is_bypassed_then_muse_opens(self):
        self.driver_options[1] = {
            "login_required": True,
            "google_two_factor": True,
            "youtube_channel_picker": True,
        }

        future = self.manager.open_session(
            1,
            "owner@example.com",
            password="temporary-secret",
            manual_browser=True,
        )
        self._wait_state(1, MuseSessionState.LOGIN_REQUIRED)
        driver = self.drivers[1]
        driver.complete_login()
        future.result(timeout=3)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.assertEqual(driver.current_url, "https://muse.ai/chat")
        self.assertTrue(any(value.startswith("https://muse.ai/") for value in driver.requested_urls))

    def test_google_myaccount_redirect_is_verified_then_muse_opens(self):
        self.driver_options[1] = {
            "login_required": True,
            "google_two_factor": True,
            "google_myaccount_redirect": True,
        }

        future = self.manager.open_session(
            1,
            "owner@example.com",
            password="temporary-secret",
            manual_browser=True,
        )
        self._wait_state(1, MuseSessionState.LOGIN_REQUIRED)
        driver = self.drivers[1]
        driver.complete_login()
        future.result(timeout=3)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.assertEqual(driver.current_url, "https://muse.ai/chat")
        self.assertTrue(any(value.startswith("https://muse.ai/") for value in driver.requested_urls))

    def test_discovers_and_binds_one_ready_muse_tab_per_profile(self):
        self._open_three()

        candidates = self.manager.discover_muse_tabs().result(timeout=2)

        self.assertEqual(len(candidates), 3)
        self.assertEqual({item.session_id for item in candidates}, {1, 2, 3})
        self.assertTrue(all(item.ready for item in candidates))
        selections = {item.session_id: item.handle for item in candidates}
        emails = {session_id: f"owner{session_id}@example.com" for session_id in range(1, 4)}
        navigation_counts = {session_id: len(driver.requested_urls) for session_id, driver in self.drivers.items()}

        self.manager.bind_existing_tabs(emails, selections).result(timeout=2)

        self.assertTrue(
            all(self.manager.snapshot(session_id).state == MuseSessionState.READY for session_id in range(1, 4))
        )
        self.assertEqual(
            navigation_counts,
            {session_id: len(driver.requested_urls) for session_id, driver in self.drivers.items()},
        )

    def test_existing_tab_binding_rejects_non_muse_hostname(self):
        self.manager.open_session(1, "owner@example.com").result(timeout=2)
        self.drivers[1]._current_url = "https://muse.ai.evil.invalid/chat"

        candidates = self.manager.discover_muse_tabs().result(timeout=2)

        self.assertEqual(candidates, ())
        with self.assertRaisesRegex(MuseSessionError, "hostname"):
            self.manager.bind_existing_tabs(
                {1: "one@example.com"},
                {1: "main"},
            ).result(timeout=2)

    def test_password_is_autofilled_once_and_never_written_to_checkpoint(self):
        self.driver_options[1] = {"login_required": True}
        secret = "Only-In-Memory-123!"

        self.manager.open_session(
            1,
            "owner@example.com",
            password=secret,
        ).result(timeout=2)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        expected_login_url = google_youtube_login_url("owner@example.com")
        self.assertEqual(self.drivers[1].requested_urls[0], expected_login_url)
        self.assertIn("/v3/signin/identifier?", expected_login_url)
        self.assertIn("Email=owner%40example.com", expected_login_url)
        self.assertIn("https://muse.ai/", self.drivers[1].requested_urls)
        self.assertEqual(self.drivers[1].password.value, secret + GOOGLE_ENTER_KEY)
        checkpoint = (self.root / "data" / "muse_sessions.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, checkpoint)
        self.assertNotIn(secret, repr(self.manager.snapshot(1)))

    def test_logs_into_google_first_then_clicks_muse_login_and_fills_identifier(self):
        self.driver_options[1] = {"login_required": True, "muse_identifier_login": True}
        secret = "Only-For-Google-123!"

        self.manager.open_session(
            1,
            "owner@example.com",
            password=secret,
        ).result(timeout=2)

        self.assertEqual(self.drivers[1].identifier.value, "owner@example.com")
        self.assertEqual(self.drivers[1].login_button.text, "Log in")
        self.assertEqual(self.drivers[1].continue_button.text, MUSE_CONTINUE_TEXT[0].title())
        self.assertEqual(self.drivers[1].password.value, secret + GOOGLE_ENTER_KEY)
        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)

    def test_unverified_google_manage_account_never_opens_muse(self):
        self.driver_options[1] = {"unverified_manage_account": True}

        future = self.manager.open_session(1, "owner@example.com")
        self._wait_state(1, MuseSessionState.LOGIN_REQUIRED)

        driver = self.drivers[1]
        self.assertTrue(self.manager.snapshot(1).driver_open)
        self.assertFalse(any(url.startswith("https://muse.ai/") for url in driver.requested_urls))
        self.assertIn("owner@example.com", self.manager.snapshot(1).status_message)
        self.manager.stop_session(1)
        future.result(timeout=2)

    def test_muse_page_load_timeout_is_accepted_after_expected_host_is_reached(self):
        self.driver_options[1] = {"muse_page_load_timeout": True}

        self.manager.open_session(1, "owner@example.com").result(timeout=2)

        snapshot = self.manager.snapshot(1)
        self.assertEqual(snapshot.state, MuseSessionState.READY)
        self.assertTrue(snapshot.driver_open)

    def test_open_all_sessions_logs_in_three_profiles_concurrently(self):
        self.login_barrier = threading.Barrier(3)
        credentials = {}
        secrets = []
        for session_id in range(1, 4):
            self.driver_options[session_id] = {"login_required": True}
            secret = f"temporary-{session_id}"
            secrets.append(secret)
            credentials[session_id] = (f"owner{session_id}@example.com", secret)

        results = self.manager.open_all_sessions(credentials).result(timeout=3)

        self.assertEqual(set(results), {1, 2, 3})
        self.assertTrue(
            all(self.manager.snapshot(session_id).state == MuseSessionState.READY for session_id in range(1, 4))
        )
        self.assertEqual(len({next(iter(driver.thread_ids)) for driver in self.drivers.values()}), 3)
        checkpoint = (self.root / "data" / "muse_sessions.json").read_text(encoding="utf-8")
        self.assertTrue(all(secret not in checkpoint for secret in secrets))

    def test_open_all_sessions_accepts_only_selected_accounts(self):
        credentials = {
            1: ("owner1@example.com", ""),
            2: ("owner2@example.com", ""),
        }

        results = self.manager.open_all_sessions(credentials).result(timeout=3)

        self.assertEqual(set(results), {1, 2})
        self.assertEqual(set(self.drivers), {1, 2})
        self.assertEqual(self.manager.snapshot(3).state, MuseSessionState.IDLE)

    def test_two_assisted_accounts_use_two_direct_isolated_profiles(self):
        self.driver_options[1] = {"login_required": True}
        self.driver_options[2] = {"login_required": True}
        credentials = {
            1: ("owner1@example.com", "secret-one"),
            2: ("owner2@example.com", "secret-two"),
        }

        results = self.manager.open_all_sessions(
            credentials,
            manual_browser=True,
        ).result(timeout=3)

        self.assertEqual(set(results), {1, 2})
        self.assertEqual({driver.profile.name for driver in self.drivers.values()}, {"account_1", "account_2"})
        self.assertIsNot(self.drivers[1], self.drivers[2])
        self.assertEqual(self.native_browser_launches, [])
        self.assertEqual(self.attached_profiles, [])
        self.assertTrue(all(self.manager.snapshot(index).driver_open for index in (1, 2)))

    def test_assisted_login_retries_transient_driver_error_without_closing_window(self):
        self.driver_options[1] = {
            "login_required": True,
            "transient_window_handle_errors": 1,
        }

        self.manager.open_session(
            1,
            "owner@example.com",
            password="google-secret",
            manual_browser=True,
        ).result(timeout=3)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.assertTrue(self.manager.snapshot(1).driver_open)
        self.assertFalse(self.drivers[1].quit_called)

    def test_muse_security_code_stays_manual_after_google_login(self):
        self.driver_options[1] = {
            "login_required": True,
            "muse_identifier_login": True,
            "muse_security_code": True,
        }
        future = self.manager.open_session(1, "owner@example.com", password="google-secret")
        self._wait_state(1, MuseSessionState.LOGIN_REQUIRED)

        self.assertEqual(self.drivers[1].password.value, "google-secret" + GOOGLE_ENTER_KEY)
        self.assertTrue(self.manager.snapshot(1).driver_open)
        self.drivers[1].complete_login()
        future.result(timeout=2)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)

    def test_google_password_is_sent_only_on_google(self):
        self.driver_options[1] = {"login_required": True}
        self.manager.open_session(1, "owner@example.com", password="google-secret").result(timeout=2)

        self.assertEqual(self.drivers[1].password.value, "google-secret" + GOOGLE_ENTER_KEY)
        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)

    def test_waitlist_account_fails_without_blocking_or_retrying_login(self):
        self.driver_options[1] = {"login_required": True, "waitlist": True}

        with self.assertRaises(MuseSessionError):
            self.manager.open_session(1, "owner@example.com", password="google-secret").result(timeout=2)

        snapshot = self.manager.snapshot(1)
        self.assertEqual(snapshot.state, MuseSessionState.FAILED)
        self.assertIn("waitlist", snapshot.error.casefold())
        self.assertEqual(self.drivers[1].cookies_cleared, 1)
        self.assertEqual(self.drivers[1].site_storage_cleared, 1)
        self.assertTrue(self.drivers[1].quit_called)

    def test_assisted_login_error_keeps_usable_browser_open(self):
        self.driver_options[1] = {"login_required": True, "waitlist": True}

        with self.assertRaises(MuseSessionError):
            self.manager.open_session(
                1,
                "owner@example.com",
                password="google-secret",
                manual_browser=True,
            ).result(timeout=5)

        snapshot = self.manager.snapshot(1)
        self.assertEqual(snapshot.state, MuseSessionState.FAILED)
        self.assertTrue(snapshot.driver_open)
        self.assertFalse(self.drivers[1].quit_called)

    def test_generation_timeout_is_local_to_one_session(self):
        self.driver_options[1] = {"never_respond": True}
        self._open_three()

        with self.assertRaises(MuseSessionError):
            self.manager.send_prompt(1, "timeout").result(timeout=2)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.FAILED)
        self.assertEqual(self.manager.snapshot(2).state, MuseSessionState.READY)
        self.assertEqual(self.manager.snapshot(3).state, MuseSessionState.READY)

    def test_stop_one_releases_task_without_clearing_prompt_or_result(self):
        self.driver_options[1] = {"never_respond": True}
        self._open_three()
        self.manager.sessions[1].result = "previous-result"
        future = self.manager.send_prompt(1, "keep-this-prompt")
        self._wait_state(1, MuseSessionState.GENERATING)

        self.assertTrue(self.manager.stop_session(1))
        future.result(timeout=2)
        snapshot = self.manager.snapshot(1)

        self.assertEqual(snapshot.state, MuseSessionState.READY)
        self.assertFalse(snapshot.task_running)
        self.assertEqual(snapshot.prompt, "keep-this-prompt")
        self.assertEqual(snapshot.result, "previous-result")
        self.assertEqual(self.manager.snapshot(2).state, MuseSessionState.READY)

    def test_stop_all_only_stops_muse_sessions(self):
        for session_id in range(1, 4):
            self.driver_options[session_id] = {"never_respond": True}
        self._open_three()
        unrelated_auto_registry_cancel = threading.Event()
        unrelated_youtube_cancel = threading.Event()
        future = self.manager.send_all({1: "one", 2: "two", 3: "three"})
        for session_id in range(1, 4):
            self._wait_state(session_id, MuseSessionState.GENERATING)

        self.assertEqual(self.manager.stop_all(), 3)
        future.result(timeout=2)

        self.assertFalse(unrelated_auto_registry_cancel.is_set())
        self.assertFalse(unrelated_youtube_cancel.is_set())
        self.assertTrue(all(self.manager.snapshot(index).state == MuseSessionState.READY for index in range(1, 4)))

    def test_reload_snapshots_do_not_create_duplicate_workers_or_drivers(self):
        self._open_three()
        session_objects = tuple(self.manager.sessions.values())
        with patch("auth.muse_sessions._manager_singleton", self.manager):
            first_client = get_muse_session_manager()
            second_client = get_muse_session_manager()

        self.assertEqual(len(self.drivers), 3)
        self.assertIs(first_client, second_client)
        self.assertIs(first_client, self.manager)
        self.assertIs(session_objects[0], self.manager.sessions[1])

    def test_duplicate_account_is_rejected_while_another_slot_uses_it(self):
        self.manager.open_session(1, "same@example.com").result(timeout=2)

        with self.assertRaises(ValueError):
            self.manager.open_session(2, "same@example.com").result(timeout=2)

        self.assertNotIn(2, self.drivers)

    def test_same_profile_directory_cannot_be_opened_by_second_manager(self):
        self.manager.open_session(1, "first@example.com").result(timeout=2)
        second = self._manager(
            checkpoint_path=self.root / "second_sessions.json",
            account_store=MuseAccountStore(self.root / "second_accounts.json"),
        )
        try:
            with self.assertRaises(MuseSessionError):
                second.open_session(1, "second@example.com").result(timeout=2)
            self.assertEqual(second.snapshot(1).state, MuseSessionState.FAILED)
        finally:
            second.shutdown(timeout=2)

    def test_dead_driver_is_failed_and_can_restart_only_that_session(self):
        self._open_three()
        original = self.drivers[2]
        original.closed = True

        with self.assertRaises(MuseSessionError):
            self.manager.send_prompt(2, "will fail").result(timeout=2)

        self.assertEqual(self.manager.snapshot(2).state, MuseSessionState.FAILED)
        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.manager.open_session(2, "owner2@example.com", force_relogin=True).result(timeout=2)
        self.assertEqual(self.manager.snapshot(2).state, MuseSessionState.READY)
        self.assertIsNot(self.drivers[2], original)

    def test_shutdown_quits_all_open_drivers_without_removing_profiles(self):
        self._open_three()
        profiles = [driver.profile for driver in self.drivers.values()]

        self.manager.shutdown(timeout=2)

        self.assertTrue(all(driver.quit_called for driver in self.drivers.values()))
        self.assertTrue(all(path.is_dir() for path in profiles))

    def test_checkpoint_contains_no_browser_secrets(self):
        self.manager.open_session(1, "owner@example.com").result(timeout=2)
        checkpoint = (self.root / "data" / "muse_sessions.json").read_text(encoding="utf-8").casefold()

        self.assertNotIn("cookie", checkpoint)
        self.assertNotIn("token", checkpoint)
        self.assertNotIn("password", checkpoint)
        self.assertIn("owner@example.com", checkpoint)


if __name__ == "__main__":
    unittest.main()
