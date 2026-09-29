from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpacerItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class Sidebar(QWidget):
    page_changed = Signal(int)
    add_channel_clicked = Signal()
    settings_clicked = Signal()
    browser_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(200)
        self.setMaximumWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("<b>YouTube Uploader</b>")
        title.setStyleSheet("font-size: 14px; padding: 8px 0;")
        layout.addWidget(title)

        self.nav_buttons: list[QPushButton] = []
        pages = [
            ("Dashboard", 0),
            ("Channels", 1),
            ("Tasks", 2),
            ("Projects", 3),
            ("Proxies", 4),
            ("Models", 5),
        ]
        for label, idx in pages:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFlat(True)
            btn.setStyleSheet(
                "QPushButton { text-align: left; padding: 8px 12px; border-radius: 4px; }"
                "QPushButton:checked { background-color: #3a7bd5; color: white; font-weight: bold; }"
                "QPushButton:hover:!checked { background-color: #e0e0e0; }"
            )
            btn.clicked.connect(lambda checked, i=idx: self._on_nav(i))
            self.nav_buttons.append(btn)
            layout.addWidget(btn)

        layout.addSpacing(20)

        self.add_channel_btn = QPushButton("+ Add Channel")
        layout.addWidget(self.add_channel_btn)
        self.browser_btn = QPushButton("Browser")
        layout.addWidget(self.browser_btn)

        layout.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

        self.sse_status = QLabel("SSE: <span style='color:gray'>Disconnected</span>")
        layout.addWidget(self.sse_status)
        self.last_browser_proxy = QLabel("Last browser proxy: none")
        self.last_browser_proxy.setWordWrap(True)
        layout.addWidget(self.last_browser_proxy)

        self.settings_btn = QPushButton("Settings")
        layout.addWidget(self.settings_btn)

        self.add_channel_btn.clicked.connect(self.add_channel_clicked.emit)
        self.browser_btn.clicked.connect(self.browser_clicked.emit)
        self.settings_btn.clicked.connect(self.settings_clicked.emit)

        self.set_current_page(0)

    def set_current_page(self, index: int) -> None:
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)
        self.page_changed.emit(index)

    def _on_nav(self, index: int) -> None:
        self.set_current_page(index)

    def set_sse_connected(self, connected: bool) -> None:
        self.set_sse_state("connected" if connected else "disconnected")

    def set_sse_state(self, state: str) -> None:
        """state: 'connected' | 'connecting' | 'disconnected'."""
        styles = {
            "connected": ("green", "Connected"),
            "connecting": ("#e6a817", "Reconnecting..."),
            "disconnected": ("gray", "Disconnected"),
        }
        color, text = styles.get(state, styles["disconnected"])
        self.sse_status.setText(f"SSE: <span style='color:{color}'>{text}</span>")

    def set_last_browser_proxy(self, proxy_url: str | None) -> None:
        self.last_browser_proxy.setText(f"Last browser proxy: {proxy_url or 'none'}")
