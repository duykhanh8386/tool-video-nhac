from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai.comfyui import (
    WAN_DMD_LORA,
    WAN_MODEL,
    WAN_VARIANT_DMD,
    WAN_TEXT_ENCODER,
    WAN_VAE,
    _find_video_file,
    build_wan22_workflow,
    prepare_workflow,
)


class ComfyUiWorkflowTests(unittest.TestCase):
    def test_built_in_workflow_is_native_and_receives_settings(self):
        workflow = build_wan22_workflow(
            "uploaded.png",
            "Khói bay nhẹ và nhân vật chớp mắt",
            width=1280,
            height=704,
            length=81,
            steps=20,
            cfg=5.0,
            seed=123,
        )
        classes = {node["class_type"] for node in workflow.values()}
        self.assertNotIn("WanVideoApi", classes)
        self.assertTrue(all("cloud" not in name.lower() for name in classes))
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], WAN_MODEL)
        self.assertEqual(workflow["2"]["inputs"]["clip_name"], WAN_TEXT_ENCODER)
        self.assertEqual(workflow["3"]["inputs"]["vae_name"], WAN_VAE)
        self.assertEqual(workflow["6"]["inputs"]["image"], "uploaded.png")
        self.assertEqual(workflow["7"]["inputs"]["width"], 1280)
        self.assertEqual(workflow["7"]["inputs"]["height"], 704)
        self.assertEqual(workflow["7"]["inputs"]["length"], 81)
        self.assertEqual(workflow["9"]["inputs"]["seed"], 123)
        self.assertIn("Locked-off tripod camera", workflow["4"]["inputs"]["text"])
        self.assertIn("Khói bay nhẹ", workflow["4"]["inputs"]["text"])

    def test_dmd_workflow_uses_base_model_lora_and_four_step_operating_point(self):
        workflow = build_wan22_workflow(
            "uploaded.png", "Nhân vật cử động nhẹ", steps=20, cfg=5.0,
            wan_variant=WAN_VARIANT_DMD,
        )
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], WAN_MODEL)
        self.assertEqual(workflow["13"]["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(workflow["13"]["inputs"]["lora_name"], WAN_DMD_LORA)
        self.assertEqual(workflow["13"]["inputs"]["strength_model"], 1.0)
        self.assertEqual(workflow["8"]["inputs"]["model"], ["13", 0])
        self.assertEqual(workflow["8"]["inputs"]["shift"], 5.0)
        self.assertEqual(workflow["9"]["inputs"]["steps"], 4)
        self.assertEqual(workflow["9"]["inputs"]["cfg"], 1.0)
        self.assertEqual(workflow["9"]["inputs"]["sampler_name"], "uni_pc")

    def test_custom_api_workflow_is_injected(self):
        workflow = build_wan22_workflow("old.png", "old prompt")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workflow_api.json"
            path.write_text(json.dumps(workflow), encoding="utf-8")
            result = prepare_workflow(
                str(path), "new.png", "new motion", 832, 480, 49, 12, 4.0,
                987, "no blur", "video/test",
            )
        self.assertEqual(result["6"]["inputs"]["image"], "new.png")
        self.assertEqual(result["7"]["inputs"]["width"], 832)
        self.assertEqual(result["7"]["inputs"]["length"], 49)
        self.assertEqual(result["9"]["inputs"]["steps"], 12)
        self.assertEqual(result["9"]["inputs"]["cfg"], 4.0)
        self.assertEqual(result["12"]["inputs"]["filename_prefix"], "video/test")

    def test_cloud_workflow_is_rejected(self):
        workflow = build_wan22_workflow("old.png", "old prompt")
        workflow["99"] = {"class_type": "SomePartnerCloudApi", "inputs": {}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cloud.json"
            path.write_text(json.dumps(workflow), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "API/Cloud"):
                prepare_workflow(str(path), "x.png", "move", 832, 480, 49, 12, 4, 1, "", "video/test")

    def test_invalid_frame_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError, r"4n\+1"):
            build_wan22_workflow("x.png", "move", length=80)

    def test_video_output_is_found_inside_comfy_history(self):
        outputs = {"12": {"videos": [{"filename": "VisualLoop.mp4", "subfolder": "video", "type": "output"}]}}
        self.assertEqual(_find_video_file(outputs)["filename"], "VisualLoop.mp4")


if __name__ == "__main__":
    unittest.main()
