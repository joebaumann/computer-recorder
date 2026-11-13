"""Synthetic frames/listeners only. Never captures the real desktop or input."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {'PYNPUT_BACKEND': 'dummy'}):
    from crec.observers import screen as module
from crec import crec
from crec.schemas import Update
from PIL import Image


def frame(width, height, value=255):
    return SimpleNamespace(width=width, height=height,
                           rgb=bytes([value]) * width * height * 3)


def red_bounds(path):
    with Image.open(path) as image:
        points = [(x, y) for y in range(image.height) for x in range(image.width)
                  if (lambda c: c[0] > 160 and c[1] < 120 and c[2] < 120)(image.getpixel((x, y)))]
        if not points:
            raise AssertionError('Missing red marker')
        return (min(x for x, _ in points), min(y for _, y in points),
                max(x for x, _ in points), max(y for _, y in points))


class FakeListener:
    instances = []

    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.stopped = False
        self.joined = False
        self.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


class FakeCapture:
    monitors = [
        {'left': -640, 'top': 0, 'width': 1280, 'height': 400},
        {'left': 0, 'top': 0, 'width': 640, 'height': 400},
        {'left': -640, 'top': 0, 'width': 640, 'height': 400},
    ]

    def __init__(self):
        self.closed = False
        self.value = 255

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def grab(self, monitor):
        if self.closed:
            raise AssertionError('Capture closed before pending event completed')
        # Main monitor returns 2x pixels, left monitor 1x.
        ratio = 2 if monitor['left'] == 0 else 1
        return frame(monitor['width'] * ratio, monitor['height'] * ratio, self.value)


class CaptureRegressionTests(unittest.TestCase):
    def test_marker_pixels_match_actual_resolution_and_edges(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                screen = module.Screen.__new__(module.Screen)
                screen.screens_dir = directory
                screen._thread_pool = ThreadPoolExecutor(max_workers=1)
                try:
                    for ratio in (1, 1.25, 2):
                        w, h = int(640 * ratio), int(400 * ratio)
                        monitor = {'width': 640, 'height': 400}
                        path = await screen._save_frame(frame(w, h), 410, 270,
                                                        f'ratio_{ratio}', monitor=monitor)
                        x0, y0, x1, y1 = red_bounds(path)
                        self.assertAlmostEqual((x0 + x1) / 2, 410 * ratio, delta=2)
                        self.assertAlmostEqual((y0 + y1) / 2, 270 * ratio, delta=2)
                        for x, y in ((0, 0), (639, 399)):
                            path = await screen._save_frame(frame(w, h), x, y,
                                                            f'edge_{ratio}_{x}', monitor=monitor)
                            bounds = red_bounds(path)
                            self.assertGreaterEqual(min(bounds), 0)
                            self.assertLess(bounds[2], w)
                            self.assertLess(bounds[3], h)
                finally:
                    screen._thread_pool.shutdown(wait=True)
        asyncio.run(check())

    def test_stationary_scroll_accumulates_deltas_and_respects_rate(self):
        screen = module.Screen.__new__(module.Screen)
        for key, value in {
            '_scroll_last_time': None, '_scroll_last_input': None,
            '_scroll_distance': 0, '_scroll_session_start': None,
            '_scroll_event_count': 0, '_scroll_debounce_sec': 0.5,
            '_scroll_min_distance': 5, '_scroll_max_frequency': 10,
            '_scroll_session_timeout': 2,
        }.items():
            setattr(screen, key, value)
        with patch.object(module.time, 'monotonic') as clock:
            clock.return_value = 100
            self.assertTrue(screen._should_log_scroll(400, 200, 0, .1))
            clock.return_value = 100.1
            self.assertFalse(screen._should_log_scroll(400, 200, 0, 3))
            clock.return_value = 100.6
            self.assertTrue(screen._should_log_scroll(400, 200, 0, 3))
            clock.return_value = 100.7
            self.assertFalse(screen._should_log_scroll(400, 200, 0, 8))
            clock.return_value = 101.2
            self.assertTrue(screen._should_log_scroll(400, 200, 0, .25))
            clock.return_value = 101.3
            self.assertFalse(screen._should_log_scroll(400, 200, 0, 0))
            clock.return_value = 104
            self.assertTrue(screen._should_log_scroll(400, 200, .2, 0))

    def test_real_event_path_with_fake_capture_and_stop_during_writes(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
                backend = FakeCapture()
                FakeListener.instances = []
                patches.enter_context(patch.object(module.mss, 'mss', return_value=backend))
                patches.enter_context(patch.object(module.mouse, 'Listener', FakeListener))
                patches.enter_context(patch.object(module.keyboard, 'Listener', FakeListener))
                patches.enter_context(patch.object(module.mouse, 'Controller',
                                                    return_value=SimpleNamespace(position=(-230, 270))))
                patches.enter_context(patch.object(module.Screen, '_detect_high_dpi', return_value=False))
                errors = []
                asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
                screen = module.Screen(screenshots_dir=directory, scroll_debounce_sec=.01,
                                       scroll_min_distance=0, scroll_max_frequency=100)
                try:
                    async with asyncio.timeout(3):
                        while len(screen._frames) != 2:
                            await asyncio.sleep(.005)
                    mouse_listener, keys = FakeListener.instances
                    # Off-screen callbacks must not index a missing monitor.
                    mouse_listener.callbacks['on_click'](-10000, 0, SimpleNamespace(name='left'), True)
                    mouse_listener.callbacks['on_scroll'](-10000, 0, 0, 5)
                    # Overlapping callbacks must keep their own coordinates.
                    mouse_listener.callbacks['on_click'](-230, 270, SimpleNamespace(name='left'), True)
                    mouse_listener.callbacks['on_scroll'](410, 270, 0, .25)
                    await asyncio.sleep(.03)
                    backend.value = 180  # Simulated changed page after scrolling.
                    await asyncio.sleep(.03)
                    mouse_listener.callbacks['on_scroll'](410, 270, 0, 2)
                    for char in 'synthetic':
                        keys.callbacks['on_press'](char)
                    # Allow listener dispatch onto the loop, but stop while
                    # delayed after frames and keyboard saves are still pending.
                    await asyncio.sleep(.005)
                    await asyncio.wait_for(screen.stop(), timeout=5)
                    await screen.stop()  # idempotent
                    self.assertTrue(backend.closed)
                    self.assertTrue(all(x.stopped and x.joined for x in FakeListener.instances))
                    self.assertFalse(screen._event_tasks)
                    self.assertTrue(screen._thread_pool._shutdown)
                    events = []
                    while not screen.update_queue.empty():
                        events.append(screen.update_queue.get_nowait().content)
                    self.assertEqual(sum(x.startswith('click_left') for x in events), 1)
                    self.assertEqual(sum(x.startswith('scroll') for x in events), 2)
                    self.assertEqual(sum(x.startswith('key_press') for x in events), 9)
                    files = list(Path(directory).glob('*.jpg'))
                    self.assertEqual(len(files), 8)  # 3 before/after pairs, first/final key frames
                    click = next(p for p in files if 'click_left' in p.name and '_after' in p.name)
                    x0, y0, x1, y1 = red_bounds(click)
                    self.assertAlmostEqual((x0 + x1) / 2, 410, delta=2)
                    self.assertAlmostEqual((y0 + y1) / 2, 270, delta=2)
                    scroll = next(p for p in files if 'scroll' in p.name and '_after' in p.name)
                    x0, y0, x1, y1 = red_bounds(scroll)
                    self.assertAlmostEqual((x0 + x1) / 2, 820, delta=2)
                    self.assertAlmostEqual((y0 + y1) / 2, 540, delta=2)
                    with Image.open(scroll) as image:
                        self.assertAlmostEqual(image.getpixel((0, 0))[0], 180, delta=3)
                    self.assertFalse(errors)
                finally:
                    await screen.stop()
        asyncio.run(check())

    def test_shutdown_persists_last_events_from_all_observers(self):
        async def check(directory):
            class FinalObserver:
                def __init__(self, name):
                    self.name = name
                    self.update_queue = asyncio.Queue()

                async def stop(self):
                    await self.update_queue.put(Update(content=self.name + '-final', content_type='input_text'))
                    await asyncio.sleep(0)

            observers = [FinalObserver('first'), FinalObserver('second')]
            async with crec('synthetic', *observers, data_directory=directory, verbosity=50):
                for i in range(25):
                    for observer in observers:
                        await observer.update_queue.put(Update(content=f'{observer.name}-{i}', content_type='input_text'))
            with sqlite3.connect(Path(directory) / 'actions.db') as db:
                actual = {row[0] for row in db.execute('SELECT content FROM observations')}
            expected = {f'{name}-{i}' for name in ('first', 'second') for i in range(25)}
            expected.update({'first-final', 'second-final'})
            self.assertEqual(actual, expected)
            self.assertEqual(len(asyncio.all_tasks()), 1)
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(check(directory))


if __name__ == '__main__':
    unittest.main()
