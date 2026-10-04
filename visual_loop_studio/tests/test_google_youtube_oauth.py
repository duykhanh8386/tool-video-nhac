from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from auth.google_youtube import (
    AUTHORIZE_URL,
    TOKEN_URL,
    GoogleDesktopClientConfig,
    GoogleYouTubeAuthService,
    TokenRevokedError,
    WrongChannelError,
    YouTubeAccountStore,
    build_authorization_url,
)
from models.ai_audit import AiAuditLog


CHANNEL_ID = "UC_OWNED_CHANNEL"


class MemorySecrets:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str:
        return self.values.get(name, "")

    def set(self, name: str, value: str) -> bool:
        self.values[name] = value
        return True

    def delete(self, name: str) -> bool:
        self.values.pop(name, None)
        return True


class FakeGoogle:
    def __init__(self, channel_id: str = CHANNEL_ID):
        self.channel_id = channel_id
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.refresh_payload: dict = {"access_token": "access-refreshed", "expires_in": 3600}

    def request(self, method: str, url: str, form: dict | None, headers: dict) -> dict:
        self.calls.append((method, url, form, headers))
        if "youtube/v3/channels" in url:
            return {
                "items": [
                    {"id": self.channel_id, "snippet": {"title": "Owned Channel"}},
                ]
            }
        if "revoke" in url:
            return {}
        if "token" in url:
            if form and form.get("grant_type") == "authorization_code":
                return {"access_token": "access-first", "refresh_token": "refresh-first", "expires_in": 3600}
            return self.refresh_payload
        raise AssertionError(f"Unexpected URL: {url}")


class GoogleYouTubeOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = YouTubeAccountStore(self.root / "accounts.json")
        self.secrets = MemorySecrets()
        self.google = FakeGoogle()
        self.config = GoogleDesktopClientConfig("desktop-client", "desktop-secret")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self, authorizer=None, browser_opener=None) -> GoogleYouTubeAuthService:
        kwargs = {"authorizer": authorizer} if authorizer else {}
        if browser_opener:
            kwargs["browser_opener"] = browser_opener
        return GoogleYouTubeAuthService(
            self.config,
            store=self.store,
            secret_getter=self.secrets.get,
            secret_setter=self.secrets.set,
            secret_deleter=self.secrets.delete,
            requester=self.google.request,
            audit_log=AiAuditLog(self.root / "audit.jsonl"),
            clock=lambda: 1_000.0,
            **kwargs,
        )

    @staticmethod
    def authorizer(refresh_token: str = "refresh-first"):
        def authorize(config, email_hint, cancelled):
            if cancelled():
                raise AssertionError("Unexpected cancellation")
            return {"access_token": "access-first", "refresh_token": refresh_token, "expires_in": 3600}

        return authorize

    def test_first_login_validates_channel_and_stores_only_refresh_token_in_credential_store(self) -> None:
        opened_urls: list[str] = []

        def open_browser(url: str, **_kwargs) -> bool:
            opened_urls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            callback_url = query["redirect_uri"][0] + "?" + urllib.parse.urlencode(
                {"code": "authorization-code", "state": query["state"][0]}
            )

            def send_callback() -> None:
                with urllib.request.urlopen(callback_url, timeout=5) as response:
                    response.read()

            threading.Thread(target=send_callback, daemon=True).start()
            return True

        account = self.service(browser_opener=open_browser).authenticate("owner@example.com", CHANNEL_ID)

        self.assertEqual(account.channel_id, CHANNEL_ID)
        self.assertEqual(account.email_label, "owner@example.com")
        credential = json.loads(self.secrets.values[account.credential_name])
        self.assertEqual(credential["refresh_token"], "refresh-first")
        metadata = (self.root / "accounts.json").read_text(encoding="utf-8")
        self.assertNotIn("refresh-first", metadata)
        self.assertNotIn("access-first", metadata)
        self.assertNotIn("refresh-first", (self.root / "audit.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(len(opened_urls), 1)
        redirect_uri = urllib.parse.parse_qs(urllib.parse.urlparse(opened_urls[0]).query)["redirect_uri"][0]
        self.assertTrue(redirect_uri.startswith("http://127.0.0.1:"))
        channel_call = next(call for call in self.google.calls if "youtube/v3/channels" in call[1])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(channel_call[1]).query)
        self.assertEqual(query["mine"], ["true"])

    def test_access_token_is_refreshed_without_interactive_login(self) -> None:
        account = self.service(self.authorizer()).authenticate("owner@example.com", CHANNEL_ID)
        fresh_service = self.service(authorizer=lambda *_args: self.fail("Interactive OAuth must not run"))

        token = fresh_service.get_access_token(account.account_id)

        self.assertEqual(token, "access-refreshed")
        refresh_call = next(call for call in self.google.calls if call[2] and call[2].get("grant_type") == "refresh_token")
        self.assertEqual(refresh_call[2]["refresh_token"], "refresh-first")

    def test_revoked_refresh_token_marks_account_for_relogin_and_removes_secret(self) -> None:
        account = self.service(self.authorizer()).authenticate("owner@example.com", CHANNEL_ID)
        self.google.refresh_payload = {"error": "invalid_grant"}
        fresh_service = self.service()

        with self.assertRaises(TokenRevokedError):
            fresh_service.get_access_token(account.account_id)

        self.assertNotIn(account.credential_name, self.secrets.values)
        self.assertTrue(self.store.get(account.account_id).needs_reauth)

    def test_wrong_selected_channel_revokes_new_token_and_does_not_save_account(self) -> None:
        with self.assertRaises(WrongChannelError):
            self.service(self.authorizer()).authenticate("owner@example.com", "UC_SOMEONE_ELSE")

        self.assertEqual(self.store.all(), [])
        self.assertEqual(self.secrets.values, {})
        revoke_call = next(call for call in self.google.calls if "revoke" in call[1])
        self.assertEqual(revoke_call[2]["token"], "refresh-first")

    def test_relogin_reuses_account_and_replaces_refresh_token(self) -> None:
        first = self.service(self.authorizer()).authenticate("owner@example.com", CHANNEL_ID)
        relogged = self.service(self.authorizer("refresh-second")).relogin(first.account_id)

        self.assertEqual(relogged.account_id, first.account_id)
        self.assertEqual(len(self.store.all()), 1)
        credential = json.loads(self.secrets.values[first.credential_name])
        self.assertEqual(credential["refresh_token"], "refresh-second")
        self.assertFalse(relogged.needs_reauth)

    def test_disconnect_revokes_refresh_token_and_deletes_local_account(self) -> None:
        account = self.service(self.authorizer()).authenticate("owner@example.com", CHANNEL_ID)

        self.service().disconnect(account.account_id)

        self.assertIsNone(self.store.get(account.account_id))
        self.assertNotIn(account.credential_name, self.secrets.values)
        revoke_call = next(call for call in self.google.calls if "revoke" in call[1])
        self.assertEqual(revoke_call[2]["token"], "refresh-first")

    def test_authorization_url_uses_pkce_offline_access_and_minimum_scope(self) -> None:
        url = build_authorization_url(
            self.config,
            redirect_uri="http://127.0.0.1:54321/oauth2/callback",
            state="random-state",
            code_challenge="pkce-challenge",
            email_hint="owner@example.com",
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], ["pkce-challenge"])
        self.assertEqual(query["state"], ["random-state"])
        self.assertEqual(query["login_hint"], ["owner@example.com"])
        self.assertEqual(query["scope"], ["https://www.googleapis.com/auth/youtube.readonly"])
        self.assertTrue(query["redirect_uri"][0].startswith("http://127.0.0.1:"))

    def test_client_file_cannot_replace_google_oauth_endpoints(self) -> None:
        path = self.root / "client.json"
        path.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "desktop-client",
                        "client_secret": "desktop-secret",
                        "auth_uri": "https://attacker.invalid/auth",
                        "token_uri": "https://attacker.invalid/token",
                    }
                }
            ),
            encoding="utf-8",
        )

        config = GoogleDesktopClientConfig.from_file(path)

        self.assertEqual(config.auth_uri, AUTHORIZE_URL)
        self.assertEqual(config.token_uri, TOKEN_URL)


if __name__ == "__main__":
    unittest.main()
