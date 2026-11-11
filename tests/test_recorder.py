"""Recorder smoke tests using synthetic input; no real screen/input capture."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crec import crec
from crec.schemas import Update


class RecorderTests(unittest.TestCase):
    def test_cli_entry_points(self):
        for command in ([sys.executable, "-m", "crec", "--help"],
                        [str(Path(sys.executable).with_name("crec")), "--help"]):
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--scroll-debounce", result.stdout)
            self.assertIn("macOS", result.stdout)

    def test_cli_help_does_not_load_capture_backends(self):
        code = """
import sys
from crec.cli import main
sys.argv = ['crec', '--help']
try:
    main()
except SystemExit as exc:
    assert exc.code == 0
assert not {'Quartz', 'pynput', 'mss'} & sys.modules.keys()
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recording_persists_without_processing_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            async def record():
                recorder = crec("test", data_directory=directory)
                await recorder.connect_db()
                try:
                    await recorder._default_handler(
                        SimpleNamespace(name="Screen"),
                        Update(content="click_left(10.0, 20.0)", content_type="input_text"),
                    )
                finally:
                    await recorder.engine.dispose()

            asyncio.run(record())
            with sqlite3.connect(Path(directory) / "actions.db") as db:
                tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                self.assertEqual(tables, [("observations",)])
                row = db.execute("SELECT observer_name, content, content_type FROM observations").fetchone()
                self.assertEqual(row, ("Screen", "click_left(10.0, 20.0)", "input_text"))

    @unittest.skipUnless(sys.platform == "darwin", "Screen uses macOS Quartz")
    def test_synthetic_screenshot_and_action(self):
        # Import the real screen code with inert keyboard/mouse backends.
        with patch.dict(os.environ, {"PYNPUT_BACKEND": "dummy"}):
            from crec.observers.screen import Screen
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            async def capture():
                # Bypass construction so no screen capture or listeners start.
                screen = Screen.__new__(Screen)
                screen.screens_dir = directory
                screen.update_queue = asyncio.Queue()
                screen._thread_pool = ThreadPoolExecutor(max_workers=1)
                try:
                    frame = SimpleNamespace(width=160, height=100, rgb=bytes([255]) * 160 * 100 * 3)
                    path = await screen._save_frame(frame, 20, 20, "test_before")
                    with Image.open(path) as image:
                        self.assertEqual(image.format, "JPEG")
                        self.assertEqual(image.size, (160, 100))
                    await screen._process_and_emit(path, None, "click_left", {"position": (20, 20)})
                    update = screen.update_queue.get_nowait()
                    self.assertEqual(update.content, "click_left(20.0, 20.0)")
                finally:
                    screen._thread_pool.shutdown(wait=True)

            asyncio.run(capture())


if __name__ == "__main__":
    unittest.main()
