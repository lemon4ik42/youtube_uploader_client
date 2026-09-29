import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.api_client import ApiClient
from src.models import AuthCallbackPayload, ChannelOut, ChannelTagOut
from src.widgets.auth_tags_dialog import AuthTagsDialog

APP = QApplication.instance() or QApplication([])


class AuthTagsTests(unittest.TestCase):
    def setUp(self):
        self.tag = ChannelTagOut(id=uuid4(), name="Gaming")
        self.channel = ChannelOut(id=uuid4(), youtube_channel_id="UCtest",
                                  created_at="2026-09-01T00:00:00Z",
                                  updated_at="2026-09-01T00:00:00Z", tags=[self.tag])
        self.worker = patch("src.widgets.auth_tags_dialog.run_api").start()
        self.addCleanup(patch.stopall)

    def dialog(self, existing=True):
        dialog = AuthTagsDialog(Mock(), self.channel if existing else None)
        self.addCleanup(dialog.deleteLater)
        dialog._loaded([self.tag])
        return dialog

    def test_preserve_replace_and_clear_are_distinct(self):
        dialog = self.dialog()
        self.assertIsNone(dialog.tag_ids())
        dialog._replace.setChecked(True)
        self.assertEqual(dialog.tag_ids(), [self.tag.id])
        dialog._list.item(0).setCheckState(Qt.Unchecked)
        self.assertEqual(dialog.tag_ids(), [])
        dialog._replace.setChecked(False)
        self.assertIsNone(dialog.tag_ids())

    def test_selection_survives_search_and_catalog_refresh(self):
        dialog = self.dialog(False)
        dialog._list.item(0).setCheckState(Qt.Checked)
        dialog._search.setText("missing")
        dialog._loaded([self.tag])
        self.assertTrue(dialog._list.item(0).isHidden())
        self.assertEqual(dialog.tag_ids(), [self.tag.id])

    def test_creation_selects_returned_id_and_deduplicates(self):
        dialog = self.dialog(False)
        dialog._name.setText(" Gaming ")
        dialog._create_tag()
        self.assertEqual(self.worker.call_args.args[4], "Gaming")
        self.assertFalse(dialog._finish.isEnabled())
        dialog.accept()
        self.assertFalse(dialog._closed)
        dialog._created(self.tag)
        self.assertTrue(dialog._finish.isEnabled())
        self.assertEqual(dialog.tag_ids(), [self.tag.id])
        self.assertEqual(dialog._list.count(), 1)

    def test_errors_and_closed_dialog(self):
        dialog = self.dialog()
        dialog._load_failed("offline")
        self.assertIsNone(dialog.tag_ids())
        self.assertTrue(dialog._finish.isEnabled())
        dialog._name.setText("new")
        dialog._create_tag()
        dialog._create_failed("offline")
        self.assertTrue(dialog._finish.isEnabled())
        dialog.reject()
        dialog._created(ChannelTagOut(id=uuid4(), name="Ignored"))
        self.assertEqual(dialog._list.count(), 1)

    def test_callback_serialization(self):
        api = ApiClient("http://localhost", "test")
        self.addCleanup(api.close)
        for ids in (None, [], [self.tag.id]):
            with self.subTest(ids=ids), patch.object(api, "_request", return_value=self.channel.model_dump(mode="json")) as request:
                api.callback_auth(AuthCallbackPayload(state="state", code="code", tag_ids=ids))
                expected = {"state": "state", "code": "code"}
                if ids is not None:
                    expected["tag_ids"] = [str(tag_id) for tag_id in ids]
                request.assert_called_once_with("POST", "/channels/auth/callback", json=expected)


if __name__ == "__main__":
    unittest.main()
