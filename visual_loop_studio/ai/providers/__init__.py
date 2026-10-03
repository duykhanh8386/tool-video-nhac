"""Pluggable AI video provider contracts and official/local adapters."""

from ai.providers.base import (
    GenerationRequest,
    ModelCapability,
    ProviderCancelled,
    ProviderError,
    ProviderExecutionContext,
    ProviderHealth,
    VideoProvider,
)

__all__ = [
    "GenerationRequest",
    "ModelCapability",
    "ProviderCancelled",
    "ProviderError",
    "ProviderExecutionContext",
    "ProviderHealth",
    "VideoProvider",
]
