from datetime import datetime
import errno
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import organizer as org

STAMP = int(datetime(2026, 2, 12, 10).timestamp()) * 10**9


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.photos = self.root / "2026年/2月/Photos"
        self.xxx = self.root / "XXX"
        self.messages = []

    def media(self, name, data, offset=0):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, ns=(STAMP + offset, STAMP + offset))
        return path

    def run_task(self):
        return org.organize(self.root, self.messages.append)

    def test_scope_sort_and_dates(self):
        nested = self.media("child/2001.jpg", b"unrelated")
        self.run_task()
        self.assertEqual(list(self.root.iterdir()), [nested.parent])
        exif = b"Exif\x00\x002001:01:01"
        self.media("1990.JPG", exif)
        self.media("a.png", b"later", 1_000_000)
        self.media("clip.MOV", b"movie")
        self.run_task()
        self.assertEqual((self.photos / "MQP202602_01.JPG").read_bytes(), exif)
        self.assertEqual((self.photos / "MQP202602_02.png").read_bytes(), b"later")
        self.assertEqual((self.photos.parent / "Videos/MQV202602_01.MOV").read_bytes(), b"movie")
        self.assertTrue(nested.exists())
        self.assertFalse(self.xxx.exists())

    def test_continue_and_repeat(self):
        for num in ("01", "03"):
            self.media(f"2026年/2月/Photos/mqp202602_{num}.jpg", num.encode())
        self.media("new.gif", b"4")
        self.run_task()
        self.assertEqual((self.photos / "MQP202602_04.gif").read_bytes(), b"4")
        self.media("2026年/2月/Photos/MQP202602_99.jpg", b"99")
        self.media("new.gif", b"100")
        self.run_task()
        before = sorted(self.root.rglob("*"))
        self.run_task()
        self.assertEqual(before, sorted(self.root.rglob("*")))
        self.assertTrue((self.photos / "MQP202602_100.gif").exists())
        self.assertFalse((self.photos.parent / "Videos").exists())

    def test_exact_duplicates_and_log_append(self):
        self.media("XXX/B.png", b"unique")
        self.media("B.png", b"same", 2_000_000)
        self.media("A.jpg", b"same")
        self.media("C.jpg", b"diff")
        self.media("D.gif", b"unique")
        counts = self.run_task()
        self.assertEqual(counts["archived", "Photos"], 3)
        self.assertEqual(counts["duplicate", "Photos"], 1)
        duplicate = self.xxx / "B_1.png"
        self.assertEqual(duplicate.read_bytes(), b"same")
        self.assertEqual(duplicate.stat().st_mtime_ns, STAMP + 2_000_000)
        original = self.photos / "MQP202602_01.jpg"
        self.assertEqual(original.stat().st_mtime_ns, STAMP)
        self.assertFalse((self.root / "A.jpg").exists())
        log = next(self.xxx.glob("*日志.txt"))
        first = log.read_text(encoding="utf-8")
        self.assertIn("MQP202602_01.jpg", first)
        self.assertIn("B_1.png", first)
        self.media("other.mov", b"same")
        self.run_task()
        second = log.read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first))
        self.assertIn("other.mov", second[len(first):])
        self.run_task()
        self.assertEqual(log.read_text(encoding="utf-8"), second)

    def test_no_clobber_and_failures(self):
        source = self.media("raw.jpg", b"current")
        old = self.media("other.txt", b"old")
        with self.assertRaises(FileExistsError):
            org.move(org.File.read(source), old)
        self.assertEqual(old.read_bytes(), b"old")
        for error in (PermissionError("locked"), PermissionError("access denied")):
            with patch("organizer.publish", side_effect=error):
                self.assertEqual(self.run_task()["failed", "Photos"], 1)
            self.assertIn(str(error), "\n".join(self.messages))
            self.assertEqual(source.read_bytes(), b"current")
            self.assertFalse(self.photos.exists())
        snap = org.File.read(source)
        source.write_bytes(b"changed")
        with self.assertRaises(OSError):
            org.move(snap, self.root / "unused")
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            org.digest(snap)

    def test_cross_volume_and_chunking(self):
        content = os.urandom(org.BLOCK * 2 + 7)
        source = self.media("large.mp4", content)
        target = self.root / "out.mp4"
        publish = org.publish

        def different_volume(path, dest):
            if path == source:
                raise OSError(errno.EXDEV, "different volume")
            publish(path, dest)

        with patch("organizer.publish", side_effect=different_volume):
            org.move(org.File.read(source), target)
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(target.stat().st_mtime_ns, STAMP)
        hasher = Mock(wraps=org.hashlib.sha256())
        with patch("organizer.hashlib.sha256", return_value=hasher):
            org.digest(org.File.read(target))
            sizes = [len(call.args[0]) for call in hasher.update.call_args_list]
            self.assertEqual(sizes, [org.BLOCK, org.BLOCK, 7])

    def test_log_failure(self):
        self.media("a.jpg", b"a")
        self.media("b.jpg", b"a")
        with patch("organizer.append_log", side_effect=OSError("disk full")):
            counts = self.run_task()
        self.assertEqual(counts["log_error"], 1)
        self.assertTrue((self.xxx / "b.jpg").exists())
        self.assertIn("写入日志失败", "\n".join(self.messages))

    def test_linked_directory(self):
        source = self.media("a.jpg", b"a")
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "2026年").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(str(error))
            self.assertEqual(self.run_task()["failed", "Photos"], 1)
            self.assertTrue(source.exists())
            self.assertEqual(list(Path(outside).iterdir()), [])


class GuiTests(unittest.TestCase):
    def test_window_and_worker(self):
        from main import QApplication, Window
        from PySide6.QtTest import QTest

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            missing = Window(Path(tmp) / "missing.ico")
            self.assertIn("默认图标", missing.output.toPlainText())
            window = Window()
            self.assertFalse(window.windowIcon().isNull())
            self.assertFalse(window.execute.isEnabled())
            with patch("main.QFileDialog.getExistingDirectory", return_value=tmp):
                window.choose.click()
            window.execute.click()
            worker = window.worker
            window.start()
            self.assertIs(window.worker, worker)
            self.assertFalse(window.execute.isEnabled())
            deadline = time.monotonic() + 5
            while window.worker and time.monotonic() < deadline:
                QTest.qWait(10)
                app.processEvents()
            self.assertTrue(window.execute.isEnabled())
            self.assertIn("扫描完成", window.output.toPlainText())
            self.assertIn("全部操作完成", window.output.toPlainText())
            window.close()
            missing.close()
