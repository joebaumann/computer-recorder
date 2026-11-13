# Modified for the recorder-only fork: removed unused processing hooks.
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from .models import Observation, init_db
from .observers import Observer
from .schemas import Update

class crec:
    def __init__(
        self,
        user_name: str,
        *observers: Observer,
        data_directory: str = "~/Downloads/records",
        db_name: str = "actions.db",
        max_concurrent_updates: int = 4,
        verbosity: int = logging.INFO,
    ):
        # basic paths
        data_directory = os.path.expanduser(data_directory)
        os.makedirs(data_directory, exist_ok=True)

        # runtime
        self.user_name = user_name
        self.observers: list[Observer] = list(observers)

        # logging
        self.logger = logging.getLogger("crec")
        self.logger.setLevel(verbosity)
        if not self.logger.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            self.logger.addHandler(h)

        self.engine = None
        self.Session = None
        self._db_name        = db_name
        self._data_directory = data_directory

        self._update_sem = asyncio.Semaphore(max_concurrent_updates)
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None

    def start_update_loop(self):
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._update_loop())

    async def stop_update_loop(self):
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def connect_db(self):
        if self.engine is None:
            self.engine, self.Session = await init_db(
                self._db_name, self._data_directory
            )

    async def __aenter__(self):
        await self.connect_db()
        self.start_update_loop()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # Finish screenshots and their associated events while the sink is
        # still running. Otherwise the final interaction can be discarded.
        for obs in self.observers:
            await obs.stop()
        await self.stop_update_loop()

        # wait for any in-flight handlers
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        for obs in self.observers:
            while not obs.update_queue.empty():
                await self._default_handler(obs, obs.update_queue.get_nowait())

        if self.engine is not None:
            await self.engine.dispose()

    async def _update_loop(self):
        """
        Efficiently wait for *any* observer to produce an Update and
        dispatch it through the semaphore-guarded handler.
        """
        gets = {asyncio.create_task(obs.update_queue.get()): obs for obs in self.observers}
        try:
            while gets:
                done, _ = await asyncio.wait(gets, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    obs = gets.pop(fut)
                    t = asyncio.create_task(self._run_with_gate(obs, fut.result()))
                    self._tasks.add(t)
                    gets[asyncio.create_task(obs.update_queue.get())] = obs
        finally:
            # A queue getter may have consumed the final event just before
            # cancellation. Persist it, and cancel only still-pending getters.
            for fut in gets:
                if not fut.done():
                    fut.cancel()
            await asyncio.gather(*gets, return_exceptions=True)
            for fut, obs in gets.items():
                if not fut.cancelled() and fut.exception() is None:
                    await self._run_with_gate(obs, fut.result())

    async def _run_with_gate(self, observer: Observer, update: Update):
        """Wrapper that enforces max_concurrent_updates."""
        async with self._update_sem:
            try:
                await self._default_handler(observer, update)
            finally:
                self._tasks.discard(asyncio.current_task())

    async def _default_handler(self, observer: Observer, update: Update) -> None:
        self.logger.info(f"Processing update from {observer.name}")
        self.logger.info(f"Content ({update.content_type}): {update.content[:10]}")

        async with self._session() as session:
            observation = Observation(
                observer_name=observer.name,
                content=update.content,
                content_type=update.content_type,
            )

            session.add(observation)
            await session.flush()

    @asynccontextmanager
    async def _session(self):
        async with self.Session() as s:
            async with s.begin():
                yield s

    def add_observer(self, observer: Observer):
        self.observers.append(observer)

    def remove_observer(self, observer: Observer):
        if observer in self.observers:
            self.observers.remove(observer)
