"""Handlers per message type.

- A plain function is called inline, while the frame is decoded: no scheduling, but it must be
  quick and must not block.
- An async function gets the messages through its own queue, one at a time and in arrival
  order (a partial fill always before the fill that follows it).

Exceptions in handlers are logged; they never reach the stream.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

Handler = Callable[[Any], None] | Callable[[Any], Awaitable[None]]
Predicate = Callable[[Any], bool]


class HandlerHandle:
	"""Returned by ``Handlers.add``; pass it to ``remove``."""

	__slots__ = ("msg_type", "func", "predicate", "is_async", "queue", "worker")

	def __init__(self, msg_type: type, func: Handler, predicate: Predicate | None):
		self.msg_type = msg_type
		self.func = func
		self.predicate = predicate
		self.is_async = inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(getattr(func, "__call__", None))
		self.queue: asyncio.Queue | None = None
		self.worker: asyncio.Task | None = None

	def __repr__(self) -> str:
		name = getattr(self.func, "__qualname__", repr(self.func))
		return f"<handler {self.msg_type.__name__} -> {name} ({'async' if self.is_async else 'inline'})>"


class Handlers:
	def __init__(self, log: logging.Logger):
		self._log = log
		self._by_type: dict[type, tuple[HandlerHandle, ...]] = {}

	def add(self, msg_type: type, func: Handler, predicate: Predicate | None = None) -> HandlerHandle:
		handle = HandlerHandle(msg_type, func, predicate)
		self._by_type[msg_type] = (*self._by_type.get(msg_type, ()), handle)    # copy on write: dispatch never sees a change
		return handle

	def remove(self, handle: HandlerHandle) -> bool:
		handles = self._by_type.get(handle.msg_type, ())
		if handle not in handles:
			return False
		rest = tuple(h for h in handles if h is not handle)
		if rest:
			self._by_type[handle.msg_type] = rest
		else:
			del self._by_type[handle.msg_type]
		if handle.worker is not None:
			handle.worker.cancel()
			handle.worker = None
		return True

	def all(self) -> list[HandlerHandle]:
		return [h for handles in self._by_type.values() for h in handles]

	def dispatch(self, msg: Any) -> None:
		for h in self._by_type.get(type(msg), ()):
			if h.predicate is not None:
				try:
					if not h.predicate(msg):
						continue
				except Exception:
					self._log.exception("predicate of %r failed", h)
					continue
			if h.is_async:
				if h.worker is None or h.worker.done():
					h.queue = h.queue or asyncio.Queue()
					h.worker = asyncio.get_running_loop().create_task(self._work(h))
				h.queue.put_nowait(msg)  # type: ignore[union-attr]
			else:
				try:
					h.func(msg)
				except Exception:
					self._log.exception("%r failed", h)

	async def _work(self, h: HandlerHandle) -> None:
		queue = h.queue
		assert queue is not None
		while True:
			msg = await queue.get()
			try:
				await h.func(msg)  # type: ignore[misc]
			except Exception:
				self._log.exception("%r failed", h)
			finally:
				queue.task_done()

	async def drain(self) -> None:
		"""Wait until the async handlers have processed everything queued so far."""
		for h in self.all():
			if h.queue is not None and h.worker is not None and not h.worker.done():
				await h.queue.join()

	async def aclose(self) -> None:
		"""Stop the async handlers' workers (queued messages are dropped)."""
		workers = []
		for h in self.all():
			if h.worker is not None:
				h.worker.cancel()
				workers.append(h.worker)
				h.worker = None
			h.queue = None
		if workers:
			await asyncio.gather(*workers, return_exceptions=True)
