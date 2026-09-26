from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
CAMERA_LOCK = (
    "Locked-off tripod camera. Preserve the exact original framing, composition, background, "
    "identity, clothing, anatomy, objects, colors and lighting. No camera movement, no zoom, "
    "no pan, no crop, no reframing, no scene change, no morphing and no flicker. Animate only "
    "the actions explicitly requested. Use subtle physically natural motion with stable anatomy "
    "and smooth temporal consistency. The final frame must match the first frame for a seamless loop."
)


class GenerationCancelled(RuntimeError):
    pass


def motion_prompt(user_prompt: str) -> str:
    prompt = user_prompt.strip()
    if not prompt:
        raise ValueError("Chưa nhập mô tả chuyển động cho video AI.")
    return f"{CAMERA_LOCK}\n\nRequested motion: {prompt}"


def build_request_payload(
    image_path: str | Path,
    prompt: str,
    aspect_ratio: str = "16:9",
    duration: int = 8,
    resolution: str = "720p",
) -> dict:
    source = Path(image_path)
    if not source.is_file():
        raise ValueError(f"Không tìm thấy ảnh nguồn: {source}")
    if duration not in {4, 6, 8}:
        raise ValueError("Veo chỉ hỗ trợ thời lượng 4, 6 hoặc 8 giây.")
    if resolution in {"1080p", "4k"} and duration != 8:
        raise ValueError("Veo yêu cầu thời lượng 8 giây khi chọn 1080p hoặc 4K.")
    mime_type = mimetypes.guess_type(source.name)[0] or "image/png"
    if not mime_type.startswith("image/"):
        raise ValueError("Tạo video AI cần một file ảnh làm khung hình nguồn.")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    frame = {"inlineData": {"mimeType": mime_type, "data": encoded}}
    return {
        "instances": [{
            "prompt": motion_prompt(prompt),
            "image": frame,
            "lastFrame": frame,
        }],
        "parameters": {
            "aspectRatio": aspect_ratio,
            "durationSeconds": duration,
            "resolution": resolution,
            "personGeneration": "allow_adult",
            "numberOfVideos": 1,
        },
    }


def _json_request(url: str, api_key: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "VisualLoopStudio/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except ValueError:
            message = detail
        raise RuntimeError(f"Gemini/Veo API trả lỗi {exc.code}: {message}") from exc


def _sleep_with_cancel(seconds: int, cancelled: Callable[[], bool]) -> None:
    for _ in range(seconds):
        if cancelled():
            raise GenerationCancelled("Đã hủy tạo video AI.")
        time.sleep(1)


def generate_image_to_video(
    api_key: str,
    image_path: str | Path,
    prompt: str,
    output_path: str | Path,
    model: str = "veo-3.1-fast-generate-preview",
    aspect_ratio: str = "16:9",
    duration: int = 8,
    resolution: str = "720p",
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    timeout_seconds: int = 12 * 60,
) -> Path:
    key = api_key.strip()
    if not key:
        raise ValueError("Chưa có Gemini API key. Hãy nhập key trong Cài đặt.")
    cancelled = cancelled or (lambda: False)
    progress = progress or (lambda _value, _message: None)
    payload = build_request_payload(image_path, prompt, aspect_ratio, duration, resolution)
    progress(5, "Đang gửi ảnh và prompt đến Veo…")
    operation = _json_request(f"{BASE_URL}/models/{model}:predictLongRunning", key, payload, timeout=120)
    operation_name = str(operation.get("name") or "").lstrip("/")
    if not operation_name:
        raise RuntimeError(f"Veo không trả về mã tác vụ: {operation}")

    started = time.monotonic()
    polls = 0
    while True:
        if cancelled():
            raise GenerationCancelled("Đã hủy tạo video AI.")
        status = _json_request(f"{BASE_URL}/{operation_name}", key, timeout=60)
        if status.get("done"):
            if status.get("error"):
                raise RuntimeError(str(status["error"].get("message") or status["error"]))
            samples = status.get("response", {}).get("generateVideoResponse", {}).get("generatedSamples", [])
            video_uri = str(samples[0].get("video", {}).get("uri", "")) if samples else ""
            if not video_uri:
                raise RuntimeError(f"Veo hoàn tất nhưng không trả về file video: {status}")
            break
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError("Veo mất quá nhiều thời gian. Tác vụ đã vượt giới hạn chờ 12 phút.")
        polls += 1
        progress(min(80, 12 + polls * 3), "Veo đang tạo chuyển động; có thể mất vài phút…")
        _sleep_with_cancel(10, cancelled)

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    progress(85, "Đang tải video AI về máy…")
    request = urllib.request.Request(video_uri, headers={"x-goog-api-key": key, "User-Agent": "VisualLoopStudio/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as stream:
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            while True:
                if cancelled():
                    raise GenerationCancelled("Đã hủy tải video AI.")
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
                downloaded += len(block)
                if total:
                    progress(85 + min(14, round(downloaded / total * 14)), "Đang tải video AI về máy…")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    progress(100, "Đã tạo video chuyển động AI.")
    return destination
