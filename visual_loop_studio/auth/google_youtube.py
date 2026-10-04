from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from models.ai_audit import AiAuditLog
from utils.config import read_json, write_json
from utils.paths import DATA_DIR
from utils.secret_store import delete_secret, get_secret, set_secret


AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_SCOPES = (YOUTUBE_READONLY_SCOPE,)
DEFAULT_ACCOUNTS_FILE = DATA_DIR / "youtube_accounts.json"
CALLBACK_PATH = "/oauth2/callback"
OAUTH_TIMEOUT_SECONDS = 5 * 60


class GoogleYouTubeAuthError(RuntimeError):
    """A safe, user-facing Google/YouTube authentication error."""


class OAuthCancelled(GoogleYouTubeAuthError):
    pass


class TokenRevokedError(GoogleYouTubeAuthError):
    pass


class WrongChannelError(GoogleYouTubeAuthError):
    pass


class ChannelSelectionRequired(GoogleYouTubeAuthError):
    def __init__(self, channels: list["YouTubeChannel"]):
        self.channels = channels
        choices = ", ".join(f"{item.title} ({item.channel_id})" for item in channels)
        super().__init__(f"Tài khoản có nhiều kênh. Hãy nhập Channel ID cần dùng: {choices}")


class _ProviderError(Exception):
    def __init__(self, code: str = "", status: int = 0):
        super().__init__(code)
        self.code = str(code or "")
        self.status = int(status or 0)


@dataclass(frozen=True)
class GoogleDesktopClientConfig:
    client_id: str
    client_secret: str = ""
    auth_uri: str = AUTHORIZE_URL
    token_uri: str = TOKEN_URL

    @classmethod
    def from_file(cls, path: str | Path) -> "GoogleDesktopClientConfig":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise GoogleYouTubeAuthError("Không đọc được file OAuth client JSON của Google.") from exc
        installed = value.get("installed") if isinstance(value, dict) else None
        if not isinstance(installed, dict) or not str(installed.get("client_id") or "").strip():
            raise GoogleYouTubeAuthError(
                "File OAuth không phải loại Desktop app. Hãy tải client JSON có mục 'installed' từ Google Cloud Console."
            )
        return cls(
            client_id=str(installed["client_id"]).strip(),
            client_secret=str(installed.get("client_secret") or "").strip(),
            # Desktop client files identify the app; endpoints stay pinned to
            # Google so a substituted JSON file cannot redirect credentials.
            auth_uri=AUTHORIZE_URL,
            token_uri=TOKEN_URL,
        )


@dataclass(frozen=True)
class YouTubeChannel:
    channel_id: str
    title: str


@dataclass
class YouTubeAccount:
    account_id: str
    email_label: str
    channel_id: str
    channel_title: str
    credential_name: str
    chrome_profile_dir: str
    connected_at: str
    updated_at: str
    needs_reauth: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "YouTubeAccount":
        return cls(
            account_id=str(value.get("account_id") or ""),
            email_label=str(value.get("email_label") or ""),
            channel_id=str(value.get("channel_id") or ""),
            channel_title=str(value.get("channel_title") or ""),
            credential_name=str(value.get("credential_name") or ""),
            chrome_profile_dir=str(value.get("chrome_profile_dir") or ""),
            connected_at=str(value.get("connected_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            needs_reauth=bool(value.get("needs_reauth", False)),
        )


class YouTubeAccountStore:
    """Persist non-secret account metadata. OAuth tokens never enter this file."""

    def __init__(self, path: str | Path = DEFAULT_ACCOUNTS_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()

    def all(self) -> list[YouTubeAccount]:
        with self._lock:
            raw = read_json(self.path, {"accounts": []}) or {}
            values = raw.get("accounts", []) if isinstance(raw, dict) else []
            return [YouTubeAccount.from_dict(item) for item in values if isinstance(item, dict)]

    def get(self, account_id: str) -> YouTubeAccount | None:
        return next((item for item in self.all() if item.account_id == account_id), None)

    def save(self, account: YouTubeAccount) -> None:
        with self._lock:
            accounts = self.all()
            accounts = [item for item in accounts if item.account_id != account.account_id]
            accounts.append(account)
            accounts.sort(key=lambda item: (item.email_label.casefold(), item.channel_title.casefold()))
            write_json(self.path, {"version": 1, "accounts": [asdict(item) for item in accounts]})

    def remove(self, account_id: str) -> None:
        with self._lock:
            accounts = [item for item in self.all() if item.account_id != account_id]
            write_json(self.path, {"version": 1, "accounts": [asdict(item) for item in accounts]})


RequestCallable = Callable[[str, str, dict[str, str] | None, dict[str, str]], dict[str, Any]]
AuthorizationCallable = Callable[[GoogleDesktopClientConfig, str, Callable[[], bool]], dict[str, Any]]


class GoogleYouTubeAuthService:
    def __init__(
        self,
        client_config: GoogleDesktopClientConfig,
        *,
        store: YouTubeAccountStore | None = None,
        secret_getter: Callable[[str], str] = get_secret,
        secret_setter: Callable[[str, str], bool] = set_secret,
        secret_deleter: Callable[[str], bool] = delete_secret,
        requester: RequestCallable | None = None,
        authorizer: AuthorizationCallable | None = None,
        browser_opener: Callable[..., Any] = webbrowser.open,
        clock: Callable[[], float] = time.time,
        audit_log: AiAuditLog | None = None,
    ):
        self.client_config = client_config
        self.store = store or YouTubeAccountStore()
        self._secret_getter = secret_getter
        self._secret_setter = secret_setter
        self._secret_deleter = secret_deleter
        self._requester = requester or _request_json
        self._authorizer = authorizer
        self._browser_opener = browser_opener
        self._clock = clock
        self._access_cache: dict[str, tuple[str, float]] = {}
        self._audit = audit_log or AiAuditLog()

    def authenticate(
        self,
        email_hint: str,
        expected_channel_id: str = "",
        *,
        relogin_account_id: str = "",
        cancelled: Callable[[], bool] | None = None,
    ) -> YouTubeAccount:
        cancelled = cancelled or (lambda: False)
        email_hint = str(email_hint or "").strip()
        expected_channel_id = str(expected_channel_id or "").strip()
        if cancelled():
            raise OAuthCancelled("Đã hủy đăng nhập Google.")
        token = (
            self._authorizer(self.client_config, email_hint, cancelled)
            if self._authorizer
            else self._interactive_authorization(email_hint, cancelled)
        )
        access_token = str(token.get("access_token") or "")
        refresh_token = str(token.get("refresh_token") or "")
        if not access_token:
            raise GoogleYouTubeAuthError("Google không trả về access token hợp lệ.")
        if not refresh_token and relogin_account_id:
            existing = self.store.get(relogin_account_id)
            if existing:
                refresh_token = str(self._load_credential(existing).get("refresh_token") or "")
        if not refresh_token:
            self._revoke_token(access_token, ignore_errors=True)
            raise GoogleYouTubeAuthError(
                "Google không cấp refresh token. Hãy thu hồi quyền ứng dụng trong tài khoản Google rồi đăng nhập lại."
            )
        try:
            channels = self._list_channels(access_token)
        except GoogleYouTubeAuthError:
            self._revoke_token(refresh_token, ignore_errors=True)
            raise
        try:
            selected = self._select_channel(channels, expected_channel_id)
        except (WrongChannelError, ChannelSelectionRequired):
            self._revoke_token(refresh_token, ignore_errors=True)
            raise
        account_id = relogin_account_id or _account_id(selected.channel_id)
        previous = self.store.get(account_id)
        if previous and previous.channel_id != selected.channel_id:
            self._revoke_token(refresh_token, ignore_errors=True)
            raise WrongChannelError("Kênh Google vừa xác thực không khớp với tài khoản đã chọn.")
        credential_name = previous.credential_name if previous else f"youtube_oauth_{account_id}"
        credential = {
            "refresh_token": refresh_token,
            "client_id": self.client_config.client_id,
            "client_secret": self.client_config.client_secret,
            "token_uri": self.client_config.token_uri,
        }
        if not self._secret_setter(credential_name, json.dumps(credential, separators=(",", ":"))):
            self._revoke_token(refresh_token, ignore_errors=True)
            raise GoogleYouTubeAuthError(
                "Không thể lưu refresh token an toàn bằng Windows Credential Manager. Tài khoản chưa được kết nối."
            )
        now = _utc_now()
        from auth.youtube_studio import youtube_studio_profile_dir

        account = YouTubeAccount(
            account_id=account_id,
            email_label=email_hint or (previous.email_label if previous else selected.title),
            channel_id=selected.channel_id,
            channel_title=selected.title,
            credential_name=credential_name,
            chrome_profile_dir=str(youtube_studio_profile_dir(account_id)),
            connected_at=previous.connected_at if previous else now,
            updated_at=now,
            needs_reauth=False,
        )
        self.store.save(account)
        expires_in = max(0, int(token.get("expires_in") or 3600))
        self._access_cache[account_id] = (access_token, self._clock() + expires_in)
        self._record("youtube.oauth_relogin" if previous else "youtube.oauth_connect", account)
        return account

    def relogin(self, account_id: str, *, cancelled: Callable[[], bool] | None = None) -> YouTubeAccount:
        account = self._require_account(account_id)
        return self.authenticate(
            account.email_label,
            account.channel_id,
            relogin_account_id=account.account_id,
            cancelled=cancelled,
        )

    def get_access_token(self, account_id: str) -> str:
        account = self._require_account(account_id)
        cached = self._access_cache.get(account_id)
        if cached and cached[1] - 60 > self._clock():
            return cached[0]
        credential = self._load_credential(account)
        refresh_token = str(credential.get("refresh_token") or "")
        if not refresh_token:
            self._mark_reauth(account)
            raise TokenRevokedError("Refresh token không còn tồn tại. Hãy đăng nhập lại tài khoản Google.")
        form = {
            "client_id": str(credential.get("client_id") or self.client_config.client_id),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        client_secret = str(credential.get("client_secret") or "")
        if client_secret:
            form["client_secret"] = client_secret
        try:
            payload = self._requester(
                "POST",
                str(credential.get("token_uri") or self.client_config.token_uri or TOKEN_URL),
                form,
                {},
            )
            _raise_payload_error(payload)
        except _ProviderError as exc:
            if exc.code in {"invalid_grant", "invalid_token", "unauthorized_client"}:
                self._secret_deleter(account.credential_name)
                self._mark_reauth(account)
                raise TokenRevokedError("Quyền Google đã hết hiệu lực hoặc bị thu hồi. Hãy đăng nhập lại.") from None
            raise GoogleYouTubeAuthError("Google không thể làm mới phiên đăng nhập lúc này.") from None
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise GoogleYouTubeAuthError("Google không trả về access token khi làm mới phiên.")
        expires_in = max(0, int(payload.get("expires_in") or 3600))
        self._access_cache[account_id] = (access_token, self._clock() + expires_in)
        return access_token

    def verify_account(self, account_id: str) -> YouTubeAccount:
        account = self._require_account(account_id)
        try:
            channels = self._list_channels(self.get_access_token(account_id))
        except TokenRevokedError:
            # An access token can be invalidated before its advertised expiry.
            # Drop only the memory cache and retry once through the refresh flow.
            self._access_cache.pop(account_id, None)
            try:
                channels = self._list_channels(self.get_access_token(account_id))
            except TokenRevokedError:
                self._mark_reauth(account)
                raise
        selected = self._select_channel(channels, account.channel_id)
        account.channel_title = selected.title
        account.needs_reauth = False
        account.updated_at = _utc_now()
        self.store.save(account)
        self._record("youtube.oauth_verify", account)
        return account

    def disconnect(self, account_id: str, *, revoke: bool = True) -> None:
        account = self._require_account(account_id)
        credential = self._load_credential(account, required=False)
        refresh_token = str(credential.get("refresh_token") or "")
        if revoke and refresh_token:
            self._revoke_token(refresh_token, ignore_errors=False)
        self._secret_deleter(account.credential_name)
        self._access_cache.pop(account_id, None)
        self.store.remove(account_id)
        self._record("youtube.oauth_disconnect", account)

    def _interactive_authorization(
        self,
        email_hint: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        callback: dict[str, str] = {}
        handler = _callback_handler(callback)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError as exc:
            raise GoogleYouTubeAuthError("Không thể mở callback OAuth trên 127.0.0.1.") from exc
        server.timeout = 0.4
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        url = build_authorization_url(
            self.client_config,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            email_hint=email_hint,
        )
        try:
            try:
                self._browser_opener(url, new=2)
            except Exception:
                raise GoogleYouTubeAuthError("Không thể mở trình duyệt hệ thống cho Google OAuth.") from None
            deadline = time.monotonic() + OAUTH_TIMEOUT_SECONDS
            while "done" not in callback:
                if cancelled():
                    raise OAuthCancelled("Đã hủy đăng nhập Google.")
                if time.monotonic() >= deadline:
                    raise GoogleYouTubeAuthError("Đăng nhập Google quá 5 phút nên callback đã hết hạn.")
                server.handle_request()
        finally:
            server.server_close()
        if not secrets.compare_digest(callback.get("state", ""), state):
            raise GoogleYouTubeAuthError("Callback OAuth không hợp lệ (state không khớp).")
        if callback.get("error"):
            raise GoogleYouTubeAuthError("Google đã từ chối hoặc hủy yêu cầu đăng nhập.")
        code = callback.get("code", "")
        if not code:
            raise GoogleYouTubeAuthError("Callback OAuth không chứa authorization code.")
        form = {
            "code": code,
            "client_id": self.client_config.client_id,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
        if self.client_config.client_secret:
            form["client_secret"] = self.client_config.client_secret
        try:
            payload = self._requester("POST", self.client_config.token_uri, form, {})
            _raise_payload_error(payload)
            return payload
        except _ProviderError:
            raise GoogleYouTubeAuthError("Google không thể đổi authorization code thành token.") from None

    def _list_channels(self, access_token: str) -> list[YouTubeChannel]:
        query = urllib.parse.urlencode({"part": "id,snippet", "mine": "true", "maxResults": "50"})
        try:
            payload = self._requester(
                "GET",
                f"{CHANNELS_URL}?{query}",
                None,
                {"Authorization": f"Bearer {access_token}"},
            )
            _raise_payload_error(payload)
        except _ProviderError as exc:
            if exc.status in {401, 403} or exc.code in {"invalid_token", "authError"}:
                raise TokenRevokedError("Google không chấp nhận phiên đăng nhập. Hãy đăng nhập lại.") from None
            raise GoogleYouTubeAuthError("Không thể kiểm tra kênh bằng YouTube Data API.") from None
        channels: list[YouTubeChannel] = []
        for item in payload.get("items", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            channel_id = str(item.get("id") or "").strip()
            snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
            if channel_id:
                channels.append(YouTubeChannel(channel_id, str(snippet.get("title") or channel_id)))
        if not channels:
            raise GoogleYouTubeAuthError("Tài khoản Google này không có kênh YouTube có thể truy cập.")
        return channels

    @staticmethod
    def _select_channel(channels: list[YouTubeChannel], expected_channel_id: str) -> YouTubeChannel:
        if expected_channel_id:
            selected = next((item for item in channels if item.channel_id == expected_channel_id), None)
            if selected:
                return selected
            raise WrongChannelError(
                f"Tài khoản vừa đăng nhập không sở hữu kênh đã chọn ({expected_channel_id}). Không lưu token."
            )
        if len(channels) != 1:
            raise ChannelSelectionRequired(channels)
        return channels[0]

    def _load_credential(self, account: YouTubeAccount, *, required: bool = True) -> dict[str, Any]:
        try:
            raw = self._secret_getter(account.credential_name)
            value = json.loads(raw) if raw else {}
        except (OSError, ValueError, TypeError):
            value = {}
        if required and not value:
            self._mark_reauth(account)
            raise TokenRevokedError("Không tìm thấy refresh token an toàn. Hãy đăng nhập lại.")
        return value if isinstance(value, dict) else {}

    def _revoke_token(self, token: str, *, ignore_errors: bool) -> None:
        if not token:
            return
        try:
            payload = self._requester("POST", REVOKE_URL, {"token": token}, {})
            _raise_payload_error(payload)
        except _ProviderError as exc:
            if exc.code in {"invalid_token", "invalid_grant"} or exc.status == 400:
                return
            if not ignore_errors:
                raise GoogleYouTubeAuthError(
                    "Không thể thu hồi token tại Google; tài khoản vẫn được giữ để bạn thử lại."
                ) from None
        except GoogleYouTubeAuthError:
            if not ignore_errors:
                raise

    def _mark_reauth(self, account: YouTubeAccount) -> None:
        self._access_cache.pop(account.account_id, None)
        account.needs_reauth = True
        account.updated_at = _utc_now()
        self.store.save(account)

    def _require_account(self, account_id: str) -> YouTubeAccount:
        account = self.store.get(account_id)
        if not account:
            raise GoogleYouTubeAuthError("Không tìm thấy tài khoản YouTube đã chọn.")
        return account

    def _record(self, action: str, account: YouTubeAccount) -> None:
        try:
            self._audit.record(
                action,
                target_type="youtube_channel",
                target_id=account.channel_id,
                details={"account_id": account.account_id, "needs_reauth": account.needs_reauth},
            )
        except OSError:
            pass


def build_authorization_url(
    config: GoogleDesktopClientConfig,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    email_hint: str = "",
) -> str:
    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if email_hint:
        params["login_hint"] = email_hint
    return f"{config.auth_uri}?{urllib.parse.urlencode(params)}"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _callback_handler(result: dict[str, str]):
    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            for key in ("code", "state", "error"):
                result[key] = str((query.get(key) or [""])[0])
            result["done"] = "1"
            message = (
                "Đăng nhập đã hoàn tất. Bạn có thể đóng tab này và quay lại Visual Loop Studio."
                if not result.get("error")
                else "Yêu cầu đăng nhập đã bị hủy. Bạn có thể đóng tab này."
            )
            body = f"<!doctype html><meta charset='utf-8'><title>Visual Loop Studio</title><p>{message}</p>".encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            # The default logger would include the callback URL and authorization code.
            return

    return OAuthCallbackHandler


def _request_json(method: str, url: str, form: dict[str, str] | None, headers: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    safe_headers = {"Accept": "application/json", **headers}
    if data is not None:
        safe_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=safe_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(64 * 1024).decode("utf-8", errors="replace"))
            code = _payload_error_code(payload)
        except (ValueError, TypeError, OSError):
            code = ""
        raise _ProviderError(code, exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GoogleYouTubeAuthError("Không thể kết nối an toàn tới dịch vụ Google.") from None
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeError):
        raise GoogleYouTubeAuthError("Google trả về phản hồi không hợp lệ.") from None
    return value if isinstance(value, dict) else {}


def _payload_error_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        return str(error.get("status") or error.get("message") or "")
    return ""


def _raise_payload_error(payload: Any) -> None:
    code = _payload_error_code(payload)
    if code:
        raise _ProviderError(code)


def _account_id(channel_id: str) -> str:
    return hashlib.sha256(channel_id.encode("utf-8")).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
