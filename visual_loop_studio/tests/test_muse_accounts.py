import os
import unittest
from PySide6.QtWidgets import QApplication

from models.settings_model import AppSettings
from ui.muse_accounts import MuseAccountsPage


class TestMuseAccountsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        cls.app = QApplication.instance() or QApplication([])

    def test_badges_and_prompt_counting(self):
        settings = AppSettings()
        page = MuseAccountsPage(settings)
        self.assertEqual(page.badge_total.text(), "📋 Tổng 0")
        self.assertEqual(page.badge_done.text(), "✔ Done 0")
        self.assertEqual(page.badge_error.text(), "✖ Lỗi 0")
        self.assertEqual(page.badge_remaining.text(), "⏳ Còn 0")

        # Set 3 lines of prompt
        page.prompt.setPlainText("prompt 1\nprompt 2\nprompt 3")
        self.assertEqual(page.badge_total.text(), "📋 Tổng 3")
        self.assertEqual(page.badge_done.text(), "✔ Done 0")
        self.assertEqual(page.badge_error.text(), "✖ Lỗi 0")
        self.assertEqual(page.badge_remaining.text(), "⏳ Còn 3")
        self.assertEqual(page.overall_progress.maximum(), 3)
        self.assertEqual(page.overall_progress.value(), 0)
        self.assertEqual(page.overall_progress.format(), "0 / 3 video (0.0%)")

        # Simulate batch stats progress
        page._on_batch_stats(3, 2, 1, 0)
        self.assertEqual(page.badge_total.text(), "📋 Tổng 3")
        self.assertEqual(page.badge_done.text(), "✔ Done 2")
        self.assertEqual(page.badge_error.text(), "✖ Lỗi 1")
        self.assertEqual(page.badge_remaining.text(), "⏳ Còn 0")
        self.assertEqual(page.overall_progress.maximum(), 3)
        self.assertEqual(page.overall_progress.value(), 3)
        self.assertEqual(page.overall_progress.format(), "3 / 3 video (100.0%)")

        # Simulate larger batch progress (e.g., 24 out of 2500 videos)
        page._on_batch_stats(2500, 24, 0, 2476)
        self.assertEqual(page.overall_progress.maximum(), 2500)
        self.assertEqual(page.overall_progress.value(), 24)
        self.assertEqual(page.overall_progress.format(), "24 / 2,500 video (1.0%)")

    def test_f5_spin_and_settings(self):
        settings = AppSettings(muse_auto_refresh_minutes=45)
        page = MuseAccountsPage(settings)
        self.assertEqual(page.f5_spin.value(), 45)

        page.f5_spin.setValue(90)
        self.assertEqual(settings.muse_auto_refresh_minutes, 90)


if __name__ == "__main__":
    unittest.main()
