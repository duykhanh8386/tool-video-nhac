from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.providers.base import GenerationRequest, ProviderError, ProviderExecutionContext
from ai.providers.muse_web_provider import MuseWebProvider
from auth.muse_generation import MuseGenerationQuotaError, MuseGenerationService, MuseGenerationTimeout
from auth.muse_login import CLICKABLE_SELECTOR, MUSE_APP_SELECTORS, MuseAccountStore


class Element:
    def __init__(self, text="", *, attrs=None, tag_name="button", element_id="element", click=None):
        self.text = text
        self.attrs = attrs or {}
        self.tag_name = tag_name
        self.id = element_id
        self.click_callback = click
        self.sent: list[tuple] = []
        self.clicks = 0

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self.attrs.get(name, "")

    def click(self):
        self.clicks += 1
        if self.click_callback:
            self.click_callback()

    def clear(self):
        self.sent.clear()

    def send_keys(self, *values):
        self.sent.append(values)


class SwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        self.driver.current = handle


class GenerationDriver:
    def __init__(self, download_dir: Path, *, quota=False):
        self.download_dir = download_dir
        self.current_url = "https://muse.ai/chat"
        self.current = "task"
        self.handles = ["task"]
        self.switch_to = SwitchTo(self)
        self.marker = Element("Muse", element_id="marker")
        self.upload = Element(attrs={"accept": "image/*"}, tag_name="input", element_id="upload")
        self.prompt = Element(tag_name="textarea", element_id="prompt")
        self.video = Element(attrs={"src": "https://media.muse.ai/generated.mp4"}, tag_name="video", element_id="video-new")
        self.generated = False
        self.quota = quota
        self.submit = Element("Send", click=self._submit)
        self.download = Element("Download", click=self._download)
        self.closed = False
        self.quit_called = False

    @property
    def window_handles(self):
        return list(self.handles)

    @property
    def current_window_handle(self):
        return self.current

    def set_page_load_timeout(self, _value):
        pass

    def get(self, _url):
        pass

    def find_elements(self, by, selector):
        if by == "css selector" and selector == "input[type='file']":
            return [self.upload]
        if by == "css selector" and selector == "textarea":
            return [self.prompt]
        if by == "css selector" and selector in MUSE_APP_SELECTORS:
            return [self.marker]
        if by == "css selector" and selector == CLICKABLE_SELECTOR:
            return [self.download] if self.generated and not self.quota else [self.submit]
        if by == "css selector" and selector == "video":
            return [self.video] if self.generated and not self.quota else []
        if by == "tag name" and selector == "body":
            return [Element("Usage limit reached" if self.generated and self.quota else "")]
        return []

    def execute_script(self, *_args):
        self._download()

    def close(self):
        self.closed = True
        self.handles.clear()

    def quit(self):
        self.quit_called = True

    def _submit(self):
        self.generated = True

    def _download(self):
        (self.download_dir / "muse-result.mp4").write_bytes(b"fake mp4 data")


class MuseGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = MuseAccountStore(self.root / "accounts.json")
        self.account = self.store.ensure("owner@example.com")
        self.account.profile_dir = str(self.root / "profile")
        self.account.status = "connected"
        self.store.save(self.account)
        self.image = self.root / "input.png"
        self.image.write_bytes(b"image")

    def tearDown(self):
        self.temp.cleanup()

    def test_uploads_image_sends_prompt_and_downloads_video(self):
        holder = {}

        def factory(_profile, download_dir):
            holder["driver"] = GenerationDriver(download_dir)
            return holder["driver"]

        target = self.root / "output.mp4"
        service = MuseGenerationService(
            store=self.store,
            driver_factory=factory,
            sleeper=lambda _seconds: None,
            poll_interval=0.1,
        )

        result = service.generate(self.account.account_id, "Animate the image", self.image, target)

        driver = holder["driver"]
        self.assertEqual(result, target.resolve())
        self.assertEqual(target.read_bytes(), b"fake mp4 data")
        self.assertIn((str(self.image.resolve()),), driver.upload.sent)
        self.assertIn(("Animate the image",), driver.prompt.sent)
        self.assertEqual(driver.submit.clicks, 1)
        self.assertEqual(driver.download.clicks, 1)
        self.assertTrue(driver.closed)

    def test_quota_message_stops_generation_without_downloading(self):
        holder = {}

        def factory(_profile, download_dir):
            holder["driver"] = GenerationDriver(download_dir, quota=True)
            return holder["driver"]

        service = MuseGenerationService(
            store=self.store,
            driver_factory=factory,
            sleeper=lambda _seconds: None,
            poll_interval=0.1,
        )

        with self.assertRaises(MuseGenerationQuotaError):
            service.generate(self.account.account_id, "Animate", self.image, self.root / "never.mp4")

        self.assertEqual(holder["driver"].download.clicks, 0)

    def test_provider_requires_one_selected_connected_account(self):
        provider = MuseWebProvider("https://muse.ai/", store=self.store)
        request = GenerationRequest(
            provider_id="muse_web",
            model_id="muse-web-video",
            prompt="Animate",
            output_path=str(self.root / "out.mp4"),
            source_images=[str(self.image)],
            duration=10,
            aspect_ratio="16:9",
            resolution="720p",
            options={"muse_account_id": self.account.account_id},
        )

        with patch("ai.providers.muse_web_provider.MuseGenerationService.generate", return_value=self.root / "out.mp4") as generate:
            result = provider.generate(request, ProviderExecutionContext())

        self.assertEqual(result, str(self.root / "out.mp4"))
        self.assertEqual(generate.call_args.args[0], self.account.account_id)

    def test_provider_maps_quota_to_non_retrying_provider_quota_error(self):
        provider = MuseWebProvider("https://muse.ai/", store=self.store)
        request = GenerationRequest(
            provider_id="muse_web",
            model_id="muse-web-video",
            prompt="Animate",
            output_path=str(self.root / "out.mp4"),
            source_images=[str(self.image)],
            duration=10,
            aspect_ratio="16:9",
            resolution="720p",
            options={"muse_account_id": self.account.account_id},
        )
        with patch(
            "ai.providers.muse_web_provider.MuseGenerationService.generate",
            side_effect=MuseGenerationQuotaError("quota"),
        ):
            with self.assertRaises(ProviderError) as raised:
                provider.generate(request, ProviderExecutionContext())

        self.assertTrue(raised.exception.quota_error)
        self.assertFalse(raised.exception.retryable)

    def test_provider_does_not_replay_an_ambiguous_browser_timeout(self):
        provider = MuseWebProvider("https://muse.ai/", store=self.store)
        request = GenerationRequest(
            provider_id="muse_web",
            model_id="muse-web-video",
            prompt="Animate",
            output_path=str(self.root / "out.mp4"),
            source_images=[str(self.image)],
            duration=10,
            aspect_ratio="16:9",
            resolution="720p",
            options={"muse_account_id": self.account.account_id},
        )
        with patch(
            "ai.providers.muse_web_provider.MuseGenerationService.generate",
            side_effect=MuseGenerationTimeout("timeout"),
        ):
            with self.assertRaises(ProviderError) as raised:
                provider.generate(request, ProviderExecutionContext())

        self.assertEqual(raised.exception.code, "timeout")
        self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
