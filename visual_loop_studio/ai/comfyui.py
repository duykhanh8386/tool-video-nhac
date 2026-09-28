from __future__ import annotations

import copy
import json
import mimetypes
import random
import re
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ai.veo import motion_prompt


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
    timeout_seconds: int = 4 * 60 * 60,
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
    base = _base_url(comfyui_url)
    progress = progress or (lambda _value, _message: None)
    cancelled = cancelled or (lambda: False)
    variant_label = local_model_label(wan_variant)
    progress(2, f"Đang kiểm tra ComfyUI localhost và {variant_label}…")
    check_comfyui(base, require_default_models=not bool(workflow_path), wan_variant=wan_variant)
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
    response = _request_json("POST", f"{base}/prompt", {"prompt": workflow, "client_id": client_id}, timeout=60)
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI không trả về prompt_id: {response}")
    progress(8, f"Đã xếp hàng {variant_label}; đang tạo video local…")
    started = time.monotonic()
    while True:
        _raise_if_cancelled(cancelled, base)
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            _interrupt(base)
            raise TimeoutError("ComfyUI vượt quá thời gian chờ 4 giờ cho một video.")
        history = _request_json("GET", f"{base}/history/{prompt_id}", timeout=30)
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
                progress(100, f"Hoàn tất video {variant_label} local.")
                return str(Path(output_path).resolve())
        minutes = elapsed / 60
        estimated = min(90, 10 + int(minutes * 4))
        progress(estimated, f"{variant_label} đang chạy trên GPU… {minutes:.1f} phút")
        time.sleep(2)


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
