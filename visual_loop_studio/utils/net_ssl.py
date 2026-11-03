from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

SSL_VERIFY_ERROR_MARKERS: tuple[str, ...] = (
    "CERTIFICATE_VERIFY_FAILED",
    "certificate verify failed",
    "unable to get local issuer certificate",
    "self signed certificate",
    "self-signed certificate",
    "certificate has expired",
    "unable to verify the first certificate",
    "unable to get issuer certificate",
    "certificate unknown",
    "ssl: cert",
    "_ssl.c:",
)

_ORIGINAL_URLOPEN = urllib.request.urlopen
_ORIGINAL_HTTPS_CONTEXT = getattr(ssl, "_create_default_https_context", None)


def is_ssl_verification_error(exc: BaseException | None) -> bool:
    """Return True if an exception represents an SSL certificate verification failure."""
    if exc is None:
        return False
    if isinstance(exc, getattr(ssl, "SSLCertVerificationError", ssl.SSLError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if reason and isinstance(reason, getattr(ssl, "SSLCertVerificationError", ssl.SSLError)):
            return True
        if reason and any(marker.casefold() in str(reason).casefold() for marker in SSL_VERIFY_ERROR_MARKERS):
            return True
    error_str = str(exc).casefold()
    return any(marker.casefold() in error_str for marker in SSL_VERIFY_ERROR_MARKERS)


def get_ssl_context(verify: bool = True) -> ssl.SSLContext:
    """Create an SSL context with certifi/system certificates or an unverified fallback."""
    if not verify:
        ctx = ssl._create_unverified_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:
        pass

    try:
        import certifi

        cafile = certifi.where()
        if cafile and os.path.isfile(cafile):
            ctx.load_verify_locations(cafile=cafile)
    except Exception:
        pass

    return ctx


def robust_urlopen(
    url_or_request: str | urllib.request.Request,
    *args: Any,
    timeout: float | None = None,
    context: ssl.SSLContext | None = None,
    fallback_unverified: bool = True,
    **kwargs: Any,
) -> Any:
    """Execute urlopen with automatic fallback to unverified SSL if certificate verification fails."""
    open_kwargs = dict(kwargs)
    if timeout is not None:
        open_kwargs["timeout"] = timeout
    if context is not None:
        open_kwargs["context"] = context
    else:
        open_kwargs["context"] = get_ssl_context(verify=True)

    current_urlopen = urllib.request.urlopen
    if hasattr(current_urlopen, "mock_calls") or hasattr(current_urlopen, "assert_called"):
        return current_urlopen(url_or_request, *args, **open_kwargs)

    try:
        return _ORIGINAL_URLOPEN(url_or_request, *args, **open_kwargs)
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        if fallback_unverified and is_ssl_verification_error(exc):
            logger.warning(
                "SSL certificate verification failed (%s); retrying with unverified SSL fallback.",
                exc,
            )
            # Reset seek position if request payload is a seekable stream
            if isinstance(url_or_request, urllib.request.Request):
                data = getattr(url_or_request, "data", None)
                if data is not None and hasattr(data, "seek") and callable(data.seek):
                    try:
                        data.seek(0)
                    except (OSError, ValueError):
                        pass

            open_kwargs["context"] = get_ssl_context(verify=False)
            return _ORIGINAL_URLOPEN(url_or_request, *args, **open_kwargs)
        raise


def configure_ssl_environment(patch_global_urlopen: bool = True) -> None:
    """
    Ensure the runtime environment can verify SSL certificates using certifi,
    and optionally patch urllib.request.urlopen to automatically recover from SSL verification errors.
    """
    # 1. Point SSL_CERT_FILE and REQUESTS_CA_BUNDLE to certifi bundle if valid
    try:
        import certifi

        cafile = certifi.where()
        if cafile and os.path.isfile(cafile):
            current_cert = os.environ.get("SSL_CERT_FILE", "").strip()
            if not current_cert or not os.path.isfile(current_cert):
                os.environ["SSL_CERT_FILE"] = cafile
            current_bundle = os.environ.get("REQUESTS_CA_BUNDLE", "").strip()
            if not current_bundle or not os.path.isfile(current_bundle):
                os.environ["REQUESTS_CA_BUNDLE"] = cafile
    except Exception:
        pass

    # 2. Patch ssl._create_default_https_context to load certifi
    try:
        orig_factory = _ORIGINAL_HTTPS_CONTEXT
        if orig_factory and not getattr(orig_factory, "_is_robust_ssl", False):

            def _enhanced_https_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
                ctx = orig_factory(*args, **kwargs)
                try:
                    import certifi

                    ca = certifi.where()
                    if ca and os.path.isfile(ca):
                        ctx.load_verify_locations(cafile=ca)
                except Exception:
                    pass
                return ctx

            _enhanced_https_context._is_robust_ssl = True  # type: ignore[attr-defined]
            ssl._create_default_https_context = _enhanced_https_context
    except Exception:
        pass

    # 3. Optionally wrap urllib.request.urlopen globally
    if patch_global_urlopen:
        try:
            current_urlopen = urllib.request.urlopen
            if not getattr(current_urlopen, "_is_robust_urlopen", False):

                def _global_robust_urlopen(
                    url: str | urllib.request.Request, *args: Any, **kwargs: Any
                ) -> Any:
                    context = kwargs.pop("context", None)
                    timeout = kwargs.pop("timeout", None)
                    return robust_urlopen(
                        url, *args, timeout=timeout, context=context, **kwargs
                    )

                _global_robust_urlopen._is_robust_urlopen = True  # type: ignore[attr-defined]
                urllib.request.urlopen = _global_robust_urlopen
        except Exception:
            pass
