import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Qt
import shiboken6

from test_channel_tags import APP, channel
from src.api_worker import run_api, cancel_api_requests
from src.models import ChannelTagOut
from src.widgets.channels_page import ChannelsPage
from src.widgets.dashboard import DashboardWidget
from src.widgets.channel_labels import authorization_age


class ChannelPerformanceTests(unittest.TestCase):
    def test_count_reads_are_parallel_bounded_and_stale_results_ignored(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        page.set_api(Mock())
        channels = [channel(str(i)) for i in range(10)]
        with patch("src.widgets.channels_page.run_api") as worker:
            page.set_channels(channels)
            self.assertEqual(len(page._count_inflight), 3)
            self.assertEqual(len(page._count_queue), 7)
            count_calls = [call for call in worker.call_args_list if call.args[1] == page.api.get_uploaded_count]
            self.assertEqual(len(count_calls), 3)
            page.set_api(Mock())
            # Old server results may arrive after a switch: neither UI nor queue changes.
            with patch.object(page, "_on_uploaded_count_loaded") as loaded:
                count_calls[0].args[2](Mock(total_uploaded=999))
                loaded.assert_not_called()
            self.assertEqual(page._count_inflight, set())

    def test_group_and_sort_preserve_selection_without_detail_reload(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        channels = [channel(str(i), tags=["Gaming", "Shorts"]) for i in range(50)]
        page.set_channels(channels)
        page._channel_table.selectRow(0)
        selected = page._current_channel.id
        with patch.object(page, "_load_upload_limit") as limits, patch.object(page, "_load_uploaded_count") as counts, patch.object(page._files_tab, "set_channel") as files:
            page._group_tags.setChecked(True)
            page._channel_table.sortByColumn(6, Qt.DescendingOrder)
            page._group_tags.setChecked(False)
            self.assertEqual(page._current_channel.id, selected)
            limits.assert_not_called()
            counts.assert_not_called()
            files.assert_not_called()
        page._channel_table.selectAll()
        with patch.object(page, "_refresh_bulk_settings") as refresh:
            page._group_tags.setChecked(True)
            refresh.assert_called_once()
        self.assertEqual(len(page._selected_channels()), 50)
        header = page._channel_table.horizontalHeader()
        self.assertEqual([header.logicalIndex(i) for i in range(header.count())], [0, 1, 2, 3, 4, 9, 5, 8, 7, 6])
        self.assertEqual(page._channel_model.index(0, 6).data(), f"{authorization_age(channels[0])}d")
        self.assertEqual(page._channel_table.verticalHeader().sectionSize(0), page._channel_table.fontMetrics().height() + 8)

    def test_tag_groups_are_collapsible_and_isolate_multi_selection(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        channels = [
            channel("One", tags=["Tag A"]),
            channel("Two", tags=["Tag A"]),
            channel("Three", tags=["Tag B"]),
        ]
        page.set_channels(channels)
        page._group_tags.setChecked(True)
        panels = {panel.tag_name: panel for panel in page._tag_groups._panels}
        tag_a, tag_b = panels["Tag A"], panels["Tag B"]
        self.assertFalse(tag_a._table.isVisible())
        tag_a.set_expanded(True)
        tag_a._select_all.click()
        self.assertEqual({item.id for item in page._selected_channels()}, {channels[0].id, channels[1].id})
        self.assertFalse(page._bulk_widget.isHidden())
        tag_b.set_expanded(True)
        self.assertFalse(tag_a._table.isVisible())
        self.assertEqual(tag_a.selected_channels(), [])
        self.assertEqual(page._selected_channels(), [])
        tag_b._table.selectRow(0)
        self.assertEqual([item.id for item in page._selected_channels()], [channels[2].id])
        self.assertTrue(page._bulk_widget.isHidden())

    def test_tag_groups_follow_channel_tag_updates_without_reordering_existing_groups(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        no_tag = channel("No tag")
        red = channel("Red", tags=["Red"])
        blue = channel("Blue", tags=["Blue"])
        remaining = channel("Still no tag")
        page.set_channels([no_tag, red, blue, remaining])
        page._group_tags.setChecked(True)
        names = lambda: [panel.tag_name for panel in page._tag_groups._panels]
        self.assertEqual(names(), ["No tags", "Red", "Blue"])

        # Moving a channel from the untagged group into an existing tag preserves
        # every existing group position and immediately changes its membership.
        page.update_channel(no_tag.model_copy(update={"tags": red.tags}))
        self.assertEqual(names(), ["No tags", "Red", "Blue"])
        red_panel = next(panel for panel in page._tag_groups._panels if panel.tag_name == "Red")
        self.assertEqual(
            {item.id for item in red_panel._proxy.sourceModel()._channels if item.id in red_panel.channel_ids},
            {no_tag.id, red.id},
        )

        green_tag = ChannelTagOut(id=__import__("uuid").uuid4(), name="Green")
        page.update_channel(remaining.model_copy(update={"tags": [green_tag]}))
        self.assertEqual(names(), ["Red", "Blue", "Green"])
        green_panel = page._tag_groups._panels[-1]
        self.assertEqual(green_panel.tag_name, "Green")
        self.assertEqual(green_panel.channel_ids, {remaining.id})

    def test_tag_group_height_fits_one_channel_and_shows_up_to_24_rows(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        one = channel("Only", tags=["One"])
        many = [channel(str(index), tags=["Many"]) for index in range(25)]
        page.set_channels([one, *many])
        page._group_tags.setChecked(True)
        page.resize(1200, 900)
        page.show()
        APP.processEvents()
        panels = {panel.tag_name: panel for panel in page._tag_groups._panels}
        one_panel, many_panel = panels["One"], panels["Many"]
        one_panel.set_expanded(True)
        APP.processEvents()
        self.assertGreaterEqual(
            one_panel._table.viewport().height(),
            one_panel._table.verticalHeader().sectionSize(0),
        )
        self.assertGreaterEqual(many_panel._table.height(), many_panel._table.verticalHeader().sectionSize(0) * 24)
        many_panel.set_expanded(True)
        APP.processEvents()
        self.assertGreaterEqual(many_panel._table.viewport().height(), many_panel._table.verticalHeader().sectionSize(0) * 24)
        self.assertGreater(many_panel._table.verticalScrollBar().maximum(), 0)

    def test_no_sorting_restores_server_order(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        channels = [channel(name) for name in ("Zulu", "Alpha", "Mike")]
        page.set_channels(channels)
        page._sort.setCurrentIndex(1)  # Title A-Z
        self.assertEqual([page._proxy_model.index(i, 0).data() for i in range(3)], ["Alpha", "Mike", "Zulu"])
        page._sort.setCurrentIndex(0)
        self.assertFalse(page._channel_table.isSortingEnabled())
        self.assertEqual([page._proxy_model.index(i, 0).data() for i in range(3)], ["Zulu", "Alpha", "Mike"])

    def test_snapshot_does_not_reset_table_or_reload_counts(self):
        page = ChannelsPage()
        self.addCleanup(page.deleteLater)
        channels = [channel(str(i)) for i in range(20)]
        page.set_channels(channels)
        page._count_queue.clear()
        resets = []
        page._channel_model.modelReset.connect(lambda: resets.append(True))
        with patch.object(page, "_load_tags") as tags:
            page.set_channels([c.model_copy() for c in channels])
            tags.assert_not_called()
        self.assertEqual(resets, [])
        self.assertEqual(page._count_queue, [])
        page._channel_model.set_uploaded_count(channels[0].id, 7)
        changed = Mock()
        page._channel_model.dataChanged.connect(changed)
        page._channel_model.set_uploaded_count(channels[0].id, 7)
        changed.assert_not_called()

    def test_dashboard_reuses_cards_and_renders_in_batches(self):
        dashboard = DashboardWidget()
        self.addCleanup(dashboard.deleteLater)
        channels = [channel(str(i)) for i in range(40)]
        dashboard.set_channels(channels)
        self.assertLess(len(dashboard._cards), len(channels))
        card = dashboard._cards[str(channels[0].id)]
        dashboard.set_channels(channels)
        self.assertIs(dashboard._cards[str(channels[0].id)], card)
        dashboard.update_channel(channels[-1].model_copy(update={"status": "deleted"}))
        while dashboard._card_queue:
            dashboard._render_cards()
        self.assertNotIn(str(channels[-1].id), dashboard._cards)

    def test_dashboard_long_tags_wrap_inside_card(self):
        dashboard = DashboardWidget()
        self.addCleanup(dashboard.deleteLater)
        channel_with_long_tag = channel("A", tags=["a" * 140, "second-tag"])
        dashboard.set_channels([channel_with_long_tag])
        dashboard.resize(320, 500)
        dashboard.show()
        APP.processEvents()
        card = dashboard._cards[str(channel_with_long_tag.id)]
        self.assertIn("\u200b", card.tags_label.text())
        self.assertGreater(card.tags_label.height(), card.tags_label.fontMetrics().height())

    def test_network_jobs_are_bounded_and_callbacks_run_on_ui_thread(self):
        lock = threading.Lock()
        active = 0
        maximum = 0
        callback_threads = []
        worker_threads = []
        ticks = []
        loop = QEventLoop()
        def work():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                worker_threads.append(QThread.currentThread())
            time.sleep(0.025)
            with lock:
                active -= 1
            return 1
        def done(value):
            callback_threads.append(QThread.currentThread())
            if len(callback_threads) == 18:
                loop.quit()
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start(5)
        for _ in range(18):
            run_api(None, work, done)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(5000)
        loop.exec()
        timer.stop()
        deadline.stop()
        self.assertEqual(len(callback_threads), 18)
        self.assertLessEqual(maximum, 6)
        self.assertGreater(maximum, 1)
        self.assertTrue(ticks)
        self.assertTrue(all(t == APP.thread() for t in callback_threads))
        self.assertTrue(all(t != APP.thread() for t in worker_threads))

    def test_destroyed_owner_does_not_receive_callback(self):
        loop = QEventLoop()
        owner = QObject()
        callback = Mock()
        request = run_api(None, lambda: time.sleep(0.02), callback, parent=owner)
        request.finished.connect(loop.quit)
        shiboken6.delete(owner)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(5000)
        loop.exec()
        deadline.stop()
        callback.assert_not_called()

    def test_cancelled_server_does_not_receive_callback(self):
        loop = QEventLoop()
        api = object()
        callback = Mock()
        request = run_api(api, lambda: time.sleep(0.02), callback)
        request.finished.connect(loop.quit)
        cancel_api_requests(api)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(5000)
        loop.exec()
        deadline.stop()
        callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
