from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional


class ProviderError(RuntimeError):
    """Lỗi chung của AI Provider."""
    pass


class CookieExpiredError(ProviderError):
    """Cookie của tài khoản AI (ví dụ: Muse) đã hết hạn."""
    pass


class IProvider(ABC):
    """Giao diện chuẩn cho tất cả các AI Provider (Wan Local, Muse Cookie, v.v.)."""

    @property
    @abstractmethod
    def key(self) -> str:
        """Định danh provider ('wan', 'muse')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Tên hiển thị thân thiện trên UI."""
        pass

    @abstractmethod
    def check_health(self) -> tuple[bool, str]:
        """Kiểm tra tính sẵn sàng của kết nối, tài khoản, hoặc service."""
        pass

    @abstractmethod
    def text_to_image(
        self,
        prompt: str,
        width: int = 1280,
        height: int = 704,
        seed: Optional[int] = None,
        cache_path: Optional[Path] = None,
        progress: Optional[Callable[[int, str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """
        Bước 1: Sinh ảnh tĩnh từ prompt.
        Trả về đường dẫn file ảnh .png.
        """
        pass

    @abstractmethod
    def image_to_video(
        self,
        image_path: Path,
        prompt: str,
        duration_seconds: int = 5,
        width: int = 1280,
        height: int = 704,
        output_path: Optional[Path] = None,
        progress: Optional[Callable[[int, str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """
        Bước 2: Tạo video chuyển động từ ảnh nguồn và prompt.
        Trả về đường dẫn file video .mp4.
        """
        pass
