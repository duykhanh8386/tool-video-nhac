from __future__ import annotations

import os
import ssl
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

from utils.net_ssl import (
    SSL_VERIFY_ERROR_MARKERS,
    configure_ssl_environment,
    get_ssl_context,
    is_ssl_verification_error,
    robust_urlopen,
)


class FakeSSLCertVerificationError(ssl.SSLError):
    pass


class NetSSLTests(unittest.TestCase):
    def test_is_ssl_verification_error(self):
        # Direct verification error string
        err_msg = (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:1010)"
        )
        url_err = urllib.error.URLError(FakeSSLCertVerificationError(1, err_msg))
        self.assertTrue(is_ssl_verification_error(url_err))

        # URLError with string reason
        url_err_str = urllib.error.URLError(err_msg)
        self.assertTrue(is_ssl_verification_error(url_err_str))

        # Direct SSLCertVerificationError if available
        if hasattr(ssl, "SSLCertVerificationError"):
            cert_err = ssl.SSLCertVerificationError(1, "certificate verify failed")
            self.assertTrue(is_ssl_verification_error(cert_err))
            self.assertTrue(is_ssl_verification_error(urllib.error.URLError(cert_err)))

        # Non-SSL errors should return False
        self.assertFalse(is_ssl_verification_error(urllib.error.URLError("Connection refused")))
        self.assertFalse(is_ssl_verification_error(TimeoutError("timed out")))
        self.assertFalse(is_ssl_verification_error(None))

    def test_get_ssl_context_unverified(self):
        ctx = get_ssl_context(verify=False)
        self.assertFalse(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_get_ssl_context_verified(self):
        ctx = get_ssl_context(verify=True)
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_robust_urlopen_recovers_from_ssl_verify_failure(self):
        calls: list[ssl.SSLContext | None] = []

        def fake_urlopen(req, *args, **kwargs):
            ctx = kwargs.get("context")
            calls.append(ctx)
            if len(calls) == 1:
                raise urllib.error.URLError(
                    FakeSSLCertVerificationError(
                        1,
                        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                        "unable to get local issuer certificate (_ssl.c:1010)",
                    )
                )
            mock_resp = MagicMock()
            mock_resp.status = 200
            return mock_resp

        with patch("utils.net_ssl._ORIGINAL_URLOPEN", side_effect=fake_urlopen):
            resp = robust_urlopen("https://github.com/test", timeout=10)
            self.assertEqual(resp.status, 200)
            self.assertEqual(len(calls), 2)
            # First call was verified, second call was unverified fallback
            self.assertEqual(calls[1].verify_mode, ssl.CERT_NONE)
            self.assertFalse(calls[1].check_hostname)

    def test_robust_urlopen_does_not_retry_non_ssl_errors(self):
        calls: list[int] = []

        def fake_urlopen(req, *args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError("Connection refused")

        with patch("utils.net_ssl._ORIGINAL_URLOPEN", side_effect=fake_urlopen):
            with self.assertRaises(urllib.error.URLError):
                robust_urlopen("https://github.com/test", timeout=10)
            self.assertEqual(len(calls), 1)

    def test_configure_ssl_environment(self):
        configure_ssl_environment(patch_global_urlopen=True)
        self.assertTrue(getattr(urllib.request.urlopen, "_is_robust_urlopen", False))


if __name__ == "__main__":
    unittest.main()
