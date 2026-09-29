import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.api_client import ApiClient
from src.models import ChannelOut, ChannelTagOut, ChannelUploadedCount
from src.widgets.channel_table_model import ChannelTableModel, ChannelProxyModel
from src.widgets.channels_page import ChannelsPage
from src.widgets.dashboard import DashboardWidget

APP = QApplication.instance() or QApplication([])


def channel(title, date="2026-09-01T00:00:00Z", tags=()):
    return ChannelOut(id=uuid4(), youtube_channel_id="UC" + title, title=title,
                      created_at=date, updated_at=date,
                      tags=[ChannelTagOut(id=uuid4(), name=name) for name in tags])


class ChannelTagsTests(unittest.TestCase):
    def test_existing_tag_is_assigned_with_one_request(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        existing_tag = ChannelTagOut(id=uuid4(), name="Gaming")
        current = channel("A")
        updated = current.model_copy(update={"tags": [existing_tag]})
        api = Mock()
        api.assign_channel_tag.return_value = updated
        page.api = api
        page._current_channel = current
        page._on_tags_loaded([existing_tag])
        page._tag_combo.setEditText("Gaming")
        with patch("src.widgets.channels_page.run_api") as worker, patch.object(page, "_load_tags") as reload_tags:
            page._change_tag(True)
            operation = worker.call_args.args[1]
            self.assertEqual(operation(), updated)  # API mock result is returned below.
            api.create_tag.assert_not_called()
            api.assign_channel_tag.assert_called_once_with(current.id, existing_tag.id)
            worker.call_args.args[2](updated)
            reload_tags.assert_not_called()

    def test_new_tag_still_creates_then_assigns(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        current = channel("A")
        new_tag = ChannelTagOut(id=uuid4(), name="New")
        updated = current.model_copy(update={"tags": [new_tag]})
        api = Mock()
        api.create_tag.return_value = new_tag
        api.assign_channel_tag.return_value = updated
        page.api = api
        page._current_channel = current
        page._tag_combo.setEditText("New")
        with patch("src.widgets.channels_page.run_api") as worker, patch.object(page, "_load_tags") as reload_tags:
            page._change_tag(True)
            self.assertEqual(worker.call_args.args[1](), updated)
            api.create_tag.assert_called_once_with("New")
            api.assign_channel_tag.assert_called_once_with(current.id, new_tag.id)
            worker.call_args.args[2](updated)
            reload_tags.assert_called_once()

    def test_api_contract(self):
        api = ApiClient("http://localhost", "test")
        ch = channel("A", tags=["Gaming"])
        tag = ch.tags[0]
        with patch.object(api, "_request", return_value=[tag.model_dump(mode="json")]) as request:
            self.assertEqual(api.get_tags(), [tag])
            request.assert_called_once_with("GET", "/tags")
        with patch.object(api, "_request", return_value=tag.model_dump(mode="json")) as request:
            self.assertEqual(api.create_tag("Gaming"), tag)
            request.assert_called_once_with("POST", "/tags", json={"name": "Gaming"})
        for method, verb in [(api.assign_channel_tag, "PUT"), (api.remove_channel_tag, "DELETE")]:
            with patch.object(api, "_request", return_value=ch.model_dump(mode="json")) as request:
                self.assertEqual(method(ch.id, tag.id).tags, [tag])
                request.assert_called_once_with(verb, f"/channels/{ch.id}/tags/{tag.id}")
        api.close()

    def test_numeric_date_sort_and_unknown_last(self):
        old = channel("Old", "2025-12-31T23:00:00Z")
        new = channel("New", "2026-01-01T02:00:00+02:00")
        unknown = channel("Unknown")
        model = ChannelTableModel()
        model.set_channels([new, old, unknown])
        model.set_uploaded_count(old.id, 100)
        model.set_uploaded_count(new.id, 9)
        proxy = ChannelProxyModel()
        proxy.setSourceModel(model)
        proxy.sort(7, Qt.AscendingOrder)
        self.assertEqual([proxy.index(i, 0).data(Qt.UserRole).id for i in range(3)], [new.id, old.id, unknown.id])
        proxy.sort(7, Qt.DescendingOrder)
        self.assertEqual([proxy.index(i, 0).data(Qt.UserRole).id for i in range(3)], [old.id, new.id, unknown.id])
        proxy.sort(6, Qt.AscendingOrder)
        self.assertEqual(proxy.index(0, 0).data(Qt.UserRole).id, old.id)

    def test_search_group_selection_and_live_update(self):
        page = ChannelsPage()
        a = channel("Alpha", tags=["Gaming", "Shorts"])
        b = channel("Beta")
        page.set_channels([a, b])
        page._search.setText("gAmInG")
        self.assertEqual(page._proxy_model.rowCount(), 1)
        page._search.clear()
        page._group_tags.setChecked(True)
        self.assertEqual(len(page._tag_groups._panels), 3)
        shorts = next(panel for panel in page._tag_groups._panels if panel.tag_name == "Shorts")
        shorts.set_expanded(True)
        shorts._table.selectAll()
        self.assertEqual(page._proxy_model.rowCount(), 2)
        self.assertEqual(len(page._selected_channels()), 1)
        page.update_channel(a.model_copy(update={"tags": []}))
        self.assertEqual(len(page._tag_groups._panels), 1)
        page.remove_channel(b.id)
        self.assertEqual(len(page._tag_groups._panels), 1)
        page.deleteLater()

    def test_dashboard_and_count_signal(self):
        page = ChannelsPage()
        dashboard = DashboardWidget()
        a = channel("Alpha", tags=["<Gaming>"])
        page.set_channels([a])
        dashboard.set_channels([a])
        page.uploaded_count_changed.connect(dashboard.set_uploaded_count)
        page._on_uploaded_count_loaded(a.id, ChannelUploadedCount(total_uploaded=123, uploads_in_current_period=0, period_hours=24, is_limited=False))
        self.assertEqual(dashboard._cards[str(a.id)].uploaded_label.text(), "Uploaded: 123")
        updated = a.model_copy(update={"tags": []})
        dashboard.update_channel(updated)
        dashboard.set_channels(dashboard._channels)
        self.assertEqual(dashboard._cards[str(a.id)].tags_label.text(), "No tags")
        self.assertEqual(dashboard._cards[str(a.id)].uploaded_label.text(), "Uploaded: 123")
        page.deleteLater()
        dashboard.deleteLater()


if __name__ == "__main__":
    unittest.main()
