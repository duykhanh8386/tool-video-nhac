from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auth.muse_login import (
    CLICKABLE_SELECTOR,
    GOOGLE_EMAIL_NEXT_SELECTORS,
    GOOGLE_EMAIL_SELECTORS,
    GOOGLE_ENTER_KEY,
    MUSE_APP_SELECTORS,
    MUSE_IDENTIFIER_SELECTORS,
    MUSE_SECURITY_SELECTORS,
    MuseAccountStore,
    MuseLoginCancelled,
    MuseLoginService,
    MuseLoginTimeout,
    MuseUnsafeNavigationError,
    muse_native_browser_command,
)


class FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        attrs=None,
        on_click=None,
        on_send=None,
        visible: bool = True,
        enabled: bool = True,
    ):
        self.text = text
        self.attrs = attrs or {}
        self.on_click = on_click
        self.on_send = on_send
        self.visible = visible
        self.enabled = enabled
        self.clicks = 0
        self.value = ""

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        return self.attrs.get(name, "")

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    def clear(self):
        self.value = ""

    def send_keys(self, *values):
        self.value += "".join(str(value) for value in values)
        if self.on_send:
            self.on_send(*values)


class FakePage:
    def __init__(self, url: str, *, logged_in=False, clickables=None, accounts=None, security=None, body_text=""):
        self.url = url
        self.logged_in = logged_in
        self.clickables = clickables or []
        self.accounts = accounts or []
        self.security = security or {}
        self.body = FakeElement(body_text)


class FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle not in self.driver.pages:
            raise RuntimeError("closed")
        self.driver.current = handle


class FakeDriver:
    def __init__(self, page: FakePage, *, unrelated: FakePage | None = None):
        self.pages = {"task": page}
        if unrelated:
            self.pages["unrelated"] = unrelated
        self.current = "task"
        self.switch_to = FakeSwitchTo(self)
        self.closed: list[str] = []
        self.quit_called = False
        self.requested_url = ""

    @property
    def window_handles(self):
        return list(self.pages)

    @property
    def current_window_handle(self):
        return self.current

    @property
    def current_url(self):
        return self.pages[self.current].url

    def set_page_load_timeout(self, _seconds):
        pass

    def get(self, url):
        self.requested_url = url

    def find_elements(self, by, selector):
        page = self.pages[self.current]
        if by == "css selector" and selector == CLICKABLE_SELECTOR:
            return page.clickables
        if by == "css selector" and selector.startswith("[data-identifier]"):
            return page.accounts
        if by == "css selector" and selector in MUSE_APP_SELECTORS:
            return [FakeElement("Muse app")] if page.logged_in else []
        if by == "css selector" and selector in page.security:
            return page.security[selector]
        if by == "tag name" and selector == "body":
            return [page.body]
        return []

    def add_popup(self, handle: str, page: FakePage):
        self.pages[handle] = page

    def close(self):
        handle = self.current
        self.closed.append(handle)
        self.pages.pop(handle, None)
        self.current = next(iter(self.pages), "")

    def quit(self):
        self.quit_called = True


class MuseLoginServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MuseAccountStore(Path(self.temp.name) / "muse_accounts.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_native_login_browser_uses_real_profile_without_automation_evasion_flags(self):
        executable = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        profile = Path(self.temp.name) / "profile"

        command = muse_native_browser_command(executable, profile, "https://muse.ai/")

        self.assertEqual(command[0], str(executable))
        self.assertIn(f"--user-data-dir={profile.resolve()}", command)
        self.assertEqual(command[-1], "https://muse.ai/")
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertFalse(any("user-agent" in value.casefold() for value in command))
        self.assertFalse(any("disable-blink-features" in value.casefold() for value in command))

    def service(self, driver: FakeDriver, **kwargs) -> MuseLoginService:
        return MuseLoginService(
            store=self.store,
            driver_factory=lambda _profile: driver,
            sleeper=lambda _seconds: None,
            poll_interval=0.05,
            **kwargs,
        )

    def test_muse_already_logged_in_reuses_existing_profile_session(self):
        driver = FakeDriver(FakePage("https://muse.ai/chat", logged_in=True))

        result = self.service(driver).login("owner@example.com")

        self.assertTrue(result.reused_session)
        self.assertEqual(result.account.status, "connected")
        self.assertEqual(driver.requested_url, "https://muse.ai/")

    def test_clicks_muse_login_then_sign_in_with_google(self):
        driver = FakeDriver(FakePage("https://muse.ai/", logged_in=False))
        login = FakeElement("Log in")
        google = FakeElement("Continue with Google")
        account = FakeElement("Owner\nowner@example.com", attrs={"data-identifier": "owner@example.com"})

        def click_login():
            driver.pages["task"] = FakePage("https://auth.muse.ai/runtime/path", clickables=[google])

        def click_google():
            driver.add_popup(
                "google-popup",
                FakePage("https://accounts.google.com/o/oauth2/auth", accounts=[account]),
            )

        def choose_account():
            driver.pages["google-popup"] = FakePage("https://muse.ai/chat", logged_in=True)

        login.on_click = click_login
        google.on_click = click_google
        account.on_click = choose_account
        driver.pages["task"].clickables = [login]

        result = self.service(driver).login("owner@example.com")

        self.assertFalse(result.reused_session)
        self.assertEqual(login.clicks, 1)
        self.assertEqual(google.clicks, 1)
        self.assertEqual(account.clicks, 1)
        self.assertIn("google-popup", driver.closed)

    def test_fills_new_muse_identifier_form_without_submitting_a_password(self):
        identifier = FakeElement(attrs={"aria-label": "Mobile number or email"})
        driver = FakeDriver(
            FakePage(
                "https://muse.ai/?aymh_complete=1",
                security={MUSE_IDENTIFIER_SELECTORS[0]: [identifier]},
            )
        )

        self.assertTrue(self.service(driver)._fill_muse_identifier(driver, "Owner@Example.com"))

        self.assertEqual(identifier.value, "owner@example.com")
        self.assertNotIn(GOOGLE_ENTER_KEY, identifier.value)

    def test_never_fills_muse_identifier_on_lookalike_host(self):
        identifier = FakeElement(attrs={"aria-label": "Mobile number or email"})
        driver = FakeDriver(
            FakePage(
                "https://muse.ai.evil.invalid/",
                security={MUSE_IDENTIFIER_SELECTORS[0]: [identifier]},
            )
        )

        with self.assertRaises(MuseUnsafeNavigationError):
            self.service(driver)._fill_muse_identifier(driver, "owner@example.com")

        self.assertEqual(identifier.value, "")

    def test_muse_security_code_always_stays_manual(self):
        security_code = FakeElement(attrs={"autocomplete": "one-time-code"})
        driver = FakeDriver(
            FakePage(
                "https://muse.ai/",
                security={MUSE_SECURITY_SELECTORS[1]: [security_code]},
                body_text="Enter the 6-digit security code",
            )
        )

        self.assertTrue(self.service(driver)._requires_manual_muse_step(driver))

    def test_selects_exact_google_account_from_existing_profile(self):
        driver = FakeDriver(FakePage("https://accounts.google.com/o/oauth2/auth"))
        wrong = FakeElement("Other\nother@example.com", attrs={"data-identifier": "other@example.com"})
        right = FakeElement("Owner\nowner@example.com", attrs={"data-identifier": "owner@example.com"})

        def choose_right():
            driver.pages["task"] = FakePage("https://muse.ai/chat", logged_in=True)

        right.on_click = choose_right
        driver.pages["task"].accounts = [wrong, right]

        self.service(driver).login("owner@example.com")

        self.assertEqual(wrong.clicks, 0)
        self.assertEqual(right.clicks, 1)

    def test_security_challenge_waits_for_user_without_clicking_password(self):
        password = FakeElement(attrs={"type": "password"})
        driver = FakeDriver(
            FakePage(
                "https://accounts.google.com/signin/v2/challenge/pwd",
                security={"input[type='password']": [password]},
                body_text="2-Step Verification",
            )
        )
        events: list[str] = []

        def status(state, _message):
            events.append(state)
            if state == "manual_required":
                driver.pages["task"] = FakePage("https://muse.ai/chat", logged_in=True)

        result = self.service(driver).login("owner@example.com", status=status)

        self.assertEqual(result.account.status, "connected")
        self.assertIn("manual_required", events)
        self.assertEqual(password.clicks, 0)

    def test_google_password_can_be_filled_but_two_factor_stays_manual(self):
        password = FakeElement(attrs={"type": "password", "name": "Passwd"})
        driver = FakeDriver(
            FakePage(
                "https://accounts.google.com/signin/v2/challenge/pwd",
                security={"input[name='Passwd']": [password]},
                body_text="Enter your password",
            )
        )
        service = self.service(driver)
        secret = bytearray(b"secret-value")

        self.assertFalse(
            service._requires_manual_google_step(
                driver,
                allow_email=True,
                allow_password=True,
            )
        )
        self.assertTrue(service._fill_google_password(driver, secret))
        self.assertEqual(password.value, "secret-value" + GOOGLE_ENTER_KEY)

        driver.pages["task"].body.text = "2-Step Verification - check your phone"
        self.assertTrue(
            service._requires_manual_google_step(
                driver,
                allow_email=True,
                allow_password=True,
            )
        )

    def test_google_email_clicks_identifier_next_instead_of_relying_on_enter(self):
        email = FakeElement(attrs={"type": "email"})
        next_button = FakeElement("Next")
        driver = FakeDriver(
            FakePage(
                "https://accounts.google.com/v3/signin/identifier",
                security={
                    GOOGLE_EMAIL_SELECTORS[0]: [email],
                    GOOGLE_EMAIL_NEXT_SELECTORS[0]: [next_button],
                },
            )
        )

        self.assertTrue(self.service(driver)._fill_google_email(driver, "Owner@Example.com"))
        self.assertEqual(email.value, "owner@example.com")
        self.assertNotIn(GOOGLE_ENTER_KEY, email.value)
        self.assertEqual(next_button.clicks, 1)

    def test_google_insecure_browser_block_is_detected(self):
        driver = FakeDriver(
            FakePage(
                "https://accounts.google.com/signin/rejected",
                body_text="Couldn't sign you in. This browser or app may not be secure.",
            )
        )

        self.assertTrue(self.service(driver)._google_automation_blocked(driver))

    def test_google_continue_callback_is_verified_on_muse_main_ui(self):
        driver = FakeDriver(FakePage("https://accounts.google.com/o/oauth2/approval"))
        allow = FakeElement("Continue")

        def complete_callback():
            driver.pages["task"] = FakePage("https://muse.ai/chat", logged_in=True)

        allow.on_click = complete_callback
        driver.pages["task"].clickables = [allow]

        result = self.service(driver).login("owner@example.com")

        self.assertEqual(allow.clicks, 1)
        self.assertEqual(result.account.status, "connected")

    def test_timeout_closes_only_task_tab_and_leaves_unrelated_tab(self):
        driver = FakeDriver(
            FakePage("https://auth.muse.ai/runtime/path"),
            unrelated=FakePage("https://muse.ai/unrelated", logged_in=True),
        )
        ticks = iter((0.0, 0.0, 2.0, 3.0))
        service = self.service(driver, clock=lambda: next(ticks))

        with self.assertRaises(MuseLoginTimeout):
            service.login("owner@example.com", timeout_seconds=1)

        self.assertIn("task", driver.closed)
        self.assertNotIn("unrelated", driver.closed)
        self.assertFalse(driver.quit_called)
        self.assertEqual(self.store.get_by_email("owner@example.com").status, "timeout")

    def test_cancel_stops_and_closes_task_tab(self):
        driver = FakeDriver(FakePage("https://auth.muse.ai/runtime/path"))

        with self.assertRaises(MuseLoginCancelled):
            self.service(driver).login("owner@example.com", cancelled=lambda: True)

        self.assertEqual(driver.closed, ["task"])
        self.assertTrue(driver.quit_called)
        self.assertEqual(self.store.get_by_email("owner@example.com").status, "cancelled")

    def test_rejects_navigation_outside_exact_allowlist(self):
        driver = FakeDriver(FakePage("https://accounts.google.com.evil.invalid/signin"))

        with self.assertRaises(MuseUnsafeNavigationError):
            self.service(driver).login("owner@example.com")

    def test_each_email_has_a_different_fixed_profile(self):
        first = self.store.ensure("first@example.com")
        second = self.store.ensure("second@example.com")

        self.assertNotEqual(first.profile_dir, second.profile_dir)
        self.assertEqual(first.profile_dir, self.store.ensure("FIRST@example.com").profile_dir)


if __name__ == "__main__":
    unittest.main()
