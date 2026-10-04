from __future__ import annotations

from ai.providers.base import VideoProvider
from ai.providers.byteplus_seedance import BytePlusSeedanceProvider
from ai.providers.comfyui_provider import ComfyUiProvider
from ai.providers.muse_web_provider import MuseWebProvider
from ai.providers.veo_provider import VeoProvider
from models.settings_model import AppSettings


def create_provider_registry(settings: AppSettings) -> dict[str, VideoProvider]:
    providers: tuple[VideoProvider, ...] = (
        ComfyUiProvider(settings.comfyui_url, settings.comfyui_workflow),
        VeoProvider(settings.gemini_api_key),
        BytePlusSeedanceProvider(settings.byteplus_las_base_url),
        MuseWebProvider(settings.muse_start_url),
    )
    return {provider.provider_id: provider for provider in providers}
