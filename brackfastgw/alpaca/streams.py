"""Websocket streams: stock market data and the account's trade updates.

* ``StockDataStream`` - wss://stream.data.alpaca.markets/v2/{feed}
* ``TradingStream``   - wss://(paper-)api.alpaca.markets/stream (trade_updates)

Both connect, authenticate and reconnect with backoff by themselves; market data
subscriptions are restored after a reconnect::

	stream.on(Bar, on_bar, symbols=["AAPL", "MSFT"])     # handler + subscription
	@stream.on(Quote, symbols=["AAPL"])                  # decorator form
	async def on_quote(q: Quote): ...
	await stream.start()                                 # returns once authenticated
	...
	await stream.stop()
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

import aiohttp
import msgspec

from .config import PAPER, Credentials, Endpoints
from .errors import StreamError
from .handlers import Handler, HandlerHandle, Handlers, Predicate
from .http import SessionProvider
from .models.stream import (
	CHANNELS, AuthorizationMsg, Error, ListeningMsg, StockMessage, StreamConnected, StreamDisconnected,
	StreamReady, Subscription, Success, TradeUpdate, TradeUpdateMsg, TradingMessage,
)

# market data error codes after which reconnecting cannot help
FATAL_CODES = {402: "auth failed", 409: "insufficient subscription"}


class Stream:
	"""Connection lifecycle shared by both streams.

	Subclasses implement ``_on_open`` (connected: authenticate), ``_on_payload`` (decode,
	``_set_ready()`` once authenticated, dispatch) and optionally ``_on_ready`` (subscribe).
	"""

	log_name = "stream"

	def __init__(self, url: str, credentials: Credentials, *, session: SessionProvider | None = None,
			msgpack: bool = False, reconnect: bool = True, min_backoff: float = 1.0, max_backoff: float = 30.0,
			ready_timeout: float = 10.0, heartbeat: float = 20.0):
		self.url = url
		self._credentials = credentials
		self._provider = session
		self._own_session: aiohttp.ClientSession | None = None
		self._msgpack = msgpack
		self._reconnect = reconnect
		self._min_backoff = min_backoff
		self._max_backoff = max_backoff
		self._ready_timeout = ready_timeout
		self._heartbeat = heartbeat
		self._log = logging.getLogger(f"brackfastgw.{self.log_name}")
		self._handlers = Handlers(self._log)
		self._ws: aiohttp.ClientWebSocketResponse | None = None
		self._outbox: asyncio.Queue | None = None     # per connection, written in order by _write
		self._ready = asyncio.Event()
		self._closing = False
		self._fatal: BaseException | None = None
		self._task: asyncio.Task | None = None

	# -- handlers -----------------------------------------------------------------

	def add_handler(self, msg_type: type, handler: Handler, predicate: Predicate | None = None) -> HandlerHandle:
		return self._handlers.add(msg_type, handler, predicate)

	def remove_handler(self, handle: HandlerHandle) -> bool:
		return self._handlers.remove(handle)

	def on_event(self, msg_type: type, handler: Handler | None = None, *, predicate: Predicate | None = None) -> Any:
		"""Register a handler for ``msg_type``; directly or as a decorator."""
		def register(func: Handler) -> Handler:
			self.add_handler(msg_type, func, predicate)
			return func
		return register if handler is None else register(handler)

	def on_connected(self, handler: Handler | None = None) -> Any:
		return self.on_event(StreamConnected, handler)

	def on_ready(self, handler: Handler | None = None) -> Any:
		return self.on_event(StreamReady, handler)

	def on_disconnected(self, handler: Handler | None = None) -> Any:
		return self.on_event(StreamDisconnected, handler)

	# -- state / sending ----------------------------------------------------------

	@property
	def is_ready(self) -> bool:
		return self._ws is not None and self._ready.is_set()

	async def wait_ready(self) -> None:
		await self._ready.wait()

	def send(self, obj: Any) -> bool:
		"""Queue a control message (sent in order). False when not connected."""
		if self._outbox is None:
			return False
		self._outbox.put_nowait(msgspec.msgpack.encode(obj) if self._msgpack else msgspec.json.encode(obj))
		return True

	# -- subclass hooks -----------------------------------------------------------

	def _headers(self) -> dict[str, str]:
		return {}

	def _on_open(self) -> None:
		pass

	def _on_payload(self, payload: bytes | str) -> None:
		raise NotImplementedError

	def _on_ready(self) -> None:
		pass

	def _set_ready(self) -> None:
		if self._ready.is_set():
			return
		self._ready.set()
		self._log.info("ready: %s", self.url)
		self._on_ready()
		self._handlers.dispatch(StreamReady(url=self.url))

	def _fail(self, exc: BaseException) -> None:
		"""Fatal: stop reconnecting; start() / run() raise ``exc``."""
		self._log.error("%s: %s", self.url, exc)
		self._fatal = exc
		if self._ws is not None:
			asyncio.get_running_loop().create_task(self._ws.close())

	# -- connection loop ----------------------------------------------------------

	async def _session(self) -> aiohttp.ClientSession:
		if self._provider is not None:
			return await self._provider()
		if self._own_session is None or self._own_session.closed:
			self._own_session = aiohttp.ClientSession()
		return self._own_session

	async def _write(self, ws: aiohttp.ClientWebSocketResponse, outbox: asyncio.Queue) -> None:
		while True:
			data = await outbox.get()
			if self._msgpack:
				await ws.send_bytes(data)
			else:
				await ws.send_str(data.decode())

	async def _ready_or_close(self, ws: aiohttp.ClientWebSocketResponse) -> None:
		try:
			await asyncio.wait_for(self._ready.wait(), self._ready_timeout)
		except TimeoutError:
			self._log.warning("%s: not ready after %.0f s, reconnecting", self.url, self._ready_timeout)
			await ws.close()

	async def _connection(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
		"""Serve one connection until it closes. True if it got ready."""
		self._ws, self._outbox = ws, asyncio.Queue()
		writer = asyncio.create_task(self._write(ws, self._outbox))
		watchdog = asyncio.create_task(self._ready_or_close(ws))
		self._log.info("connected to %s", self.url)
		self._handlers.dispatch(StreamConnected(url=self.url))
		try:
			self._on_open()
			async for msg in ws:
				if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
					try:
						self._on_payload(msg.data)
					except Exception:
						self._log.exception("failed to handle a frame from %s", self.url)
				elif msg.type == aiohttp.WSMsgType.ERROR:
					self._log.warning("%s: %r", self.url, ws.exception())
					break
			if ws.close_code is not None and not self._closing:
				self._log.info("%s closed: %s", self.url, ws.close_code)
			return self._ready.is_set()
		finally:
			writer.cancel()
			watchdog.cancel()
			self._ws = self._outbox = None
			self._ready.clear()
			if not ws.closed:
				await ws.close()
			self._log.info("disconnected from %s", self.url)
			self._handlers.dispatch(StreamDisconnected(url=self.url))

	async def run(self) -> None:
		"""Connect and reconnect until ``stop()`` or a fatal error."""
		backoff = self._min_backoff
		while not self._closing:
			try:
				session = await self._session()
				ws = await session.ws_connect(self.url, headers=self._headers() or None,
					heartbeat=self._heartbeat, max_msg_size=16 * 1024 * 1024)
			except asyncio.CancelledError:
				raise
			except Exception as exc:
				self._log.warning("connect to %s failed: %r", self.url, exc)
			else:
				if await self._connection(ws):
					backoff = self._min_backoff
			if self._fatal is not None:
				raise self._fatal
			if self._closing or not self._reconnect:
				break
			self._log.info("reconnecting to %s in %.1f s", self.url, backoff)
			await asyncio.sleep(backoff)
			backoff = min(backoff * 2, self._max_backoff)

	async def start(self, timeout: float | None = None) -> None:
		"""Run the connection loop in the background; return once ready."""
		if self._task is None or self._task.done():
			self._closing = False
			self._fatal = None
			self._task = asyncio.create_task(self.run(), name=f"stream {self.url}")
		ready = asyncio.ensure_future(self._ready.wait())
		done, _ = await asyncio.wait((ready, self._task), timeout=timeout or self._ready_timeout * 3,
			return_when=asyncio.FIRST_COMPLETED)
		if ready in done:
			return
		ready.cancel()
		if self._task in done:
			self._task.result()    # raises the fatal error
			raise ConnectionError(f"{self.url}: stream stopped before it was ready")
		await self.stop()
		raise TimeoutError(f"{self.url}: not ready in time")

	async def stop(self) -> None:
		self._closing = True
		task, self._task = self._task, None
		ws = self._ws
		if ws is not None:
			await ws.close()
		if task is not None and not task.done():
			if ws is None:
				task.cancel()    # connecting or waiting to reconnect
			try:
				await task
			except (asyncio.CancelledError, StreamError):
				pass
			except Exception:
				self._log.exception("stream task ended with an error")
		await self._handlers.aclose()
		if self._own_session is not None and not self._own_session.closed:
			await self._own_session.close()
		self._own_session = None

	async def drain(self) -> None:
		"""Wait until the async handlers have processed everything received so far."""
		await self._handlers.drain()

	async def __aenter__(self):
		await self.start()
		return self

	async def __aexit__(self, *exc) -> None:
		await self.stop()


def _as_list(values: Iterable[str] | str | None) -> list[str] | None:
	if values is None:
		return None
	return [values] if isinstance(values, str) else list(values)


class StockDataStream(Stream):
	"""Stock trades, quotes, bars, statuses. ``feed``: "iex" (free), "sip" (paid), "delayed_sip"; "test" streams FAKEPACA."""

	log_name = "stocks"

	def __init__(self, credentials: Credentials, *, feed: str = "iex", endpoints: Endpoints = PAPER,
			msgpack: bool = True, **kw):
		super().__init__(f"{endpoints.data_stream}/v2/{feed}", credentials, msgpack=msgpack, **kw)
		self.feed = feed
		mod = msgspec.msgpack if msgpack else msgspec.json
		self._frame = mod.Decoder(list[StockMessage])
		self._raw = mod.Decoder(list[msgspec.Raw])
		self._item = mod.Decoder(StockMessage)
		self._subs: dict[str, set[str]] = {channel: set() for channel in CHANNELS.values()}
		self.confirmed: Subscription | None = None    # what the server last confirmed

	# -- handlers + subscriptions -------------------------------------------------

	def on(self, msg_type: type, handler: Handler | None = None, *, symbols: Iterable[str] | str | None = None) -> Any:
		"""Register ``handler`` for Trade / Quote / Bar / ... and subscribe ``symbols`` (["*"] = all).

		Without ``symbols`` the handler gets every message of that type from the existing
		subscriptions.
		"""
		channel = self._channel(msg_type)
		wanted = _as_list(symbols)
		predicate = None
		if wanted and "*" not in wanted:
			allowed = frozenset(wanted)
			predicate = lambda msg: msg.symbol in allowed

		def register(func: Handler) -> Handler:
			self.add_handler(msg_type, func, predicate)
			if wanted:
				self.subscribe(channel, *wanted)
			return func
		return register if handler is None else register(handler)

	@staticmethod
	def _channel(msg_type: type | str) -> str:
		if isinstance(msg_type, str):
			if msg_type not in CHANNELS.values():
				raise ValueError(f"unknown channel {msg_type!r}; channels: {', '.join(CHANNELS.values())}")
			return msg_type
		try:
			return CHANNELS[msg_type]
		except KeyError:
			raise ValueError(f"{msg_type.__name__} is not a stock stream message") from None

	def subscribe(self, msg_type: type | str, *symbols: str) -> None:
		"""Add symbols to a channel, given as message type (Bar) or channel name ("bars")."""
		channel = self._channel(msg_type)
		current = self._subs[channel]
		new = [s for s in dict.fromkeys(symbols) if s not in current]
		current.update(new)
		if new and self.is_ready:
			self.send({"action": "subscribe", channel: new})

	def unsubscribe(self, msg_type: type | str, *symbols: str) -> None:
		channel = self._channel(msg_type)
		current = self._subs[channel]
		gone = [s for s in dict.fromkeys(symbols) if s in current]
		current.difference_update(gone)
		if gone and self.is_ready:
			self.send({"action": "unsubscribe", channel: gone})

	@property
	def subscriptions(self) -> dict[str, list[str]]:
		"""The requested subscriptions: {channel: sorted symbols}, empty channels left out."""
		return {channel: sorted(symbols) for channel, symbols in self._subs.items() if symbols}

	# -- protocol -----------------------------------------------------------------

	def _headers(self) -> dict[str, str]:
		return {"Content-Type": "application/msgpack"} if self._msgpack else {}

	def _on_ready(self) -> None:
		if subs := self.subscriptions:
			self.send({"action": "subscribe", **subs})

	def _decode(self, payload: bytes | str) -> list[Any]:
		try:
			return self._frame.decode(payload)
		except msgspec.ValidationError:
			pass
		out = []    # a message type this version does not know: skip it, keep the others
		for raw in self._raw.decode(payload):
			try:
				out.append(self._item.decode(raw))
			except msgspec.ValidationError as exc:
				self._log.debug("skipping a message (%s): %r", exc, bytes(raw)[:200])
		return out

	def _on_payload(self, payload: bytes | str) -> None:
		dispatch = self._handlers.dispatch
		for msg in self._decode(payload):
			cls = type(msg)
			if cls is Success:
				if msg.msg == "connected":
					self.send(self._credentials.auth_message())
				elif msg.msg == "authenticated":
					self._set_ready()
			elif cls is Error:
				if msg.code in FATAL_CODES:
					self._fail(StreamError(msg.code, msg.msg))
				else:
					self._log.warning("stream error %d: %s", msg.code, msg.msg)
			elif cls is Subscription:
				self.confirmed = msg
			dispatch(msg)


class TradingStream(Stream):
	"""Order events (trade_updates) of the account behind the API key. JSON only."""

	log_name = "orders"

	def __init__(self, credentials: Credentials, *, endpoints: Endpoints = PAPER, **kw):
		kw["msgpack"] = False
		super().__init__(endpoints.trading_stream, credentials, **kw)
		self._decoder = msgspec.json.Decoder(TradingMessage)

	def on_trade_update(self, handler: Handler | None = None, *, events: Iterable[str] | str | None = None,
			symbols: Iterable[str] | str | None = None) -> Any:
		"""Register a TradeUpdate handler, optionally only for some events and / or symbols."""
		ev = None if events is None else frozenset(_as_list(events))
		sy = None if symbols is None else frozenset(_as_list(symbols))
		predicate = None
		if ev is not None or sy is not None:
			predicate = lambda u: (ev is None or u.event in ev) and (sy is None or u.order.symbol in sy)
		return self.on_event(TradeUpdate, handler, predicate=predicate)

	def _on_open(self) -> None:
		self.send(self._credentials.auth_message())

	def _on_payload(self, payload: bytes | str) -> None:
		try:
			msg = self._decoder.decode(payload)
		except msgspec.ValidationError as exc:
			self._log.debug("skipping a message (%s): %r", exc, payload[:300])
			return
		cls = type(msg)
		if cls is TradeUpdateMsg:
			self._handlers.dispatch(msg.data)
		elif cls is AuthorizationMsg:
			if msg.data.status == "authorized":
				self.send({"action": "listen", "data": {"streams": ["trade_updates"]}})
			else:
				self._fail(StreamError(None, f"trading stream authorization: {msg.data.status}"))
		elif cls is ListeningMsg:
			if msg.data.error:
				self._log.warning("listen error: %s", msg.data.error)
			if "trade_updates" in msg.data.streams:
				self._set_ready()
			self._handlers.dispatch(msg)
