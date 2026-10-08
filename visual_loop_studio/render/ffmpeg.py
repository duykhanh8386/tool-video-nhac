from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from audio.duration import main_audio_duration
from models.loop_project import LoopProject
from models.settings_model import AppSettings
from models.visual_project import VisualProject
from render.ffprobe import probe_media
from render.nvenc import resolve_encoder
from utils.media import existing_file
from utils.paths import CACHE_DIR, ensure_app_dirs, unique_output
from utils.process import hidden_process_kwargs
from visual.compositor import build_visual_graph


@dataclass
class RenderJob:
    command: list[str]
    output: Path
    duration: float
    label: str


def _filter_script(value: str) -> str:
    """Keep large per-character animation graphs off the Windows command line."""
    ensure_app_dirs()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    path = CACHE_DIR / f"filter_{digest}.txt"
    if not path.exists():
        path.write_text(value, encoding="utf-8")
    return str(path)


def _video_encoding(encoder: str) -> list[str]:
    if encoder == "libx264":
        return ["-c:v", encoder, "-preset", "veryfast", "-crf", "20", "-threads", "0"]
    if encoder == "hevc_nvenc":
        return ["-c:v", encoder, "-preset", "p4", "-cq", "22", "-tag:v", "hvc1"]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p4", "-cq", "21"]
    if encoder == "h264_qsv":
        return ["-c:v", encoder, "-preset", "veryfast", "-global_quality", "22"]
    if encoder == "h264_amf":
        return ["-c:v", encoder, "-quality", "speed", "-rc", "cqp", "-qp_p", "22", "-qp_i", "22"]
    if encoder == "h264_mf":
        return ["-c:v", encoder, "-b:v", "8M"]
    return ["-c:v", encoder, "-preset", "p4", "-cq", "21"]


def build_visual_job(project: VisualProject, settings: AppSettings) -> RenderJob:
    project.background = existing_file(project.background, "background")
    for attr, label in (("font_file", "font"), ("artwork", "artwork"), ("logo", "logo"), ("platform_icons", "platform icons"), ("audio", "audio"), ("waveform_media", "file sóng"), ("effect_overlay", "overlay toàn cảnh"), ("lut", "LUT")):
        value = getattr(project, attr)
        if value:
            setattr(project, attr, existing_file(value, label))
    background_info = probe_media(project.background, settings.ffprobe_path)
    if not background_info.has_video:
        raise ValueError("Background không có video/image stream hợp lệ.")
    if project.audio and not probe_media(project.audio, settings.ffprobe_path).has_audio:
        raise ValueError("Reactive audio không có audio stream.")
    if not project.output_folder:
        raise ValueError("Chưa chọn thư mục output.")
    try:
        width, height = (int(value) for value in project.resolution.split("x", 1))
    except (ValueError, AttributeError):
        width, height = 1920, 1080
    encoder = resolve_encoder(project.encoder, settings.ffmpeg_path)
    graph = build_visual_graph(project, width, height)
    output = unique_output(project.output_folder, project.output_name, "visual", ".mp4")
    command = [
        settings.ffmpeg_path, "-hide_banner", "-y", *graph.inputs,
        "-filter_complex_script", _filter_script(graph.filter_complex), "-map", graph.video_map,
    ]
    if graph.audio_map:
        command += ["-map", graph.audio_map, "-c:a", "aac", "-b:a", "320k"]
    command += [*_video_encoding(encoder), "-r", str(project.fps), "-t", "60.000", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
    return RenderJob(command, output, 60.0, "Visual 60 giây")


def prepare_seamless_video(video_path: str, duration: float, ffmpeg: str) -> str:
    """Creates a seamless crossfade loop clip so that each loop cycle transitions smoothly without hard cuts."""
    if duration < 2.0:
        return video_path
    ensure_app_dirs()
    trans = min(1.0, duration / 4.0)
    stem = Path(video_path).stem
    try:
        mtime = int(Path(video_path).stat().st_mtime)
    except Exception:
        mtime = 0
    cache_file = CACHE_DIR / f"seamless_v_{stem}_{mtime}_{int(trans * 1000)}.mp4"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return str(cache_file)

    body_end = duration - trans
    filter_expr = (
        f"[0:v]split=3[v_body][v_head][v_tail]; "
        f"[v_body]trim=start={trans:.3f}:end={body_end:.3f},setpts=PTS-STARTPTS[body]; "
        f"[v_head]trim=start={body_end:.3f}:end={duration:.3f},setpts=PTS-STARTPTS[head]; "
        f"[v_tail]trim=start=0:end={trans:.3f},setpts=PTS-STARTPTS[tail]; "
        f"[head][tail]xfade=transition=fade:duration={trans:.3f}:offset=0[trans]; "
        f"[body][trans]concat=n=2:v=1:a=0[vout]"
    )
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", video_path,
        "-filter_complex", filter_expr, "-map", "[vout]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        str(cache_file),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60, check=False, **hidden_process_kwargs())
        if res.returncode == 0 and cache_file.exists() and cache_file.stat().st_size > 0:
            return str(cache_file)
    except Exception:
        pass
    return video_path


def prepare_seamless_audio(audio_path: str, duration: float, ffmpeg: str) -> str:
    """Creates a seamless crossfade loop clip so that each audio cycle transitions smoothly without hard cuts."""
    if duration < 4.0:
        return audio_path
    ensure_app_dirs()
    trans = min(1.5, duration / 8.0)
    stem = Path(audio_path).stem
    try:
        mtime = int(Path(audio_path).stat().st_mtime)
    except Exception:
        mtime = 0
    cache_file = CACHE_DIR / f"seamless_a_{stem}_{mtime}_{int(trans * 1000)}.wav"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return str(cache_file)

    body_end = duration - trans
    filter_expr = (
        f"[0:a]asplit=3[a_body][a_head][a_tail]; "
        f"[a_body]atrim=start={trans:.3f}:end={body_end:.3f},asetpts=PTS-STARTPTS[body]; "
        f"[a_head]atrim=start={body_end:.3f}:end={duration:.3f},asetpts=PTS-STARTPTS[head]; "
        f"[a_tail]atrim=start=0:end={trans:.3f},asetpts=PTS-STARTPTS[tail]; "
        f"[head][tail]acrossfade=d={trans:.3f}:c1=tri:c2=tri[trans]; "
        f"[body][trans]concat=n=2:v=0:a=1[aout]"
    )
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", audio_path,
        "-filter_complex", filter_expr, "-map", "[aout]",
        str(cache_file),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60, check=False, **hidden_process_kwargs())
        if res.returncode == 0 and cache_file.exists() and cache_file.stat().st_size > 0:
            return str(cache_file)
    except Exception:
        pass
    return audio_path


def build_loop_job(project: LoopProject, settings: AppSettings) -> RenderJob:
    project.video = existing_file(project.video, "video")
    project.main_audio = existing_file(project.main_audio, "main music")
    if project.background_audio:
        project.background_audio = existing_file(project.background_audio, "background music")
    if not project.output_folder:
        raise ValueError("Chưa chọn thư mục output.")
    video_info = probe_media(project.video, settings.ffprobe_path)
    if not video_info.has_video:
        raise ValueError("File video không có video stream.")
    audio_duration = main_audio_duration(project.main_audio, settings.ffprobe_path)
    if project.duration_mode == "custom" and project.custom_duration > 0:
        duration = float(project.custom_duration)
    else:
        duration = audio_duration
    if project.background_audio and not probe_media(project.background_audio, settings.ffprobe_path).has_audio:
        raise ValueError("Background music không có audio stream.")
    encoder = resolve_encoder(project.encoder, settings.ffmpeg_path)
    output = unique_output(project.output_folder, project.output_name, "final", ".mp4")

    video_input = project.video
    if project.seamless_video and video_info.duration and duration > video_info.duration and video_info.duration >= 2.0:
        video_input = prepare_seamless_video(project.video, video_info.duration, settings.ffmpeg_path)

    audio_input = project.main_audio
    if project.seamless_video and audio_duration and duration > audio_duration and audio_duration >= 4.0:
        audio_input = prepare_seamless_audio(project.main_audio, audio_duration, settings.ffmpeg_path)

    command = [settings.ffmpeg_path, "-hide_banner", "-y", "-stream_loop", "-1", "-i", video_input]
    if project.duration_mode == "custom" and duration > audio_duration:
        command += ["-stream_loop", "-1"]
    command += ["-i", audio_input]
    if project.background_audio:
        if project.loop_background:
            command += ["-stream_loop", "-1"]
        command += ["-i", project.background_audio]

    video_filters: list[str] = []
    if project.resolution != "Keep source":
        width, height = project.resolution.split("x", 1)
        video_filters += [f"scale={width}:{height}:force_original_aspect_ratio=decrease", f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"]
    if project.fps != "Keep source":
        video_filters.append(f"fps={project.fps}")
    video_filters += ["setpts=PTS-STARTPTS", "format=yuv420p"]
    if duration >= 4.0:
        fade_st = max(0.0, duration - 1.5)
        video_filters.append(f"fade=t=out:st={fade_st:.3f}:d=1.5")

    audio_filters = [f"[1:a]volume={project.main_volume:.4f}[main]"]
    audio_map = "[main]"
    if project.background_audio:
        bg_chain = f"[2:a]volume={project.background_volume:.4f}"
        if project.background_crossfade and project.crossfade_ms:
            fade = project.crossfade_ms / 1000
            # Softens the initial boundary; stream_loop keeps memory use constant for long outputs.
            bg_chain += f",afade=t=in:st=0:d={fade:.3f}"
        bg_chain += f",atrim=duration={duration:.6f}[bg]"
        audio_filters.append(bg_chain)
        mix = "[main][bg]amix=inputs=2:duration=first:dropout_transition=2"
        if project.normalize:
            mix += ",loudnorm=I=-14:LRA=11:TP=-1.5"
        if duration >= 4.0:
            fade_st = max(0.0, duration - 1.5)
            mix += f",afade=t=out:st={fade_st:.3f}:d=1.5"
        audio_filters.append(mix + "[mix]")
        audio_map = "[mix]"
    elif project.normalize:
        audio_filters.append(f"[main]loudnorm=I=-14:LRA=11:TP=-1.5" + (f",afade=t=out:st={max(0.0, duration - 1.5):.3f}:d=1.5" if duration >= 4.0 else "") + "[mix]")
        audio_map = "[mix]"
    elif duration >= 4.0:
        fade_st = max(0.0, duration - 1.5)
        audio_filters.append(f"[main]afade=t=out:st={fade_st:.3f}:d=1.5[mix]")
        audio_map = "[mix]"

    filter_complex = f"[0:v]{','.join(video_filters)}[vout];" + ";".join(audio_filters)
    command += ["-filter_complex", filter_complex, "-map", "[vout]", "-map", audio_map]
    command += [*_video_encoding(encoder), "-c:a", "aac", "-b:a", "320k", "-t", f"{duration:.6f}", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
    return RenderJob(command, output, duration, "Loop Video + Music")


def build_audio_job(inputs: list[str], volumes: list[float], output_folder: str, output_name: str, normalize: bool, settings: AppSettings) -> RenderJob:
    if not inputs:
        raise ValueError("Chưa chọn main audio.")
    inputs = [existing_file(path, "audio") for path in inputs]
    if len(inputs) != len(volumes):
        raise ValueError("Số volume không khớp số track audio.")
    for path in inputs:
        if not probe_media(path, settings.ffprobe_path).has_audio:
            raise ValueError(f"File không có audio stream: {path}")
    duration = main_audio_duration(inputs[0], settings.ffprobe_path)
    if not output_folder:
        raise ValueError("Chưa chọn thư mục output.")
    suffix = Path(output_name).suffix.lower() or ".mp3"
    if suffix not in {".wav", ".flac", ".mp3", ".aac", ".m4a"}:
        suffix = ".mp3"
    base_name = Path(output_name).stem if output_name else "audio_mix"
    output = unique_output(output_folder, base_name + suffix, "audio_mix")
    command = [settings.ffmpeg_path, "-hide_banner", "-y", "-i", inputs[0]]
    for path in inputs[1:]:
        command += ["-stream_loop", "-1", "-i", path]
    chains = [f"[{index}:a]volume={max(0, volumes[index]):.4f},atrim=duration={duration:.6f}[a{index}]" for index in range(len(inputs))]
    labels = "".join(f"[a{index}]" for index in range(len(inputs)))
    mix = f"{labels}amix=inputs={len(inputs)}:duration=first:dropout_transition=2"
    if normalize:
        mix += ",loudnorm=I=-14:LRA=11:TP=-1.5"
    chains.append(mix + "[aout]")
    codecs = {".wav": ["-c:a", "pcm_s24le"], ".flac": ["-c:a", "flac"], ".mp3": ["-c:a", "libmp3lame", "-b:a", "320k"], ".aac": ["-c:a", "aac", "-b:a", "320k"], ".m4a": ["-c:a", "aac", "-b:a", "320k"]}
    command += ["-filter_complex", ";".join(chains), "-map", "[aout]", *codecs[suffix], "-t", f"{duration:.6f}", "-progress", "pipe:1", "-nostats", str(output)]
    return RenderJob(command, output, duration, "Audio Mixer")
