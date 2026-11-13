# Modified for the recorder-only fork: removed unused AI and cloud upload code.
from __future__ import annotations
###############################################################################
# Imports                                                                     #
###############################################################################

# — Standard library —
import gc
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

import asyncio

# — Third-party —
import mss
import Quartz
from PIL import Image, ImageDraw
from pynput import mouse, keyboard           # still synchronous
from shapely.geometry import box
from shapely.ops import unary_union

# — Local —
from .observer import Observer
from ..schemas import Update

###############################################################################
# Window‑geometry helpers                                                     #
###############################################################################


def _get_global_bounds() -> tuple[float, float, float, float]:
    """Return a bounding box enclosing **all** physical displays.

    Returns
    -------
    (min_x, min_y, max_x, max_y) tuple in Quartz global coordinates.
    """
    err, ids, cnt = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != Quartz.kCGErrorSuccess:  # pragma: no cover (defensive)
        raise OSError(f"CGGetActiveDisplayList failed: {err}")

    min_x = min_y = float("inf")
    max_x = max_y = -float("inf")
    for did in ids[:cnt]:
        r = Quartz.CGDisplayBounds(did)
        x0, y0 = r.origin.x, r.origin.y
        x1, y1 = x0 + r.size.width, y0 + r.size.height
        min_x, min_y = min(min_x, x0), min(min_y, y0)
        max_x, max_y = max(max_x, x1), max(max_y, y1)
    return min_x, min_y, max_x, max_y


def _get_visible_windows() -> List[tuple[dict, float]]:
    """List *onscreen* windows with their visible‑area ratio.

    Each tuple is ``(window_info_dict, visible_ratio)`` where *visible_ratio*
    is in ``[0.0, 1.0]``.  Internal system windows (Dock, WindowServer, …) are
    ignored.
    """
    _, _, _, gmax_y = _get_global_bounds()

    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListOptionIncludingWindow
    )
    wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)

    occupied = None  # running union of opaque regions above the current window
    result: list[tuple[dict, float]] = []

    for info in wins:
        owner = info.get("kCGWindowOwnerName", "")
        if owner in ("Dock", "WindowServer", "Window Server"):
            continue

        bounds = info.get("kCGWindowBounds", {})
        x, y, w, h = (
            bounds.get("X", 0),
            bounds.get("Y", 0),
            bounds.get("Width", 0),
            bounds.get("Height", 0),
        )
        if w <= 0 or h <= 0:
            continue  # hidden or minimised

        inv_y = gmax_y - y - h  # Quartz→Shapely Y‑flip
        poly = box(x, inv_y, x + w, inv_y + h)
        if poly.is_empty:
            continue

        visible = poly if occupied is None else poly.difference(occupied)
        if not visible.is_empty:
            ratio = visible.area / poly.area
            result.append((info, ratio))
            occupied = poly if occupied is None else unary_union([occupied, poly])

    return result


def _is_app_visible(names: Iterable[str]) -> bool:
    """Return *True* if **any** window from *names* is at least partially visible."""
    targets = set(names)
    return any(
        info.get("kCGWindowOwnerName", "") in targets and ratio > 0
        for info, ratio in _get_visible_windows()
    )

###############################################################################
# Screen observer                                                             #
###############################################################################

class Screen(Observer):
    """
    Capture before/after screenshots around user interactions.
    Blocking work (Quartz, mss, Pillow) is executed in
    background threads via `asyncio.to_thread`.

    Keyboard events are optimized to save disk space:
    - Only the first and last screenshots are kept for consecutive key presses
    - Intermediate screenshots are automatically deleted
    - A keyboard session ends after `keyboard_timeout` seconds of inactivity
    """

    _CAPTURE_FPS: int = 5  # Reduced from 10 to 5 to reduce memory pressure
    _PERIODIC_SEC: int = 30
    _DEBOUNCE_SEC: int = 1
    _MON_START: int = 1     # first real display in mss
    _MEMORY_CLEANUP_INTERVAL: int = 30  # Force GC every 30 frames instead of 50
    _MAX_WORKERS: int = 4  # Limit thread pool size to prevent exhaustion

    # Scroll filtering constants
    _SCROLL_DEBOUNCE_SEC: float = 0.8  # Minimum time between scroll events
    _SCROLL_MIN_DISTANCE: float = 8.0  # Minimum scroll distance to log
    _SCROLL_MAX_FREQUENCY: int = 8  # Max scroll events per second
    _SCROLL_SESSION_TIMEOUT: float = 3.0  # Timeout for scroll sessions

    # ─────────────────────────────── construction
    def __init__(
        self,
        screenshots_dir: str = "~/Downloads/records/screenshots",
        skip_when_visible: Optional[str | list[str]] = None,
        history_k: int = 10,
        debug: bool = False,
        keyboard_timeout: float = 2.0,
        scroll_debounce_sec: float = 0.5,
        scroll_min_distance: float = 5.0,
        scroll_max_frequency: int = 10,
        scroll_session_timeout: float = 2.0,
    ) -> None:

        self.screens_dir = os.path.abspath(os.path.expanduser(screenshots_dir))
        os.makedirs(self.screens_dir, exist_ok=True)

        self._guard = {skip_when_visible} if isinstance(skip_when_visible, str) else set(skip_when_visible or [])

        self.debug = debug

        # Custom thread pool to prevent exhaustion
        self._thread_pool = ThreadPoolExecutor(max_workers=self._MAX_WORKERS)
        self._event_tasks: set[asyncio.Task] = set()
        self._listeners: list = []
        self._stopping = False
        self._stopped = False

        # Scroll filtering configuration
        self._scroll_debounce_sec = scroll_debounce_sec
        self._scroll_min_distance = scroll_min_distance
        self._scroll_max_frequency = scroll_max_frequency
        self._scroll_session_timeout = scroll_session_timeout

        # state shared with worker
        self._frames: Dict[int, Any] = {}
        self._frame_lock = asyncio.Lock()

        self._history: deque[str] = deque(maxlen=max(0, history_k))
        self._pending_event: Optional[dict] = None
        self._debounce_handle: Optional[asyncio.TimerHandle] = None

        # keyboard activity tracking
        self._key_activity_start: Optional[float] = None
        self._key_activity_timeout: float = keyboard_timeout  # seconds of inactivity to consider session ended
        self._key_screenshots: List[str] = []  # track intermediate screenshots for cleanup
        self._key_activity_lock = asyncio.Lock()

        # scroll activity tracking
        self._scroll_last_time: Optional[float] = None
        self._scroll_last_input: Optional[float] = None
        self._scroll_distance: float = 0
        self._scroll_last_position: Optional[tuple[float, float]] = None
        self._scroll_session_start: Optional[float] = None
        self._scroll_event_count: int = 0
        self._scroll_lock = asyncio.Lock()

        # call parent
        super().__init__()

        # Adjust settings for high-DPI displays
        if self._detect_high_dpi():
            self._CAPTURE_FPS = 3  # Even lower FPS for high-DPI displays
            self._MEMORY_CLEANUP_INTERVAL = 20  # More frequent cleanup
            if self.debug:
                logging.getLogger("Screen").info("High-DPI display detected, using conservative settings")

    @staticmethod
    def _mon_for(x: float, y: float, mons: list[dict]) -> Optional[int]:
        for idx, m in enumerate(mons, 1):
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return idx
        return None

    async def _run_in_thread(self, func, *args, **kwargs):
        """Run a function in the custom thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._thread_pool, lambda: func(*args, **kwargs))

    def _detect_high_dpi(self) -> bool:
        """Detect if running on a high-DPI display and adjust settings."""
        try:
            # Check if any monitor has high resolution (likely Retina)
            with mss.mss() as sct:
                for monitor in sct.monitors[1:]:  # Skip monitor 0 (all monitors)
                    if monitor['width'] > 2560 or monitor['height'] > 1600:
                        return True
        except Exception:
            pass
        return False

    def _should_log_scroll(self, x: float, y: float, dx: float, dy: float) -> bool:
        """
        Determine if a scroll event should be logged based on filtering criteria.

        Returns True if the scroll event should be logged, False otherwise.
        """
        current_time = time.monotonic()
        magnitude = (dx * dx + dy * dy) ** 0.5
        if magnitude == 0:
            return False

        # Check if this is a new scroll session
        new_session = (self._scroll_last_input is None or
                       current_time - self._scroll_last_input > self._scroll_session_timeout)
        self._scroll_last_input = current_time
        if new_session:
            # Start new session
            self._scroll_session_start = current_time
            self._scroll_event_count = 0
            self._scroll_last_position = (x, y)
            self._scroll_last_time = current_time
            self._scroll_distance = 0
            return True

        # Trackpad scrolling normally leaves the pointer stationary. Measure
        # accumulated scroll deltas, not movement of the pointer.
        self._scroll_distance += magnitude
        # Check debounce time
        if (self._scroll_last_time is not None and
            current_time - self._scroll_last_time < max(
                self._scroll_debounce_sec,
                1 / self._scroll_max_frequency if self._scroll_max_frequency > 0 else 0,
            )):
            return False

        if self._scroll_distance < self._scroll_min_distance:
            return False
        self._scroll_event_count += 1
        self._scroll_distance = 0

        # Update tracking state
        self._scroll_last_position = (x, y)
        self._scroll_last_time = current_time

        return True

    async def _cleanup_key_screenshots(self) -> None:
        """Clean up intermediate keyboard screenshots, keeping only first and last."""
        if len(self._key_screenshots) <= 2:
            return

        # Keep first and last, delete the rest
        to_delete = self._key_screenshots[1:-1]
        self._key_screenshots = [self._key_screenshots[0], self._key_screenshots[-1]]

        for path in to_delete:
            try:
                await self._run_in_thread(os.remove, path)
                if self.debug:
                    logging.getLogger("Screen").info(f"Deleted intermediate screenshot: {path}")
            except OSError:
                pass  # File might already be deleted

    # ─────────────────────────────── I/O helpers
    async def _save_frame(self, frame, x, y, tag: str, box_color: str = "red", box_width: int = 10, *, monitor: dict | None = None) -> str:
        ts   = f"{time.time():.5f}"
        path = os.path.join(self.screens_dir, f"{ts}_{tag}.jpg")
        image = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)
        draw = ImageDraw.Draw(image)
        # x/y are monitor-local logical coordinates. Use this image's actual
        # dimensions: MSS may return nominal-resolution or Retina pixels.
        if monitor is not None:
            x *= frame.width / monitor['width']
            y *= frame.height / monitor['height']
        x = min(frame.width - 1, max(0, round(x)))
        y = min(frame.height - 1, max(0, round(y)))
        x1, x2 = max(0, x - 30), min(frame.width - 1, x + 30)
        y1, y2 = max(0, y - 20), min(frame.height - 1, y + 20)
        draw.rectangle([x1, y1, x2, y2], outline=box_color, width=box_width)

        # Save with lower quality to reduce memory usage
        await self._run_in_thread(
            image.save,
            path,
            "JPEG",
            quality=70,  # Reduced from 90 to 70
            optimize=True,  # Enable optimization
        )

        # Explicitly delete image objects to free memory
        del draw
        del image

        return path

    async def _process_and_emit(
        self, before_path: str, after_path: str | None,
        action: str | None, ev: dict | None,
    ) -> None:
        if "scroll" in action:
            # Include scroll delta information
            scroll_info = ev.get("scroll", (0, 0))
            step = f"scroll({ev['position'][0]:.1f}, {ev['position'][1]:.1f}, dx={scroll_info[0]:.2f}, dy={scroll_info[1]:.2f})"
            await self.update_queue.put(Update(content=step, content_type="input_text"))
        elif "click" in action:
            step = f"{action}({ev['position'][0]:.1f}, {ev['position'][1]:.1f})"
            await self.update_queue.put(Update(content=step, content_type="input_text"))
        else:
            step = f"{action}({ev['text']})"
            await self.update_queue.put(Update(content=step, content_type="input_text"))

    def _start_event(self, callback, *args) -> None:
        """Called on the event loop; track every listener callback until done."""
        if self._stopping:
            return
        task = asyncio.create_task(callback(*args))
        self._event_tasks.add(task)

        def finished(done):
            self._event_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logging.getLogger('Screen').error(
                    'An interaction could not be captured (%s). Check recording output.',
                    type(done.exception()).__name__,
                )
        task.add_done_callback(finished)

    async def _finish_key_session(self) -> None:
        """Caller holds the key lock; leave no background cleanup running."""
        await self._cleanup_key_screenshots()
        if len(self._key_screenshots) > 1:
            last_path = self._key_screenshots[-1]
            final_path = last_path.replace('_intermediate', '_final')
            if last_path != final_path:
                await self._run_in_thread(os.rename, last_path, final_path)
        self._key_activity_start = None
        self._key_screenshots = []

    async def stop(self) -> None:
        """Stop input, finish accepted events, then close capture and executor."""
        if self._stopped:
            return
        self._stopping = True
        for listener in self._listeners:
            listener.stop()
        for listener in self._listeners:
            await asyncio.to_thread(listener.join, 1)
        # MSS and the executor must stay open for in-flight screenshots.
        if self._event_tasks:
            await asyncio.gather(*tuple(self._event_tasks), return_exceptions=True)
        async with self._key_activity_lock:
            await self._finish_key_session()
        self._running = False
        if self._task:
            # Let the current grab finish before its MSS context closes.
            await asyncio.gather(self._task, return_exceptions=True)

        # Clean up frame objects
        async with self._frame_lock:
            for frame in self._frames.values():
                if frame is not None:
                    del frame
            self._frames.clear()

        # Force garbage collection
        await self._run_in_thread(gc.collect)

        # Shutdown thread pool
        if hasattr(self, '_thread_pool'):
            self._thread_pool.shutdown(wait=True)
        self._stopped = True

    # ─────────────────────────────── skip guard
    def _skip(self) -> bool:
        return _is_app_visible(self._guard) if self._guard else False

    # ─────────────────────────────── main async worker
    async def _worker(self) -> None:          # overrides base class
        log = logging.getLogger("Screen")
        if self.debug:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s [Screen] %(message)s", datefmt="%H:%M:%S")
        else:
            if not log.handlers:
                log.addHandler(logging.StreamHandler())
            log.setLevel(logging.WARNING)
            log.propagate = False

        CAP_FPS  = self._CAPTURE_FPS
        PERIOD   = self._PERIODIC_SEC
        DEBOUNCE = self._DEBOUNCE_SEC

        loop = asyncio.get_running_loop()

        key_event_count = 0

        # ------------------------------------------------------------------
        # All calls to mss / Quartz are wrapped in `to_thread`
        # ------------------------------------------------------------------
        with mss.mss() as sct:
            mons = sct.monitors[self._MON_START:]

            # ---- mouse callbacks (pynput is sync → schedule into loop) ----
            def schedule_event(x: float, y: float, typ: str):
                loop.call_soon_threadsafe(self._start_event, mouse_event, x, y, typ)

            def schedule_scroll_event(x: float, y: float, dx: float, dy: float):
                loop.call_soon_threadsafe(self._start_event, scroll_event, x, y, dx, dy)

            def schedule_key_event(key, typ: str):
                loop.call_soon_threadsafe(self._start_event, key_event, key, typ)

            mouse_listener = mouse.Listener(
                on_click=lambda x, y, btn, prs: schedule_event(x, y, f"click_{btn.name}") if prs else None,
                on_scroll=lambda x, y, dx, dy: schedule_scroll_event(x, y, dx, dy),
            )
            key_listener = keyboard.Listener(
                on_press=lambda key: schedule_key_event(key, "press"),
            )
            self._listeners = [mouse_listener, key_listener]
            mouse_listener.start()
            key_listener.start()

            # ---- nested helper inside the async context ----
            async def flush(ev):
                if self._skip():
                    return

                # Let the application apply the interaction before its after
                # screenshot. Each callback owns its own before/event pair.
                await asyncio.sleep(0.15)
                try:
                    aft = await self._run_in_thread(sct.grab, mons[ev["mon"] - 1])
                except Exception as e:
                    log.error('Failed to capture an after screenshot (%s). Check Screen Recording permission.', type(e).__name__)
                    return

                if "scroll" in ev["type"]:
                    scroll_info = ev.get("scroll", (0, 0))
                    step = f"scroll({ev['position'][0]:.1f}, {ev['position'][1]:.1f}, dx={scroll_info[0]:.2f}, dy={scroll_info[1]:.2f})"
                else:
                    step = f"{ev['type']}({ev['position'][0]:.1f}, {ev['position'][1]:.1f})"

                monitor = mons[ev['mon'] - 1]
                bef_path = await self._save_frame(ev["before"], ev["position"][0], ev["position"][1], f"{step}_before", monitor=monitor)
                aft_path = await self._save_frame(aft, ev["position"][0], ev["position"][1], f"{step}_after", monitor=monitor)
                await self._process_and_emit(bef_path, aft_path, ev["type"], ev)

                log.info(f"{ev['type']} captured on monitor {ev['mon']}")

            # def debounce_flush():
            #     # callback from loop.call_later → must create task
            #     asyncio.create_task(flush())

            # ---- keyboard event reception ----
            async def key_event(key, typ: str):
                # Get current mouse position to determine active monitor
                x, y = mouse.Controller().position
                idx = self._mon_for(x, y, mons)
                if idx is None:
                    return

                mon = mons[idx - self._MON_START]
                x = x - mon["left"]
                y = y - mon["top"]
                log.info(f"Key {typ}: {str(key)} on monitor {idx}")

                step = f"key_{typ}({str(key)})"
                await self.update_queue.put(
                    Update(content=step, content_type="input_text")
                )

                async with self._frame_lock:
                    frame = self._frames.get(idx)
                if frame is None:
                    log.warning('No screenshot available for keyboard event. Check Screen Recording permission.')
                    return

                async with self._key_activity_lock:
                    current_time = time.time()

                    # Check if this is the start of a new keyboard session
                    if (self._key_activity_start is None or
                        current_time - self._key_activity_start > self._key_activity_timeout):
                        # Start new session - save first screenshot
                        await self._finish_key_session()
                        screenshot_path = await self._save_frame(frame, x, y, f"{step}_first", monitor=mon)
                        self._key_screenshots.append(screenshot_path)
                        log.info(f"Started new keyboard session, saved first screenshot: {screenshot_path}")
                    else:
                        # Continue existing session - save intermediate screenshot
                        screenshot_path = await self._save_frame(frame, x, y, f"{step}_intermediate", monitor=mon)
                        self._key_screenshots.append(screenshot_path)
                        log.info(f"Continued keyboard session, saved intermediate screenshot: {screenshot_path}")

                    self._key_activity_start = current_time
                    await self._cleanup_key_screenshots()

            # ---- scroll event reception ----
            async def scroll_event(x: float, y: float, dx: float, dy: float):
                idx = self._mon_for(x, y, mons)
                if idx is None or self._skip():
                    return
                # Apply scroll filtering
                async with self._scroll_lock:
                    if not self._should_log_scroll(x, y, dx, dy):
                        if self.debug:
                            log.info(f"Scroll filtered out: dx={dx:.2f}, dy={dy:.2f}")
                        return

                mon = mons[idx - self._MON_START]
                x = x - mon["left"]
                y = y - mon["top"]

                log.info(f"Scroll @({x:7.1f},{y:7.1f}) dx={dx:.2f} dy={dy:.2f} → mon={idx}")

                if self._skip():
                    return

                async with self._frame_lock:
                    bf = self._frames.get(idx)
                    if bf is None:
                        return
                    ev = {"type": "scroll", "position": (x, y), "mon": idx, "before": bf, "scroll": (dx, dy)}

                # Process event immediately
                await flush(ev)

            # ---- mouse event reception ----
            async def mouse_event(x: float, y: float, typ: str):
                idx = self._mon_for(x, y, mons)
                if idx is None or self._skip():
                    return
                mon = mons[idx - self._MON_START]
                x = x - mon["left"]
                y = y - mon["top"]
                log.info(
                    f"{typ:<6} @({x:7.1f},{y:7.1f}) → mon={idx}   {'(guarded)' if self._skip() else ''}"
                )
                if self._skip() or idx is None:
                    return

                async with self._frame_lock:
                    bf = self._frames.get(idx)
                    if bf is None:
                        return
                    ev = {"type": typ, "position": (x, y), "mon": idx, "before": bf}

                # Process event immediately instead of using debounce
                await flush(ev)

            # ---- main capture loop ----
            log.info(f"Screen observer started — guarding {self._guard or '∅'}")
            last_periodic = time.time()
            frame_count = 0

            while self._running:                         # flag from base class
                t0 = time.time()

                # refresh 'before' buffers
                for idx, m in enumerate(mons, 1):
                    old_frame = None
                    async with self._frame_lock:
                        old_frame = self._frames.get(idx)

                    # Capture new frame using custom thread pool
                    try:
                        frame = await self._run_in_thread(sct.grab, m)
                    except Exception as e:
                        if not getattr(self, '_capture_warning_shown', False):
                            log.error('Screen capture failed (%s). Check Screen Recording permission and restart the terminal.', type(e).__name__)
                            self._capture_warning_shown = True
                        continue

                    async with self._frame_lock:
                        self._frames[idx] = frame

                    # Explicitly delete old frame to free memory
                    if old_frame is not None:
                        del old_frame

                    frame_count += 1

                    # Force garbage collection every 30 frames to prevent memory buildup
                    if frame_count % self._MEMORY_CLEANUP_INTERVAL == 0:
                        await self._run_in_thread(gc.collect)

                # Check for keyboard session timeout
                current_time = time.time()
                if (self._key_activity_start is not None and
                    current_time - self._key_activity_start > self._key_activity_timeout and
                    self._key_screenshots):
                    # Session ended - rename last screenshot to indicate it's the final one
                    async with self._key_activity_lock:
                        if (self._key_activity_start is not None and
                            time.time() - self._key_activity_start > self._key_activity_timeout):
                            await self._finish_key_session()

                # fps throttle
                dt = time.time() - t0
                await asyncio.sleep(max(0, (1 / CAP_FPS) - dt))

            # shutdown
            mouse_listener.stop()
            key_listener.stop()

            # Final cleanup of any remaining keyboard session
            if self._key_activity_start is not None and len(self._key_screenshots) > 1:
                async with self._key_activity_lock:
                    await self._finish_key_session()

            # if self._debounce_handle:
            #     self._debounce_handle.cancel()
