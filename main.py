import ctypes
from pathlib import Path
import sys

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLineEdit,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from organizer import organize


class Worker(QThread):
    message = Signal(str)

    def __init__(self, root, parent):
        super().__init__(parent)
        self.root = root

    def run(self):
        try:
            organize(self.root, self.message.emit)
        except Exception as error:
            self.message.emit(f"任务中止：{error}")


class Window(QWidget):
    def __init__(self, icon_path=Path(__file__).with_name("Golden_Years.ico")):
        super().__init__()
        self.setWindowTitle("Golden Years")
        self.resize(660, 400)
        self.worker = None
        self.path = QLineEdit()
        self.path.setReadOnly(True)
        self.path.setPlaceholderText("请选择照片和视频所在的主文件夹")
        self.choose = QPushButton("选择")
        self.execute = QPushButton("执行")
        self.execute.setEnabled(False)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        row = QHBoxLayout()
        for widget in (self.path, self.choose, self.execute):
            row.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.output)
        self.choose.clicked.connect(self.select_folder)
        self.execute.clicked.connect(self.start)
        icon = QIcon(str(icon_path)) if icon_path.is_file() else QIcon()
        if icon.isNull():
            self.output.appendPlainText("提示：Golden_Years.ico 不可用，使用默认图标")
        else:
            QApplication.instance().setWindowIcon(icon)
            self.setWindowIcon(icon)

    @Slot()
    def select_folder(self):
        root = QFileDialog.getExistingDirectory(self, "选择主文件夹")
        if root:
            self.path.setText(root)
            self.execute.setEnabled(True)

    @Slot()
    def start(self):
        if self.worker is not None or not self.path.text():
            return
        self.choose.setEnabled(False)
        self.execute.setEnabled(False)
        self.output.setPlainText("正在扫描主文件夹…")
        self.worker = Worker(self.path.text(), self)
        self.worker.message.connect(self.output.appendPlainText)
        self.worker.finished.connect(self.finish)
        self.worker.start()

    @Slot()
    def finish(self):
        self.worker.deleteLater()
        self.worker = None
        self.choose.setEnabled(True)
        self.execute.setEnabled(True)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            self.output.appendPlainText("整理进行中，请在任务结束后关闭窗口")
        else:
            super().closeEvent(event)


if __name__ == "__main__":
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GoldenYears.Desktop")
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(app.exec())
