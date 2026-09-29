import sys

from PySide6.QtWidgets import QApplication

from src.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("YouTube Uploader Client")
    app.setOrganizationName("UploaderClient")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
