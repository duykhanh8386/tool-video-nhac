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

    def test_page_instantiation_without_mocks(self):
        settings = AppSettings()
        page = MuseAccountsPage(settings)
        self.assertIsNotNone(page)
        self.assertIsNotNone(page.session_manager)
        self.assertIsNotNone(page.batch_manager)
        self.assertIsNotNone(page.cookie_store)


if __name__ == "__main__":
    unittest.main()
