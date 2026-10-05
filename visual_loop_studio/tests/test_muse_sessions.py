from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auth.muse_login import (
    CLICKABLE_SELECTOR,
    GOOGLE_ENTER_KEY,
    GOOGLE_MUSE_LOGIN_URL,
    GOOGLE_PASSWORD_SELECTORS,
    MUSE_APP_SELECTORS,
    MUSE_CONTINUE_TEXT,
    MUSE_IDENTIFIER_SELECTORS,
    MUSE_PASSWORD_SELECTORS,
    MUSE_SECURITY_SELECTORS,
    MuseAccountStore,
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
        self.google_authenticated = False
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
        if self.closed:
            raise RuntimeError("driver died")
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
            self._current_url = (
                "https://accounts.google.com/ManageAccount"
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
            if self.waitlist:
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
                self.google_authenticated = True
                self._current_url = "https://accounts.google.com/ManageAccount"
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
        self.logged_in = True
        self.muse_stage = ""
        self._current_url = "https://muse.ai/chat"

    def quit(self):
        self._record_thread()
        self.quit_called = True
        self.closed = True


class MuseSessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.drivers: dict[int, FakeDriver] = {}
        self.barrier: threading.Barrier | None = None
        self.login_barrier: threading.Barrier | None = None
        self.driver_options: dict[int, dict] = {}
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

        values = {
            "profile_root": profiles,
            "checkpoint_path": self.root / "data" / "muse_sessions.json",
            "account_store": MuseAccountStore(self.root / "data" / "muse_accounts.json"),
            "driver_factory": factory,
            "poll_interval": 0.01,
            "stable_seconds": 0,
            "generation_timeout": 0.35,
            "login_timeout": 0.25,
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

    def test_password_is_autofilled_once_and_never_written_to_checkpoint(self):
        self.driver_options[1] = {"login_required": True}
        secret = "Only-In-Memory-123!"

        self.manager.open_session(
            1,
            "owner@example.com",
            password=secret,
        ).result(timeout=2)

        self.assertEqual(self.manager.snapshot(1).state, MuseSessionState.READY)
        self.assertEqual(self.drivers[1].requested_urls[0], GOOGLE_MUSE_LOGIN_URL)
        self.assertIn("accounts.google.com%2FManageAccount", GOOGLE_MUSE_LOGIN_URL)
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
