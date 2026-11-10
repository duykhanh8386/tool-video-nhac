from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import random
import re
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from ai.veo import motion_prompt
from utils.net_ssl import get_ssl_context


WAN_VARIANT_QUALITY = "quality"
WAN_VARIANT_DMD = "dmd4"
LOCAL_MODEL_LTX = "ltx2b"
LOCAL_MODEL_HUNYUAN = "hunyuan15"
WAN_MODEL = "wan2.2_ti2v_5B_fp16.safetensors"
WAN_DMD_LORA = "wan2.2_5b_nonar_dmd_4step_lora_r64_comfy.safetensors"
WAN_TEXT_ENCODER = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "wan2.2_vae.safetensors"
LTX_MODEL = "ltxv-2b-0.9.6-distilled-04-25.safetensors"
LTX_TEXT_ENCODER = "t5xxl_fp8_e4m3fn_scaled.safetensors"
HUNYUAN_MODEL = "hunyuanvideo1.5_480p_i2v_step_distilled_fp8_scaled.safetensors"
HUNYUAN_TEXT_ENCODER = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
HUNYUAN_BYT5_ENCODER = "byt5_small_glyphxl_fp16.safetensors"
HUNYUAN_VAE = "hunyuanvideo15_vae_fp16.safetensors"
HUNYUAN_CLIP_VISION = "sigclip_vision_patch14_384.safetensors"
DEFAULT_NEGATIVE_PROMPT = (
    "camera movement, camera shake, zoom, pan, tilt, scene cut, reframing, flicker, "
    "morphing, identity change, deformed face, deformed hands, extra fingers, extra limbs, "
    "bad anatomy, duplicated body parts, blurry details, low quality, text, subtitles, watermark"
)
DMD_NEGATIVE_PROMPT = (
    f"{DEFAULT_NEGATIVE_PROMPT}, jitter, temporal stutter, sudden acceleration, "
    "back-and-forth oscillation, repeated micro-motion, unstable motion"
)


def _wan_positive_prompt(user_prompt: str, dmd: bool) -> str:
    """Keep the 4-step adapter focused on the user's action before stability hints."""
    if not dmd:
        return motion_prompt(user_prompt)
    prompt = user_prompt.strip()
    if not prompt:
        return motion_prompt(user_prompt)
    return (
        f"Requested motion (highest priority): {prompt}\n\n"
        "Locked camera and stable composition. Preserve the source image identity, face, anatomy, "
        "clothing, background, objects, colors and lighting. Perform only the requested motion as "
        "one coherent continuous action with controlled amplitude, even timing and smooth temporal "
        "consistency. Do not invent extra actions or oscillate back and forth unless explicitly requested."
    )


class LocalGenerationCancelled(RuntimeError):
    pass


def build_wan22_workflow(
    uploaded_image: str,
    prompt: str,
    width: int = 1280,
    height: int = 704,
    length: int = 81,
    steps: int = 20,
    cfg: float = 5.0,
    seed: int | None = None,
    negative_prompt: str = "",
    output_prefix: str = "video/VisualLoopStudio",
    wan_variant: str = WAN_VARIANT_QUALITY,
) -> dict:
    wan_variant = normalize_wan_variant(wan_variant)
    dmd = wan_variant == WAN_VARIANT_DMD
    if dmd:
        # The Apache-2.0 DMD adapter is distilled for this operating point.
        steps, cfg = 4, 1.0
    _validate_video_settings(width, height, length, steps, cfg)
    seed = random.SystemRandom().randrange(0, 2**63 - 1) if seed is None or seed < 0 else int(seed)
    model_title = "Wan 2.2 TI2V 5B + DMD LoRA (4 bước)" if dmd else "Wan 2.2 TI2V 5B Chất lượng"
    workflow = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": WAN_MODEL, "weight_dtype": "default"},
            "_meta": {"title": model_title},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": WAN_TEXT_ENCODER, "type": "wan", "device": "default"},
            "_meta": {"title": "Wan text encoder"},
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": WAN_VAE},
            "_meta": {"title": "Wan VAE"},
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": _wan_positive_prompt(prompt, dmd), "clip": ["2", 0]},
            "_meta": {"title": "Positive Prompt"},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative_prompt.strip() or (DMD_NEGATIVE_PROMPT if dmd else DEFAULT_NEGATIVE_PROMPT),
                "clip": ["2", 0],
            },
            "_meta": {"title": "Negative Prompt"},
        },
        "6": {
            "class_type": "LoadImage",
            "inputs": {"image": uploaded_image},
            "_meta": {"title": "Start Image"},
        },
        "7": {
            "class_type": "Wan22ImageToVideoLatent",
            "inputs": {
                "vae": ["3", 0], "start_image": ["6", 0], "width": int(width),
                "height": int(height), "length": int(length), "batch_size": 1,
            },
            "_meta": {"title": "Wan 2.2 Image to Video"},
        },
        "8": {
            "class_type": "ModelSamplingSD3",
            "inputs": {"model": ["13", 0] if dmd else ["1", 0], "shift": 5.0 if dmd else 8.0},
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["8", 0], "positive": ["4", 0], "negative": ["5", 0],
                "latent_image": ["7", 0], "seed": seed, "steps": int(steps),
                "cfg": float(cfg), "sampler_name": "uni_pc",
                "scheduler": "simple", "denoise": 1.0,
            },
        },
        "10": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["9", 0], "vae": ["3", 0]},
        },
        "11": {
            "class_type": "CreateVideo",
            "inputs": {"images": ["10", 0], "fps": 24.0},
        },
        "12": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["11", 0], "filename_prefix": output_prefix, "format": "auto", "codec": "auto"},
            "_meta": {"title": "Visual Loop Studio Output"},
        },
    }
    if dmd:
        workflow["13"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["1", 0], "lora_name": WAN_DMD_LORA, "strength_model": 1.0},
            "_meta": {"title": "Wan 2.2 DMD 4-step LoRA (Apache 2.0)"},
        }
    return workflow


def build_ltx_workflow(
    uploaded_image: str,
    prompt: str,
    width: int = 832,
    height: int = 480,
    length: int = 81,
    steps: int = 8,
    cfg: float = 1.0,
    seed: int | None = None,
    negative_prompt: str = "",
    output_prefix: str = "video/VisualLoopStudio",
) -> dict:
    """Build a stock-ComfyUI LTX-Video 2B distilled image-to-video graph."""
    steps, cfg = 8, 1.0
    _validate_video_settings(width, height, length, steps, cfg, LOCAL_MODEL_LTX)
    seed = random.SystemRandom().randrange(0, 2**63 - 1) if seed is None or seed < 0 else int(seed)
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": LTX_MODEL},
            "_meta": {"title": "LTX-Video 2B Distilled"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": LTX_TEXT_ENCODER, "type": "ltxv", "device": "default"},
            "_meta": {"title": "LTX T5 text encoder"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": motion_prompt(prompt), "clip": ["2", 0]},
            "_meta": {"title": "Positive Prompt"},
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt.strip() or DEFAULT_NEGATIVE_PROMPT, "clip": ["2", 0]},
            "_meta": {"title": "Negative Prompt"},
        },
        "5": {
            "class_type": "LoadImage",
            "inputs": {"image": uploaded_image},
            "_meta": {"title": "Start Image"},
        },
        "6": {
            "class_type": "LTXVImgToVideo",
            "inputs": {
                "positive": ["3", 0], "negative": ["4", 0], "vae": ["1", 2],
                "image": ["5", 0], "width": int(width), "height": int(height),
                "length": int(length), "batch_size": 1, "strength": 0.15,
            },
            "_meta": {"title": "LTX image to video"},
        },
        "7": {
            "class_type": "LTXVConditioning",
            "inputs": {"positive": ["6", 0], "negative": ["6", 1], "frame_rate": 24.0},
        },
        "8": {
            "class_type": "LTXVScheduler",
            "inputs": {
                "steps": steps, "max_shift": 2.05, "base_shift": 0.95,
                "stretch": True, "terminal": 0.1, "latent": ["6", 2],
            },
        },
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "10": {
            "class_type": "SamplerCustom",
            "inputs": {
                "model": ["1", 0], "positive": ["7", 0], "negative": ["7", 1],
                "sampler": ["9", 0], "sigmas": ["8", 0], "latent_image": ["6", 2],
                "add_noise": True, "noise_seed": seed, "cfg": cfg,
            },
        },
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["1", 2]}},
        "12": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "fps": 24.0}},
        "13": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["12", 0], "filename_prefix": output_prefix, "format": "auto", "codec": "auto"},
            "_meta": {"title": "Visual Loop Studio Output"},
        },
    }


def build_hunyuan15_workflow(
    uploaded_image: str,
    prompt: str,
    width: int = 832,
    height: int = 480,
    length: int = 81,
    steps: int = 8,
    cfg: float = 1.0,
    seed: int | None = None,
    negative_prompt: str = "",
    output_prefix: str = "video/VisualLoopStudio",
) -> dict:
    """Build the official 480p step-distilled HunyuanVideo 1.5 I2V graph."""
    steps, cfg = 8, 1.0
    _validate_video_settings(width, height, length, steps, cfg, LOCAL_MODEL_HUNYUAN)
    seed = random.SystemRandom().randrange(0, 2**63 - 1) if seed is None or seed < 0 else int(seed)
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": HUNYUAN_MODEL, "weight_dtype": "default"},
            "_meta": {"title": "HunyuanVideo 1.5 480p I2V Step Distilled"},
        },
        "2": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": HUNYUAN_TEXT_ENCODER, "clip_name2": HUNYUAN_BYT5_ENCODER,
                "type": "hunyuan_video_15", "device": "default",
            },
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": HUNYUAN_VAE}},
        "4": {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": HUNYUAN_CLIP_VISION}},
        "5": {"class_type": "LoadImage", "inputs": {"image": uploaded_image}},
        "6": {
            "class_type": "CLIPVisionEncode",
            "inputs": {"clip_vision": ["4", 0], "image": ["5", 0], "crop": "center"},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": motion_prompt(prompt), "clip": ["2", 0]},
            "_meta": {"title": "Positive Prompt"},
        },
        "8": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt.strip() or DEFAULT_NEGATIVE_PROMPT, "clip": ["2", 0]},
            "_meta": {"title": "Negative Prompt"},
        },
        "9": {
            "class_type": "HunyuanVideo15ImageToVideo",
            "inputs": {
                "positive": ["7", 0], "negative": ["8", 0], "vae": ["3", 0],
                "start_image": ["5", 0], "clip_vision_output": ["6", 0],
                "width": int(width), "height": int(height), "length": int(length), "batch_size": 1,
            },
        },
        "10": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 7.0}},
        "11": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "12": {
            "class_type": "CFGGuider",
            "inputs": {"model": ["10", 0], "positive": ["9", 0], "negative": ["9", 1], "cfg": cfg},
        },
        "13": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "14": {
            "class_type": "BasicScheduler",
            "inputs": {"model": ["10", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0},
        },
        "15": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["11", 0], "guider": ["12", 0], "sampler": ["13", 0],
                "sigmas": ["14", 0], "latent_image": ["9", 2],
            },
        },
        "16": {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["3", 0]}},
        "17": {"class_type": "CreateVideo", "inputs": {"images": ["16", 0], "fps": 24.0}},
        "18": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["17", 0], "filename_prefix": output_prefix, "format": "auto", "codec": "auto"},
            "_meta": {"title": "Visual Loop Studio Output"},
        },
    }


def prepare_workflow(
    workflow_path: str,
    uploaded_image: str,
    prompt: str,
    width: int,
    height: int,
    length: int,
    steps: int,
    cfg: float,
    seed: int | None,
    negative_prompt: str,
    output_prefix: str,
    wan_variant: str = WAN_VARIANT_QUALITY,
) -> dict:
    wan_variant = normalize_wan_variant(wan_variant)
    if not workflow_path:
        if wan_variant == LOCAL_MODEL_LTX:
            return build_ltx_workflow(
                uploaded_image, prompt, width, height, length, steps, cfg, seed,
                negative_prompt, output_prefix,
            )
        if wan_variant == LOCAL_MODEL_HUNYUAN:
            return build_hunyuan15_workflow(
                uploaded_image, prompt, width, height, length, steps, cfg, seed,
                negative_prompt, output_prefix,
            )
        return build_wan22_workflow(
            uploaded_image, prompt, width, height, length, steps, cfg, seed,
            negative_prompt, output_prefix, wan_variant,
        )
    if wan_variant != WAN_VARIANT_QUALITY:
        raise ValueError(
            f"Chế độ {local_model_label(wan_variant)} chỉ dùng với workflow tích hợp. "
            "Hãy xóa đường dẫn workflow ComfyUI tùy chỉnh trong Cài đặt."
        )
    path = Path(workflow_path).expanduser()
    if not path.is_file():
        raise ValueError(f"Không tìm thấy workflow ComfyUI API: {workflow_path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Không đọc được workflow ComfyUI: {exc}") from exc
    workflow = data.get("prompt", data) if isinstance(data, dict) else data
    if not isinstance(workflow, dict) or "nodes" in workflow or not all(
        isinstance(node, dict) and "class_type" in node for node in workflow.values()
    ):
        raise ValueError("Workflow phải được Export ở định dạng API của ComfyUI, không phải định dạng giao diện.")
    workflow = copy.deepcopy(workflow)
    _assert_local_only(workflow)
    _inject_workflow_values(
        workflow, uploaded_image, prompt, width, height, length, steps, cfg,
        seed, negative_prompt, output_prefix,
    )
    return workflow


def check_comfyui(
    base_url: str,
    require_default_models: bool = True,
    wan_variant: str = WAN_VARIANT_QUALITY,
) -> dict:
    base = _base_url(base_url)
    stats = _request_json("GET", f"{base}/system_stats", timeout=15)
    object_info = _request_json("GET", f"{base}/object_info", timeout=30)
    return _check_local_model_files(stats, object_info, require_default_models, wan_variant)


def _check_local_model_files(
    stats: dict,
    object_info: dict,
    require_default_models: bool,
    wan_variant: str,
) -> dict:
    wan_variant = normalize_wan_variant(wan_variant)
    common_nodes = {"CLIPTextEncode", "LoadImage", "VAEDecode", "CreateVideo", "SaveVideo"}
    if wan_variant == LOCAL_MODEL_LTX:
        required_nodes = common_nodes | {
            "CheckpointLoaderSimple", "CLIPLoader", "LTXVImgToVideo", "LTXVConditioning",
            "LTXVScheduler", "KSamplerSelect", "SamplerCustom",
        }
        checks = [
            ("CheckpointLoaderSimple", "ckpt_name", LTX_MODEL, "models/checkpoints"),
            ("CLIPLoader", "clip_name", LTX_TEXT_ENCODER, "models/text_encoders"),
        ]
    elif wan_variant == LOCAL_MODEL_HUNYUAN:
        required_nodes = common_nodes | {
            "UNETLoader", "DualCLIPLoader", "VAELoader", "CLIPVisionLoader", "CLIPVisionEncode",
            "HunyuanVideo15ImageToVideo", "ModelSamplingSD3", "RandomNoise", "CFGGuider",
            "KSamplerSelect", "BasicScheduler", "SamplerCustomAdvanced",
        }
        checks = [
            ("UNETLoader", "unet_name", HUNYUAN_MODEL, "models/diffusion_models"),
            ("DualCLIPLoader", "clip_name1", HUNYUAN_TEXT_ENCODER, "models/text_encoders"),
            ("DualCLIPLoader", "clip_name2", HUNYUAN_BYT5_ENCODER, "models/text_encoders"),
            ("VAELoader", "vae_name", HUNYUAN_VAE, "models/vae"),
            ("CLIPVisionLoader", "clip_name", HUNYUAN_CLIP_VISION, "models/clip_vision"),
        ]
    else:
        required_nodes = common_nodes | {
            "UNETLoader", "CLIPLoader", "VAELoader", "Wan22ImageToVideoLatent",
            "ModelSamplingSD3", "KSampler",
        }
        if wan_variant == WAN_VARIANT_DMD:
            required_nodes.add("LoraLoaderModelOnly")
        checks = [
            ("UNETLoader", "unet_name", WAN_MODEL, "models/diffusion_models"),
            ("CLIPLoader", "clip_name", WAN_TEXT_ENCODER, "models/text_encoders"),
            ("VAELoader", "vae_name", WAN_VAE, "models/vae"),
        ]
        if wan_variant == WAN_VARIANT_DMD:
            checks.append(("LoraLoaderModelOnly", "lora_name", WAN_DMD_LORA, "models/loras"))

    missing_nodes = sorted(required_nodes - set(object_info))
    if missing_nodes:
        raise RuntimeError(
            f"ComfyUI đang thiếu node cho {local_model_label(wan_variant)}: "
            + ", ".join(missing_nodes)
            + ". Hãy cập nhật ComfyUI lên bản mới nhất."
        )
    if require_default_models:
        missing_models = []
        for node_name, input_name, filename, folder in checks:
            values = _input_choices(object_info, node_name, input_name)
            if not any(str(value).replace(chr(92), "/").endswith(filename) for value in values):
                missing_models.append(f"{filename} → ComfyUI/{folder}")
        if missing_models:
            raise RuntimeError(
                f"Thiếu model {local_model_label(wan_variant)} đang chọn:\n- "
                + "\n- ".join(missing_models)
                + "\nBấm ‘Cài / tải model đang chọn’ để tải bổ sung."
            )
    return stats


def _legacy_check_comfyui_models(
    object_info: dict,
    wan_variant: str,
    require_default_models: bool = True,
) -> None:
    """Kept below only to preserve source compatibility; new checks return above."""
    wan_variant = normalize_wan_variant(wan_variant)
    required_nodes = {
        "UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "LoadImage",
        "Wan22ImageToVideoLatent", "ModelSamplingSD3", "KSampler", "VAEDecode",
        "CreateVideo", "SaveVideo",
    }
    if wan_variant == WAN_VARIANT_DMD:
        required_nodes.add("LoraLoaderModelOnly")
    missing_nodes = sorted(required_nodes - set(object_info))
    if missing_nodes:
        raise RuntimeError(
            "ComfyUI đang thiếu node Wan 2.2 Native: " + ", ".join(missing_nodes)
            + ". Hãy cập nhật ComfyUI lên bản mới nhất."
        )
    if require_default_models:
        missing_models = []
        checks = [
            ("UNETLoader", "unet_name", WAN_MODEL, "models/diffusion_models"),
            ("CLIPLoader", "clip_name", WAN_TEXT_ENCODER, "models/text_encoders"),
            ("VAELoader", "vae_name", WAN_VAE, "models/vae"),
        ]
        if wan_variant == WAN_VARIANT_DMD:
            checks.append(("LoraLoaderModelOnly", "lora_name", WAN_DMD_LORA, "models/loras"))
        for node_name, input_name, filename, folder in checks:
            values = _input_choices(object_info, node_name, input_name)
            if not any(str(value).replace("\\", "/").endswith(filename) for value in values):
                missing_models.append(f"{filename} → ComfyUI/{folder}")
        if missing_models:
            raise RuntimeError(
                "Thiếu model Wan 2.2 đang chọn:\n- " + "\n- ".join(missing_models)
                + "\nBấm ‘Cài / tải model Wan đang chọn’ để tải bổ sung."
            )
    return None


def recommended_video_timeout(
    wan_variant: str = WAN_VARIANT_QUALITY,
    width: int = 832,
    height: int = 480,
    length: int = 81,
    steps: int = 20,
) -> int:
    """Return a practical per-clip hard timeout (seconds) tailored to model speed and resolution."""
    variant = normalize_wan_variant(wan_variant)
    pixels = max(256 * 256, int(width) * int(height))
    high_res = pixels >= 700_000
    long_clip = int(length) > 100
    if variant in {WAN_VARIANT_DMD, LOCAL_MODEL_LTX, LOCAL_MODEL_HUNYUAN}:
        base_timeout = 2100 if high_res else 1200  # 35 min @ 720p, 20 min @ 480p
        if long_clip and high_res:
            base_timeout = 2400
        return base_timeout
    # Quality 20-step or custom workflow
    scale = max(1.0, int(steps) / 20.0)
    base_timeout = 4500 if high_res else 2700  # 75 min @ 720p, 45 min @ 480p
    return min(4 * 60 * 60, int(base_timeout * scale))


def recommended_stall_timeout(
    wan_variant: str = WAN_VARIANT_QUALITY,
    width: int = 832,
    height: int = 480,
) -> int:
    """Return max seconds with zero ComfyUI step/node progress before treating a running job as wedged."""
    variant = normalize_wan_variant(wan_variant)
    high_res = (int(width) * int(height)) >= 700_000
    if variant in {WAN_VARIANT_DMD, LOCAL_MODEL_LTX, LOCAL_MODEL_HUNYUAN}:
        return 900 if high_res else 600  # 15 min @ 720p, 10 min @ 480p
    return 1500 if high_res else 1200


def get_comfyui_queue(base_url: str, timeout: int = 15) -> dict | None:
    """Fetch ComfyUI /queue payload safely; returns None when unreachable."""
    base = _base_url(base_url)
    try:
        data = _request_json("GET", f"{base}/queue", timeout=timeout)
        return data if isinstance(data, dict) else None
    except RuntimeError:
        return None


def get_comfyui_queue_ids(base_url: str, timeout: int = 15) -> tuple[set[str], set[str]] | None:
    """Return (running_prompt_ids, pending_prompt_ids), or None when ComfyUI is unreachable."""
    queue_data = get_comfyui_queue(base_url, timeout=timeout)
    if not isinstance(queue_data, dict):
        return None
    if "queue_running" not in queue_data and "queue_pending" not in queue_data:
        return None
    return _extract_queue_prompt_ids(queue_data)


def interrupt_comfyui(base_url: str) -> None:
    """Best-effort request to interrupt the currently active ComfyUI prompt."""
    _interrupt(_base_url(base_url))


def _extract_queue_prompt_ids(queue_data: dict) -> tuple[set[str], set[str]]:
    running: set[str] = set()
    pending: set[str] = set()
    for key, target in (("queue_running", running), ("queue_pending", pending)):
        items = queue_data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1]:
                target.add(str(item[1]))
            elif isinstance(item, dict) and item.get("prompt_id"):
                target.add(str(item["prompt_id"]))
    return running, pending


def free_comfyui_memory(
    base_url: str,
    *,
    unload_models: bool = False,
    free_memory: bool = True,
    timeout: int = 15,
) -> bool:
    """Ask ComfyUI to release cached execution tensors (and optionally unload models)."""
    base = _base_url(base_url)
    try:
        _request_json(
            "POST",
            f"{base}/free",
            {"unload_models": bool(unload_models), "free_memory": bool(free_memory)},
            timeout=timeout,
        )
        return True
    except RuntimeError:
        return False


def clear_comfyui_history(
    base_url: str,
    *,
    prompt_ids: list[str] | None = None,
    clear_all: bool = False,
    timeout: int = 15,
) -> bool:
    """Remove completed history entries from ComfyUI RAM to prevent unbounded batch bloat."""
    base = _base_url(base_url)
    if clear_all:
        payload: dict = {"clear": True}
    elif prompt_ids:
        payload = {"delete": [str(pid) for pid in prompt_ids if pid]}
    else:
        return False
    try:
        _request_json("POST", f"{base}/history", payload, timeout=timeout)
        return True
    except RuntimeError:
        return False


def delete_comfyui_queue_items(
    base_url: str,
    *,
    prompt_ids: list[str] | None = None,
    clear_pending: bool = False,
    timeout: int = 15,
) -> bool:
    """Delete stuck or cancelled prompts from ComfyUI's pending queue."""
    base = _base_url(base_url)
    payload: dict = {}
    if prompt_ids:
        payload["delete"] = [str(pid) for pid in prompt_ids if pid]
    if clear_pending:
        payload["clear"] = True
    if not payload:
        return False
    try:
        _request_json("POST", f"{base}/queue", payload, timeout=timeout)
        return True
    except RuntimeError:
        return False


def get_system_stats(base_url: str, timeout: int = 15) -> dict:
    """Lightweight connectivity/device check via GET /system_stats (avoids heavy /object_info)."""
    return _request_json("GET", f"{_base_url(base_url)}/system_stats", timeout=timeout)


class _ComfyProgressMonitor:
    """Lightweight stdlib WebSocket listener for ComfyUI node/step progress and stall detection."""

    def __init__(self, base_url: str, client_id: str) -> None:
        self.base_url = _base_url(base_url)
        self.client_id = client_id
        self._stop = threading.Event()
        self._ready_event = threading.Event()
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connected = False
        self._prompt_id: str | None = None
        self._states: dict[str, dict] = {}
        self._buffer = bytearray()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float = 1.5) -> bool:
        """Wait briefly for the WebSocket handshake so early execution events are not missed."""
        if self._thread is None:
            return False
        self._ready_event.wait(timeout=max(0.0, float(timeout)))
        with self._lock:
            return self._connected

    def set_prompt_id(self, prompt_id: str) -> None:
        with self._lock:
            self._prompt_id = str(prompt_id)

    def close(self) -> None:
        self._stop.set()
        self._ready_event.set()
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def snapshot(self) -> dict:
        with self._lock:
            pid = self._prompt_id
            state = dict(self._states.get(pid or "", {})) if pid else {}
            state["ws_connected"] = self._connected
            return state

    def _run(self) -> None:
        parsed = urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        is_ssl = parsed.scheme == "https"
        port = parsed.port or (443 if is_ssl else 80)
        base_path = parsed.path.rstrip("/")
        ws_path = f"{base_path}/ws?clientId={quote(self.client_id)}"
        host_header = f"{host}:{port}" if parsed.port else host

        is_local = host in {"127.0.0.1", "localhost", "::1"}
        try:
            raw_sock = socket.create_connection((host, port), timeout=0.3 if is_local else 1.5)
            sock: socket.socket = (
                get_ssl_context(verify=True).wrap_socket(raw_sock, server_hostname=host)
                if is_ssl
                else raw_sock
            )
            sock.settimeout(1.0)
            with self._lock:
                if self._stop.is_set():
                    sock.close()
                    return
                self._sock = sock

            key = base64.b64encode(os.urandom(16)).decode("ascii")
            handshake = (
                f"GET {ws_path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
            sock.sendall(handshake)

            header_buf = bytearray()
            while b"\r\n\r\n" not in header_buf:
                if self._stop.is_set():
                    return
                chunk = sock.recv(4096)
                if not chunk:
                    return
                header_buf.extend(chunk)
                if len(header_buf) > 32768:
                    return
            head, rest = bytes(header_buf).split(b"\r\n\r\n", 1)
            status_line = head.split(b"\r\n", 1)[0]
            if b" 101 " not in status_line:
                return

            self._buffer = bytearray(rest)
            with self._lock:
                self._connected = True
            self._ready_event.set()

            fragments: list[bytes] = []
            while not self._stop.is_set():
                frame = self._read_frame(sock)
                if frame is None:
                    break
                fin, opcode, payload = frame
                if opcode == 0x8:  # Close
                    break
                if opcode == 0x9:  # Ping -> masked Pong
                    self._send_pong(sock, payload)
                    continue
                if opcode == 0xA:  # Pong
                    continue
                if opcode == 0x1:  # Text
                    if fin:
                        self._on_text(payload.decode("utf-8", errors="replace"))
                    else:
                        fragments = [payload]
                elif opcode == 0x0 and fragments:
                    fragments.append(payload)
                    if fin:
                        full = b"".join(fragments)
                        fragments.clear()
                        self._on_text(full.decode("utf-8", errors="replace"))
                elif opcode == 0x2:
                    # Binary preview frames also indicate ComfyUI is actively sampling
                    self._touch_active_prompt()
        except OSError:
            pass
        finally:
            self._ready_event.set()
            with self._lock:
                sock = self._sock
                self._sock = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _read_exact(self, sock: socket.socket, n: int) -> bytes | None:
        while len(self._buffer) < n:
            if self._stop.is_set():
                return None
            try:
                chunk = sock.recv(max(4096, n - len(self._buffer)))
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self._buffer.extend(chunk)
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    def _read_frame(self, sock: socket.socket) -> tuple[bool, int, bytes] | None:
        hdr = self._read_exact(sock, 2)
        if hdr is None:
            return None
        b1, b2 = hdr[0], hdr[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            ext = self._read_exact(sock, 2)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._read_exact(sock, 8)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]
        if length > 16 * 1024 * 1024:
            return None
        mask_key = self._read_exact(sock, 4) if masked else b""
        if masked and mask_key is None:
            return None
        payload = self._read_exact(sock, length) if length else b""
        if payload is None:
            return None
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    @staticmethod
    def _send_pong(sock: socket.socket, payload: bytes) -> None:
        trimmed = payload[:125]
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(trimmed))
        frame = bytes((0x8A, 0x80 | len(trimmed))) + mask + masked
        try:
            sock.sendall(frame)
        except OSError:
            pass

    def _touch_active_prompt(self) -> None:
        now = time.monotonic()
        with self._lock:
            pid = self._prompt_id
            if pid and pid in self._states:
                self._states[pid]["last_activity"] = now

    def _on_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        msg_type = str(msg.get("type") or "")
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        pid = data.get("prompt_id")
        if not pid:
            return
        pid_str = str(pid)
        now = time.monotonic()
        with self._lock:
            state = self._states.setdefault(
                pid_str,
                {
                    "saw_prompt": False,
                    "node": "",
                    "step": None,
                    "max_steps": None,
                    "completed": False,
                    "error": "",
                    "interrupted": False,
                    "last_activity": now,
                },
            )
            if msg_type == "execution_start":
                state["saw_prompt"] = True
                state["last_activity"] = now
            elif msg_type == "executing":
                state["saw_prompt"] = True
                node = data.get("node")
                if node is None:
                    state["completed"] = True
                    state["last_activity"] = now
                else:
                    node_str = str(node)
                    if state.get("node") != node_str:
                        state["node"] = node_str
                        state["step"] = None
                        state["max_steps"] = None
                    state["last_activity"] = now
            elif msg_type == "execution_cached":
                state["saw_prompt"] = True
                state["last_activity"] = now
            elif msg_type == "progress":
                state["saw_prompt"] = True
                if data.get("node"):
                    state["node"] = str(data["node"])
                try:
                    val = int(data.get("value") or 0)
                    mx = int(data.get("max") or 0)
                except (TypeError, ValueError):
                    val, mx = 0, 0
                if mx > 0:
                    state["step"] = val
                    state["max_steps"] = mx
                state["last_activity"] = now
            elif msg_type == "progress_state":
                state["saw_prompt"] = True
                nodes = data.get("nodes")
                if isinstance(nodes, dict):
                    for n_id, n_info in nodes.items():
                        if not isinstance(n_info, dict):
                            continue
                        if str(n_info.get("state") or "") == "running":
                            state["node"] = str(n_id)
                            try:
                                val = int(n_info.get("value") or 0)
                                mx = int(n_info.get("max") or 0)
                            except (TypeError, ValueError):
                                val, mx = 0, 0
                            if mx > 0:
                                state["step"] = val
                                state["max_steps"] = mx
                state["last_activity"] = now
            elif msg_type == "execution_success":
                state["saw_prompt"] = True
                state["completed"] = True
                state["last_activity"] = now
            elif msg_type == "execution_interrupted":
                state["saw_prompt"] = True
                state["interrupted"] = True
                state["last_activity"] = now
            elif msg_type == "execution_error":
                state["saw_prompt"] = True
                detail = str(data.get("exception_message") or data.get("node_type") or "ComfyUI execution error")
                state["error"] = detail
                state["last_activity"] = now


def _describe_workflow_stage(workflow: dict, node_id: str) -> tuple[str, str]:
    """Map ComfyUI node ID to a (stage_key, Vietnamese description) pair."""
    node = workflow.get(str(node_id)) if node_id else None
    class_type = str(node.get("class_type") or "") if isinstance(node, dict) else ""
    if class_type in {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}:
        return "sample", "đang chạy trên GPU"
    if class_type == "VAEDecode":
        return "vae", "đang giải mã video (VAE Decode)"
    if class_type in {"CreateVideo", "SaveVideo", "VHS_VideoCombine"}:
        return "encode", "đang đóng gói file video"
    if class_type:
        return "prepare", f"đang nạp model & encode ({class_type})"
    return "sample", "đang chạy trên GPU"


def generate_local_image_to_video(
    comfyui_url: str,
    image_path: str,
    prompt: str,
    output_path: str,
    workflow_path: str = "",
    width: int = 1280,
    height: int = 704,
    length: int = 81,
    steps: int = 20,
    cfg: float = 5.0,
    seed: int | None = None,
    negative_prompt: str = "",
    wan_variant: str = WAN_VARIANT_QUALITY,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    timeout_seconds: int | None = None,
    stall_timeout_seconds: int | None = None,
    pending_timeout_seconds: int | None = None,
    verify_server: bool = True,
    cleanup_history: bool = False,
) -> str:
    wan_variant = normalize_wan_variant(wan_variant)
    dmd = wan_variant == WAN_VARIANT_DMD
    if dmd:
        steps, cfg = 4, 1.0
    elif wan_variant in {LOCAL_MODEL_LTX, LOCAL_MODEL_HUNYUAN}:
        steps, cfg = 8, 1.0
    source = Path(image_path)
    if not source.is_file():
        raise ValueError(f"Không tìm thấy ảnh đầu vào: {image_path}")
    if not prompt.strip():
        raise ValueError("Prompt chuyển động local đang trống.")
    _validate_video_settings(width, height, length, steps, cfg, wan_variant)
    effective_timeout = int(
        timeout_seconds
        if timeout_seconds is not None
        else recommended_video_timeout(wan_variant, width, height, length, steps)
    )
    effective_timeout = max(30, effective_timeout)
    default_stall = recommended_stall_timeout(wan_variant, width, height)
    effective_stall = min(
        effective_timeout,
        max(30, int(stall_timeout_seconds if stall_timeout_seconds is not None else default_stall)),
    )
    effective_pending = min(
        effective_timeout,
        max(
            30,
            int(pending_timeout_seconds if pending_timeout_seconds is not None else min(effective_stall, 600)),
        ),
    )

    base = _base_url(comfyui_url)
    progress = progress or (lambda _value, _message: None)
    cancelled = cancelled or (lambda: False)
    variant_label = local_model_label(wan_variant)
    if verify_server:
        progress(2, f"Đang kiểm tra ComfyUI localhost và {variant_label}…")
        check_comfyui(base, require_default_models=not bool(workflow_path), wan_variant=wan_variant)
    else:
        progress(2, f"Đang kiểm tra kết nối ComfyUI cho {variant_label}…")
        get_system_stats(base, timeout=15)
    _raise_if_cancelled(cancelled, base)
    progress(5, f"Đang tải ảnh {source.name} vào ComfyUI…")
    uploaded = _upload_image(base, source)
    prefix = "video/VisualLoopStudio_" + re.sub(r"[^A-Za-z0-9_-]+", "_", source.stem)[:60]
    workflow = prepare_workflow(
        workflow_path, uploaded, prompt, width, height, length, steps, cfg,
        seed, negative_prompt, prefix, wan_variant,
    )
    _assert_local_only(workflow)
    _raise_if_cancelled(cancelled, base)
    client_id = uuid.uuid4().hex
    monitor = _ComfyProgressMonitor(base, client_id)
    monitor.start()
    monitor.wait_ready(timeout=1.5)
    prompt_id: str | None = None
    try:
        response = _request_json("POST", f"{base}/prompt", {"prompt": workflow, "client_id": client_id}, timeout=60)
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI không trả về prompt_id: {response}")
        monitor.set_prompt_id(str(prompt_id))
        progress(8, f"Đã xếp hàng {variant_label}; đang tạo video local…")
        started = time.monotonic()
        pending_started: float | None = None
        running_started: float | None = None
        last_pct = 8
        missing_queue_polls = 0
        poll_errors = 0

        def _cleanup_aborted_prompt(unload: bool = False) -> None:
            _interrupt(base)
            if prompt_id:
                delete_comfyui_queue_items(base, prompt_ids=[str(prompt_id)])
            if unload:
                free_comfyui_memory(base, unload_models=True, free_memory=True)

        while True:
            if cancelled():
                _cleanup_aborted_prompt(unload=False)
                raise LocalGenerationCancelled()

            now = time.monotonic()
            elapsed = now - started
            if elapsed > effective_timeout:
                _cleanup_aborted_prompt(unload=True)
                raise TimeoutError(
                    f"ComfyUI vượt quá thời gian chờ tối đa ({max(1, round(effective_timeout / 60))} phút) cho một video."
                )

            try:
                history = _request_json("GET", f"{base}/history/{prompt_id}", timeout=30)
                poll_errors = 0
            except RuntimeError as exc:
                poll_errors += 1
                if poll_errors >= 5:
                    _cleanup_aborted_prompt(unload=True)
                    raise RuntimeError(
                        f"Mất kết nối liên tiếp tới ComfyUI trong lúc chờ render ({poll_errors} lần): {exc}"
                    ) from exc
                progress(
                    last_pct,
                    f"{variant_label}: ComfyUI đang bận, thử kết nối lại ({poll_errors}/5)…",
                )
                time.sleep(2)
                continue

            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if entry:
                status = entry.get("status") or {}
                status_text = str(status.get("status_str") or "").lower()
                completed = bool(status.get("completed")) or status_text in {"success", "error", "failed"}
                if completed:
                    if status_text not in {"", "success"}:
                        raise RuntimeError(_history_error(entry))
                    file_info = _find_video_file(entry.get("outputs") or {})
                    if not file_info:
                        raise RuntimeError("Workflow đã hoàn tất nhưng không tìm thấy video output. Hãy dùng node SaveVideo.")
                    progress(94, "Đang tải video từ ComfyUI về dự án…")
                    _download_output(base, file_info, Path(output_path), cancelled)
                    if cleanup_history:
                        clear_comfyui_history(base, prompt_ids=[str(prompt_id)])
                    progress(100, f"Hoàn tất video {variant_label} local.")
                    return str(Path(output_path).resolve())

            ws = monitor.snapshot()
            if ws.get("error"):
                _cleanup_aborted_prompt(unload=False)
                raise RuntimeError(str(ws["error"]))
            if ws.get("interrupted"):
                _cleanup_aborted_prompt(unload=False)
                raise RuntimeError("Tác vụ AI Local trên ComfyUI đã bị ngắt trước khi hoàn tất.")

            queue_data = get_comfyui_queue(base, timeout=15)
            is_pending = False
            if isinstance(queue_data, dict) and (
                "queue_running" in queue_data or "queue_pending" in queue_data
            ):
                running_ids, pending_ids = _extract_queue_prompt_ids(queue_data)
                pid_str = str(prompt_id)
                if pid_str in running_ids:
                    missing_queue_polls = 0
                    pending_started = None
                    if running_started is None:
                        running_started = now
                elif pid_str in pending_ids:
                    missing_queue_polls = 0
                    is_pending = True
                    if pending_started is None:
                        pending_started = now
                    elif (now - pending_started) > effective_pending:
                        delete_comfyui_queue_items(base, prompt_ids=[pid_str])
                        pending_mins = max(1, round((now - pending_started) / 60))
                        raise TimeoutError(
                            f"Tác vụ {variant_label} đang chờ trong hàng đợi ComfyUI quá {pending_mins} phút "
                            "(tác vụ trước trên GPU có thể đã bị treo)."
                        )
                else:
                    missing_queue_polls += 1
                    if missing_queue_polls >= 3:
                        # Final check in case history flushed between /history and /queue reads.
                        final_hist = _request_json("GET", f"{base}/history/{prompt_id}", timeout=15)
                        if not (isinstance(final_hist, dict) and final_hist.get(prompt_id)):
                            raise RuntimeError(
                                "Tác vụ đã biến mất khỏi hàng đợi ComfyUI mà không có kết quả (có thể GPU/ComfyUI vừa tự hủy hoặc gặp lỗi OOM)."
                            )

            if ws.get("saw_prompt") and not is_pending and not ws.get("completed"):
                idle = now - float(ws.get("last_activity") or now)
                if idle > effective_stall:
                    _stage_k, stage_desc = _describe_workflow_stage(workflow, str(ws.get("node") or ""))
                    _cleanup_aborted_prompt(unload=True)
                    raise TimeoutError(
                        f"ComfyUI bị treo quá {max(1, round(effective_stall / 60))} phút không tiến triển ({stage_desc})."
                    )
            elif (
                ws.get("ws_connected")
                and not is_pending
                and not ws.get("completed")
                and running_started is not None
                and (now - running_started) > effective_stall
            ):
                _cleanup_aborted_prompt(unload=True)
                raise TimeoutError(
                    f"ComfyUI bị treo quá {max(1, round(effective_stall / 60))} phút không khởi chạy bước đầu tiên trên GPU."
                )

            minutes = elapsed / 60
            if is_pending:
                last_pct = 9
                msg = f"{variant_label} đang chờ trong hàng đợi ComfyUI… {minutes:.1f} phút"
            elif ws.get("saw_prompt"):
                stage_key, stage_desc = _describe_workflow_stage(workflow, str(ws.get("node") or ""))
                step = ws.get("step")
                max_steps = ws.get("max_steps")
                if stage_key == "prepare":
                    last_pct = max(last_pct, 12)
                    msg = f"{variant_label} {stage_desc}… {minutes:.1f} phút"
                elif stage_key == "sample":
                    if isinstance(step, int) and isinstance(max_steps, int) and max_steps > 0:
                        last_pct = max(last_pct, min(82, 15 + int(65 * step / max_steps)))
                        msg = f"{variant_label} đang chạy trên GPU (bước {step}/{max_steps})… {minutes:.1f} phút"
                    else:
                        last_pct = max(last_pct, min(80, 15 + int(minutes * 6)))
                        msg = f"{variant_label} đang chạy trên GPU… {minutes:.1f} phút"
                elif stage_key == "vae":
                    last_pct = max(last_pct, 86)
                    msg = f"{variant_label} {stage_desc}… {minutes:.1f} phút"
                else:
                    last_pct = max(last_pct, 92)
                    msg = f"{variant_label} {stage_desc}… {minutes:.1f} phút"
            else:
                last_pct = min(89, 10 + int(minutes * 4))
                msg = f"{variant_label} đang chạy trên GPU… {minutes:.1f} phút"
            progress(last_pct, msg)
            time.sleep(2)
    finally:
        monitor.close()


def normalize_wan_variant(value: str) -> str:
    normalized = str(value or WAN_VARIANT_QUALITY).strip().casefold()
    # Migrate projects saved by the removed non-commercial Turbo option to DMD.
    aliases = {
        WAN_VARIANT_DMD: WAN_VARIANT_DMD,
        "dmd": WAN_VARIANT_DMD,
        "turbo": WAN_VARIANT_DMD,
        LOCAL_MODEL_LTX: LOCAL_MODEL_LTX,
        "ltx": LOCAL_MODEL_LTX,
        "ltx-video": LOCAL_MODEL_LTX,
        LOCAL_MODEL_HUNYUAN: LOCAL_MODEL_HUNYUAN,
        "hunyuan": LOCAL_MODEL_HUNYUAN,
        "hunyuan1.5": LOCAL_MODEL_HUNYUAN,
    }
    return aliases.get(normalized, WAN_VARIANT_QUALITY)


def normalize_local_model(value: str) -> str:
    """Preferred generic name; normalize_wan_variant remains for saved-project compatibility."""
    return normalize_wan_variant(value)


def local_model_label(value: str) -> str:
    labels = {
        WAN_VARIANT_QUALITY: "Wan 2.2 Chất lượng 20 bước",
        WAN_VARIANT_DMD: "Wan 2.2 DMD 4 bước",
        LOCAL_MODEL_LTX: "LTX-Video 2B Distilled 8 bước",
        LOCAL_MODEL_HUNYUAN: "HunyuanVideo 1.5 480p 8 bước",
    }
    return labels[normalize_wan_variant(value)]


def _inject_workflow_values(
    workflow: dict,
    uploaded_image: str,
    prompt: str,
    width: int,
    height: int,
    length: int,
    steps: int,
    cfg: float,
    seed: int | None,
    negative_prompt: str,
    output_prefix: str,
) -> None:
    _validate_video_settings(width, height, length, steps, cfg)
    clips = [(node_id, node) for node_id, node in workflow.items() if node.get("class_type") == "CLIPTextEncode"]
    load_images = [node for node in workflow.values() if node.get("class_type") == "LoadImage"]
    latents = [node for node in workflow.values() if node.get("class_type") == "Wan22ImageToVideoLatent"]
    outputs = [node for node in workflow.values() if node.get("class_type") in {"SaveVideo", "VHS_VideoCombine"}]
    if not load_images or not clips or not latents or not outputs:
        raise ValueError("Workflow API cần có LoadImage, CLIPTextEncode, Wan22ImageToVideoLatent và SaveVideo.")
    load_images[0].setdefault("inputs", {})["image"] = uploaded_image
    positive = next((node for _node_id, node in clips if "positive" in _node_title(node)), clips[0][1])
    negative = next((node for _node_id, node in clips if "negative" in _node_title(node)), clips[1][1] if len(clips) > 1 else None)
    positive.setdefault("inputs", {})["text"] = motion_prompt(prompt)
    if negative:
        negative.setdefault("inputs", {})["text"] = negative_prompt.strip() or DEFAULT_NEGATIVE_PROMPT
    for node in latents:
        inputs = node.setdefault("inputs", {})
        inputs.update({"width": int(width), "height": int(height), "length": int(length), "batch_size": 1})
    actual_seed = random.SystemRandom().randrange(0, 2**63 - 1) if seed is None or seed < 0 else int(seed)
    for node in workflow.values():
        inputs = node.setdefault("inputs", {})
        if node.get("class_type") in {"KSampler", "KSamplerAdvanced"}:
            if "seed" in inputs:
                inputs["seed"] = actual_seed
            if "noise_seed" in inputs:
                inputs["noise_seed"] = actual_seed
            if "steps" in inputs:
                inputs["steps"] = int(steps)
            if "cfg" in inputs:
                inputs["cfg"] = float(cfg)
        if node.get("class_type") == "CreateVideo" and "fps" in inputs:
            inputs["fps"] = 24.0
        if node.get("class_type") in {"SaveVideo", "VHS_VideoCombine"}:
            if "filename_prefix" in inputs:
                inputs["filename_prefix"] = output_prefix


def _assert_local_only(workflow: dict) -> None:
    cloud_nodes = []
    for node in workflow.values():
        class_type = str(node.get("class_type") or "")
        lower = class_type.lower()
        if lower.endswith("api") or "partner" in lower or "cloud" in lower:
            cloud_nodes.append(class_type)
    if cloud_nodes:
        raise ValueError(
            "Workflow chứa node API/Cloud có thể tính credit: " + ", ".join(sorted(set(cloud_nodes)))
            + ". Hãy dùng workflow AI Video local tích hợp."
        )


def _validate_video_settings(
    width: int,
    height: int,
    length: int,
    steps: int,
    cfg: float,
    model_variant: str = WAN_VARIANT_QUALITY,
) -> None:
    model_variant = normalize_wan_variant(model_variant)
    if width < 256 or height < 256 or width % 32 or height % 32:
        raise ValueError("Kích thước AI local phải từ 256 px và chia hết cho 32.")
    frame_multiple = 8 if model_variant == LOCAL_MODEL_LTX else 4
    if length < 5 or (length - 1) % frame_multiple:
        raise ValueError(
            f"Số frame {local_model_label(model_variant)} phải có dạng "
            f"{frame_multiple}n+1, ví dụ 49, 81 hoặc 121."
        )
    if not 1 <= steps <= 100:
        raise ValueError("Số bước AI local phải từ 1 đến 100.")
    if not 0 < cfg <= 30:
        raise ValueError("CFG AI local phải lớn hơn 0 và không quá 30.")


def _input_choices(object_info: dict, node_name: str, input_name: str) -> list:
    try:
        spec = object_info[node_name]["input"]["required"][input_name]
        return list(spec[0]) if spec and isinstance(spec[0], list) else []
    except (KeyError, IndexError, TypeError):
        return []


def _node_title(node: dict) -> str:
    meta = node.get("_meta") or {}
    return str(meta.get("title") or "").lower()


def _base_url(value: str) -> str:
    value = (value or "http://127.0.0.1:8188").strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value


def _request_json(method: str, url: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method, headers={"User-Agent": "VisualLoopStudio/1", "Accept": "application/json"})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI HTTP {exc.code}: {detail[:1200]}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Không kết nối được ComfyUI tại {url}: {exc}") from exc
    try:
        return json.loads(content.decode("utf-8")) if content else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ComfyUI trả dữ liệu không hợp lệ từ {url}") from exc


def _upload_image(base: str, source: Path) -> str:
    boundary = "----VisualLoopStudio" + uuid.uuid4().hex
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{source.name}\"\r\nContent-Type: {mime}\r\n\r\n".encode("utf-8"),
        source.read_bytes(),
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput".encode("utf-8"),
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue".encode("utf-8"),
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    request = Request(f"{base}/upload/image", data=b"".join(parts), method="POST")
    request.add_header("User-Agent", "VisualLoopStudio/1")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Không upload được ảnh vào ComfyUI (HTTP {exc.code}): {detail[:1000]}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Không upload được ảnh vào ComfyUI: {exc}") from exc
    name = str(result.get("name") or "")
    subfolder = str(result.get("subfolder") or "").strip("/\\")
    if not name:
        raise RuntimeError(f"ComfyUI không trả về tên ảnh upload: {result}")
    return f"{subfolder}/{name}" if subfolder else name


def _find_video_file(outputs: object) -> dict | None:
    candidates: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("filename"):
                candidates.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(outputs)
    video_suffixes = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
    return next((item for item in candidates if Path(str(item.get("filename"))).suffix.lower() in video_suffixes), candidates[0] if candidates else None)


def _download_output(base: str, file_info: dict, output: Path, cancelled: Callable[[], bool]) -> None:
    query = urlencode({
        "filename": file_info.get("filename", ""),
        "subfolder": file_info.get("subfolder", ""),
        "type": file_info.get("type", "output"),
    })
    request = Request(f"{base}/view?{query}", headers={"User-Agent": "VisualLoopStudio/1"})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    try:
        with urlopen(request, timeout=120) as response, open(temporary, "wb") as stream:
            while True:
                if cancelled():
                    raise LocalGenerationCancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _history_error(entry: dict) -> str:
    messages = entry.get("status", {}).get("messages") or []
    for message in reversed(messages):
        if isinstance(message, (list, tuple)) and message and str(message[0]).lower() == "execution_error":
            detail = message[1] if len(message) > 1 else message[0]
            if isinstance(detail, dict):
                return str(detail.get("exception_message") or detail.get("node_type") or detail)
            return str(detail)
    return "ComfyUI không thể hoàn tất workflow AI local."


def _interrupt(base: str) -> None:
    try:
        _request_json("POST", f"{base}/interrupt", {}, timeout=10)
    except RuntimeError:
        pass


def _raise_if_cancelled(cancelled: Callable[[], bool], base: str) -> None:
    if cancelled():
        _interrupt(base)
        raise LocalGenerationCancelled()
