from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.providers.base import GenerationRequest, ProviderExecutionContext
from ai.providers.byteplus_seedance import BytePlusSeedanceProvider


class BytePlusSeedanceProviderTests(unittest.TestCase):
    def request(self, folder: str, **overrides) -> GenerationRequest:
        image = Path(folder) / "frame.png"
        image.write_bytes(b"fake-image")
        values = {
            "provider_id": "byteplus_seedance",
            "model_id": "dreamina-seedance-2-5-260628",
            "prompt": "A calm cinematic forest",
            "output_path": str(Path(folder) / "result.mp4"),
            "source_images": [str(image)],
            "duration": 8,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "generate_audio": True,
        }
        values.update(overrides)
        return GenerationRequest(**values)

    def test_payload_uses_official_content_schema_and_base64_image(self) -> None:
        provider = BytePlusSeedanceProvider()
        with tempfile.TemporaryDirectory() as folder:
            payload = provider._build_payload(self.request(folder))
        self.assertEqual(payload["model"], "dreamina-seedance-2-5-260628")
        self.assertEqual(payload["content"][0], {"type": "text", "text": "A calm cinematic forest"})
        self.assertEqual(payload["content"][1]["role"], "first_frame")
        self.assertTrue(payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(payload["generate_audio"])

    def test_seedance_25_accepts_thirty_seconds_but_seedance_20_does_not(self) -> None:
        provider = BytePlusSeedanceProvider()
        with tempfile.TemporaryDirectory() as folder:
            provider.validate(self.request(folder, duration=30))
            request = self.request(
                folder,
                model_id="dreamina-seedance-2-0-260128",
                duration=30,
            )
            with self.assertRaises(ValueError):
                provider.validate(request)

    @patch.object(BytePlusSeedanceProvider, "_download")
    @patch.object(BytePlusSeedanceProvider, "_json_request")
    @patch.object(BytePlusSeedanceProvider, "_api_key", return_value="official-key")
    def test_existing_task_is_resumed_without_submitting_again(self, _key, request_json, download) -> None:
        request_json.return_value = {
            "id": "lsd-existing",
            "status": "succeeded",
            "content": {"video_url": "https://example.test/video.mp4"},
        }
        provider = BytePlusSeedanceProvider()
        with tempfile.TemporaryDirectory() as folder:
            request = self.request(folder)
            context = ProviderExecutionContext(external_id="lsd-existing")
            provider.generate(request, context)
        self.assertEqual(request_json.call_args.args[0], "GET")
        self.assertIn("lsd-existing", request_json.call_args.args[1])
        download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
