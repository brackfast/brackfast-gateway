"""Yahoo Finance as the gateway's market data: history, the instrument's name, live prices. No account, no key.

It stands where ``market.MarketRelay`` stands for Alpaca (the same methods), with the simulated account as the
broker (``sim.py``): Yahoo has no orders.

**Unofficial.** Yahoo has no public API: this uses what its web site uses (the chart endpoint and the price stream),
which Yahoo may change or block at any time, and whose terms are Yahoo's. Nothing about the user is sent.

- History: ``/v8/finance/chart``. 1m bars reach 30 days back (8 days a request), 2m to 30m 60 days, daily and longer
  as far as there is. Extended hours are included. 1h is built from 30m bars: Yahoo's own hourly bars start at the
  half hour, the protocol's at the hour (UTC), as the client folds live bars. Daily, weekly and monthly bars are put
  at New York midnight of their day (Yahoo: the open, 09:30), as Alpaca's. Prices are split adjusted.
- What the endpoint sends besides: the last row is the price now at its own time (not a bar's start), rows can come
  twice; ``bars_of`` puts every row into its bar.
- Live: the price stream (a websocket, protobuf in base64) sends each symbol's last price. It is sent on as a quote
  with that price as bid and ask (Yahoo has no bid / ask there), and folded into the forming 1m bar. After each
  minute the chart endpoint is asked for that minute's bar (and the two before it), sent as complete bars; when it
  has none, the bar built from the prices is the complete one. Overnight prices (20:00 to 04:00 New York) are
  quotes only: the history has no bars there.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import aiohttp
import msgspec

from ..protocol import Bar, BarData, Bars, GetBars, Quote
from .barcache import BarCache

NEW_YORK = ZoneInfo("America/New_York")
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
STREAM_URL = "wss://streamer.finance.yahoo.com/?version=2"
# Yahoo answers a client without a browser's User-Agent with 429
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
	"Chrome/130.0 Safari/537.36"}
MAX_BARS = 50_000
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)

# timeframe -> (Yahoo's interval, a bar's length, days a request may span, days it reaches back); None: no limit
INTERVALS: dict[str, tuple[str, timedelta, int | None, int | None]] = {
	"1m": ("1m", MINUTE, 7, 29),
	"2m": ("2m", 2 * MINUTE, 59, 59),
	"5m": ("5m", 5 * MINUTE, 59, 59),
	"15m": ("15m", 15 * MINUTE, 59, 59),
	"30m": ("30m", 30 * MINUTE, 59, 59),
	"1h": ("30m", timedelta(hours=1), 59, 59),       # built from 30m bars
	"1D": ("1d", DAY, None, None),
	"1W": ("1wk", timedelta(weeks=1), None, None),
	"1M": ("1mo", timedelta(days=31), None, None),
}
OVERNIGHT = 4                                    # the stream's market hours: 0 pre, 1 regular, 2 post, 4 overnight
COMPLETE_DELAY = 5.0                             # s after a minute before its bar is asked from the chart endpoint


class YahooError(Exception):
	pass


def yahoo_symbol(symbol: str) -> str:
	"""BRK.B is BRK-B at Yahoo."""
	return symbol.replace(".", "-")


def lookback(timeframe: str, limit: int) -> timedelta:
	"""How far back ``limit`` bars reach at most (as market.lookback)."""
	step = INTERVALS[timeframe][1]
	if step < DAY:
		return max(step * limit * 3, timedelta(days=5))
	return step * limit * 1.6 + timedelta(days=7)


# -- the chart endpoint's answer -------------------------------------------------------------

def _bucket(ts: datetime, timeframe: str) -> datetime:
	"""The start of the bar a row belongs to."""
	step = INTERVALS[timeframe][1]
	if step < DAY:
		seconds = int(step.total_seconds())
		return datetime.fromtimestamp(int(ts.timestamp()) // seconds * seconds, timezone.utc)
	day: date = ts.astimezone(NEW_YORK).date()
	if timeframe == "1W":
		day -= timedelta(days=day.weekday())         # its Monday
	elif timeframe == "1M":
		day = day.replace(day=1)
	return datetime.combine(day, dtime(0), NEW_YORK).astimezone(timezone.utc)


def bars_of(result: dict, timeframe: str) -> list[BarData]:
	"""The bars of a chart answer's ``result``, oldest first. Rows without a price are left out; a row sent twice
	counts once; several rows of one bar (the last row is the price now, at its own time) are one bar: the first
	one's open and volume, the highest high, the lowest low, the last close."""
	stamps = result.get("timestamp") or []
	quotes = (result.get("indicators") or {}).get("quote") or [{}]
	q = quotes[0] or {}
	rows: dict[int, tuple] = {}
	for i, ts in enumerate(stamps):
		values = [(q.get(k) or [None] * len(stamps))[i] for k in ("open", "high", "low", "close", "volume")]
		if any(v is None for v in values[:4]):
			continue
		rows[ts] = values                            # the same time again: the later row
	out: dict[datetime, BarData] = {}
	for ts in sorted(rows):
		o, h, l, c, v = rows[ts]
		start = _bucket(datetime.fromtimestamp(ts, timezone.utc), timeframe)
		bar = out.get(start)
		if bar is None:
			out[start] = BarData(start, float(o), float(h), float(l), float(c), float(v or 0))
		else:
			bar.high, bar.low, bar.close = max(bar.high, float(h)), min(bar.low, float(l)), float(c)
	return list(out.values())


def hourly(bars: list[BarData]) -> list[BarData]:
	"""30m bars (oldest first) as 1h bars starting at the hour (UTC)."""
	out: list[BarData] = []
	for b in bars:
		start = b.ts.replace(minute=0, second=0, microsecond=0)
		if out and out[-1].ts == start:
			last = out[-1]
			last.high, last.low, last.close, last.volume = max(last.high, b.high), min(last.low, b.low), b.close, last.volume + b.volume
		else:
			out.append(BarData(start, b.open, b.high, b.low, b.close, b.volume))
	return out


# -- the price stream's messages -------------------------------------------------------------

def _fields(data: bytes) -> dict[int, Any]:
	"""A protobuf message's fields: varints as int, 32 bit as float, 64 bit as double, the rest as bytes."""
	out: dict[int, Any] = {}
	i, n = 0, len(data)

	def varint() -> int:
		nonlocal i
		value = shift = 0
		while True:
			byte = data[i]
			i += 1
			value |= (byte & 0x7F) << shift
			if not byte & 0x80:
				return value
			shift += 7

	while i < n:
		key = varint()
		field, kind = key >> 3, key & 7
		if kind == 0:
			out[field] = varint()
		elif kind == 5:
			out[field] = struct.unpack_from("<f", data, i)[0]
			i += 4
		elif kind == 1:
			out[field] = struct.unpack_from("<d", data, i)[0]
			i += 8
		elif kind == 2:
			size = varint()
			out[field] = data[i:i + size]
			i += size
		else:
			raise ValueError(f"protobuf wire type {kind}")
	return out


def _zigzag(value: int) -> int:
	return (value >> 1) ^ -(value & 1)


class Tick(msgspec.Struct):
	"""One message of the price stream (Yahoo's PricingData: id 1, price 2, time 3, market hours 7, day volume 9,
	bid 23, ask 25)."""
	symbol: str
	price: float
	ts: datetime
	hours: int = 1
	day_volume: float | None = None
	bid: float = 0.0
	ask: float = 0.0


def tick_of(message: str) -> Tick | None:
	"""A stream message (JSON with the protobuf in base64, or the base64 alone); None: not a price."""
	if message.startswith("{"):
		wrapped = msgspec.json.decode(message)
		if wrapped.get("type") not in (None, "pricing"):
			return None
		message = wrapped.get("message") or ""
	f = _fields(base64.b64decode(message))
	if 1 not in f or 2 not in f:
		return None
	ms = _zigzag(f[3]) if 3 in f else 0
	ts = datetime.fromtimestamp(ms / 1000, timezone.utc) if ms > 0 else datetime.now(timezone.utc)
	return Tick(symbol=f[1].decode(), price=round(float(f[2]), 4), ts=ts, hours=f.get(7, 1),
		day_volume=float(_zigzag(f[9])) if 9 in f else None, bid=round(float(f.get(23, 0.0)), 4),
		ask=round(float(f.get(25, 0.0)), 4))


class _Forming:
	__slots__ = ("ts", "open", "high", "low", "close", "volume", "dirty")

	def __init__(self, ts: datetime, price: float) -> None:
		self.ts = ts
		self.open = self.high = self.low = self.close = price
		self.volume = 0.0
		self.dirty = True

	def add(self, price: float, volume: float) -> None:
		self.high, self.low, self.close = max(self.high, price), min(self.low, price), price
		self.volume += volume
		self.dirty = True

	def bar(self, symbol: str, complete: bool = False) -> Bar:
		return Bar(symbol=symbol, ts=self.ts, open=self.open, high=self.high, low=self.low, close=self.close,
			volume=self.volume, complete=complete)


# -- the relay ---------------------------------------------------------------------------------

class YahooRelay:
	provider = "Yahoo Finance"
	feed = "yahoo"
	source = "yahoo"                             # the bar cache's file

	def __init__(self, *, publish: Callable[[str, str, Any], None] = lambda *a: None,
			on_state: Callable[[], None] = lambda: None, cache: BarCache | None = None, bar_interval: float = 0.25,
			quote_interval: float = 0.1, complete_delay: float = COMPLETE_DELAY, log: logging.Logger | None = None,
			clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
		self.publish = publish                    # (kind "bar" / "quote", symbol, message); the gateway sets both
		self.on_state = on_state                  # the stream connected / disconnected
		self.cache = cache
		self.bar_interval = bar_interval
		self.quote_interval = quote_interval
		self.complete_delay = complete_delay
		self.log = log or logging.getLogger("alpacagw.yahoo")
		self.clock = clock
		self.connected = False
		self._session: aiohttp.ClientSession | None = None
		self._requests = asyncio.Semaphore(2)     # chart requests at a time
		self._names: dict[str, str] = {}
		self._quote_refs: dict[str, int] = {}
		self._bar_refs: dict[str, int] = {}
		self._forming: dict[str, _Forming] = {}
		self._day_volume: dict[str, float] = {}
		self._quotes: dict[str, Quote] = {}       # newest quote not sent yet
		self._last: dict[str, Quote] = {}         # the stream's newest price per subscribed symbol (latest_quote)
		self._ws: aiohttp.ClientWebSocketResponse | None = None
		self._stream: asyncio.Task | None = None
		self._flusher: asyncio.Task | None = None
		self._tasks: set[asyncio.Task] = set()
		self._stopping = False

	@property
	def symbols(self) -> int:
		return len(self._quote_refs.keys() | self._bar_refs.keys())

	async def start(self) -> None:
		self._flusher = asyncio.get_running_loop().create_task(self._flush_loop())

	async def stop(self) -> None:
		self._stopping = True
		for task in (self._flusher, self._stream, *self._tasks):
			if task is not None:
				task.cancel()
		if self._ws is not None:
			await self._ws.close()
		if self._session is not None:
			await self._session.close()
		if self.cache is not None:
			self.cache.close()

	async def check(self) -> None:
		"""Nothing to check: there are no keys."""

	def _http(self) -> aiohttp.ClientSession:
		if self._session is None:
			self._session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20))
		return self._session

	# -- history ----------------------------------------------------------------------

	async def _chart(self, symbol: str, interval: str, start: datetime, end: datetime) -> dict:
		"""The chart endpoint's ``result`` for a symbol and a time range."""
		params = {"interval": interval, "period1": str(int(start.timestamp())), "period2": str(int(end.timestamp()) + 1),
			"includePrePost": "true", "events": "div,splits"}
		async with self._requests:
			async with self._http().get(CHART_URL + yahoo_symbol(symbol), params=params) as response:
				body = await response.read()
				status = response.status
		try:
			chart = msgspec.json.decode(body)["chart"]
		except (msgspec.DecodeError, KeyError, TypeError):
			if status == 429:
				raise YahooError("Yahoo Finance refuses requests for now (too many)") from None
			raise YahooError(f"Yahoo Finance answered HTTP {status}") from None
		if chart.get("error"):
			raise YahooError(f"Yahoo Finance: {chart['error'].get('description') or chart['error'].get('code')}")
		result = (chart.get("result") or [{}])[0]
		meta = result.get("meta") or {}
		name = meta.get("longName") or meta.get("shortName")
		if name:
			self._names[symbol] = name
		return result

	async def _fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int) -> list[BarData]:
		"""The newest ``limit`` bars with start <= ts <= end, newest first: as many requests as the range needs
		(1m: 7 days each), within what Yahoo still has (older: no bars)."""
		interval, step, span, reach = INTERVALS[timeframe]
		now = self.clock()
		lo = max(start, now - timedelta(days=reach)) if reach else start
		hi = min(end, now)
		# a daily bar's row is at the open, hours after the bar's start (New York midnight): asked for up to a day on
		extra = DAY if step >= DAY else timedelta(0)
		out: list[BarData] = []
		while hi >= lo and len(out) < limit:
			chunk = max(lo, hi - timedelta(days=span)) if span else lo
			ask_lo = chunk.replace(minute=0, second=0, microsecond=0) if timeframe == "1h" else chunk
			bars = bars_of(await self._chart(symbol, interval, ask_lo, hi + extra), interval_timeframe(timeframe))
			if timeframe == "1h":
				bars = hourly(bars)
			out += [b for b in reversed(bars) if chunk <= b.ts <= hi]
			if not span:
				break
			hi = chunk - timedelta(seconds=1)
		return out[:limit]

	async def get_bars(self, req: GetBars) -> Bars:
		if req.timeframe not in INTERVALS:
			return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=[],
				text=f"unknown timeframe {req.timeframe!r}; one of {', '.join(INTERVALS)}")
		limit = max(1, min(req.limit, MAX_BARS))
		end = req.end or self.clock()
		start = req.start or end - lookback(req.timeframe, limit)
		step = INTERVALS[req.timeframe][1]
		fetch = lambda lo, hi, n: self._fetch(req.symbol, req.timeframe, lo, hi, n)
		try:
			if self.cache is None or not self.cache.enabled:
				rows = await fetch(start, end, limit)
			else:
				rows = await self.cache.get(self.source, req.symbol, req.timeframe, step, start, end, limit, fetch,
					aligned=step < DAY)
		except Exception as exc:
			self.log.warning("bars %s %s failed: %s", req.symbol, req.timeframe, exc)
			return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=[], text=str(exc))
		rows.reverse()
		return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=rows,
			name=await self.name(req.symbol))

	async def name(self, symbol: str) -> str:
		"""The instrument's name (every chart answer has it); "" when Yahoo is not reached."""
		if symbol not in self._names:
			try:
				await self._chart(symbol, "1d", self.clock() - 5 * DAY, self.clock())
			except Exception as exc:
				self.log.info("name %s: %s", symbol, exc)
		return self._names.get(symbol, "")

	async def latest_quote(self, symbol: str) -> Quote | None:
		"""The last price as bid and ask: the stream's when it has sent one for the symbol, else the newest 1m
		bar's close (extended hours too), else the regular session's last price."""
		if symbol in self._last:
			return self._last[symbol]
		result = await self._chart(symbol, "1m", self.clock() - DAY, self.clock())
		bars = bars_of(result, "1m")
		meta = result.get("meta") or {}
		if bars:
			price, ts = bars[-1].close, bars[-1].ts
		elif meta.get("regularMarketPrice"):
			price = float(meta["regularMarketPrice"])
			ts = datetime.fromtimestamp(meta.get("regularMarketTime") or self.clock().timestamp(), timezone.utc)
		else:
			return None
		return Quote(symbol=symbol, ts=ts, bid=price, bid_size=0.0, ask=price, ask_size=0.0)

	# -- subscriptions (reference counted over all clients and the simulated account) ------

	def add(self, symbols: list[str], *, quotes: bool, bars: bool) -> None:
		new = []
		for symbol in symbols:
			before = symbol in self._quote_refs or symbol in self._bar_refs
			if quotes:
				self._quote_refs[symbol] = self._quote_refs.get(symbol, 0) + 1
			if bars:
				self._bar_refs[symbol] = self._bar_refs.get(symbol, 0) + 1
			if not before and (quotes or bars):
				new.append(symbol)
		if new:
			self._send({"subscribe": [yahoo_symbol(s) for s in new]})
		if self._stream is None and self.symbols:
			self._stream = asyncio.get_running_loop().create_task(self._stream_loop())

	def remove(self, symbols: list[str], *, quotes: bool, bars: bool) -> None:
		gone = []
		for symbol in symbols:
			for wanted, refs in ((quotes, self._quote_refs), (bars, self._bar_refs)):
				if wanted and symbol in refs:
					refs[symbol] -= 1
					if refs[symbol] <= 0:
						del refs[symbol]
			if symbol not in self._bar_refs:
				self._forming.pop(symbol, None)
			if symbol not in self._quote_refs:
				self._quotes.pop(symbol, None)
			if symbol not in self._quote_refs and symbol not in self._bar_refs:
				self._day_volume.pop(symbol, None)
				self._last.pop(symbol, None)
				gone.append(symbol)
		if gone:
			self._send({"unsubscribe": [yahoo_symbol(s) for s in gone]})

	def _send(self, message: dict) -> None:
		ws = self._ws
		if ws is not None and not ws.closed:
			task = asyncio.get_running_loop().create_task(ws.send_str(msgspec.json.encode(message).decode()))
			self._tasks.add(task)
			task.add_done_callback(self._tasks.discard)

	# -- the stream ---------------------------------------------------------------------

	async def _stream_loop(self) -> None:
		wait = 1.0
		while not self._stopping:
			try:
				async with self._http().ws_connect(STREAM_URL, headers={"Origin": "https://finance.yahoo.com"},
						heartbeat=20.0) as ws:
					self._ws = ws
					symbols = sorted(self._quote_refs.keys() | self._bar_refs.keys())
					if symbols:
						await ws.send_str(msgspec.json.encode({"subscribe": [yahoo_symbol(s) for s in symbols]}).decode())
					self._set_connected(True)
					wait = 1.0
					async for msg in ws:
						if msg.type == aiohttp.WSMsgType.TEXT:
							try:
								tick = tick_of(msg.data)
							except Exception as exc:
								self.log.debug("stream message not understood: %s", exc)
								continue
							if tick is not None:
								self.on_tick(tick)
			except asyncio.CancelledError:
				raise
			except Exception as exc:
				self.log.warning("yahoo price stream: %s", exc)
			self._ws = None
			self._set_connected(False)
			if self._stopping:
				break
			await asyncio.sleep(wait)
			wait = min(wait * 2, 30.0)

	def _set_connected(self, connected: bool) -> None:
		if connected != self.connected:
			self.connected = connected
			self.log.info("yahoo price stream %s", "connected" if connected else "disconnected")
			self.on_state()

	def on_tick(self, tick: Tick) -> None:
		symbol = tick.symbol.replace("-", ".") if tick.symbol.replace("-", ".") in (self._quote_refs.keys()
			| self._bar_refs.keys()) else tick.symbol
		if not tick.price > 0:
			return
		both = tick.bid > 0 and tick.ask > 0
		quote = Quote(symbol=symbol, ts=tick.ts, bid=tick.bid if both else tick.price, bid_size=0.0,
			ask=tick.ask if both else tick.price, ask_size=0.0)
		if symbol in self._quote_refs or symbol in self._bar_refs:
			self._last[symbol] = quote
		if symbol in self._quote_refs:
			self._quotes[symbol] = quote
		if symbol not in self._bar_refs:
			return
		volume = 0.0
		if tick.day_volume is not None:
			before = self._day_volume.get(symbol)
			self._day_volume[symbol] = tick.day_volume
			if before is not None and tick.day_volume > before:
				volume = tick.day_volume - before
		if tick.hours == OVERNIGHT:
			return                               # the history has no bars there
		minute = tick.ts.replace(second=0, microsecond=0)
		f = self._forming.get(symbol)
		if f is None or minute > f.ts:
			if f is not None:
				self._finish(symbol, f)
			self._forming[symbol] = _Forming(minute, tick.price)
			self._forming[symbol].volume = volume
		elif minute == f.ts:
			f.add(tick.price, volume)

	def _finish(self, symbol: str, f: _Forming) -> None:
		"""A forming minute is over: its bar from the chart endpoint, a few seconds later."""
		task = asyncio.get_running_loop().create_task(self._complete(symbol, f))
		self._tasks.add(task)
		task.add_done_callback(self._tasks.discard)

	async def _complete(self, symbol: str, f: _Forming) -> None:
		await asyncio.sleep(self.complete_delay)
		bars: list[BarData] = []
		try:
			result = await self._chart(symbol, "1m", f.ts - 2 * MINUTE, f.ts + MINUTE - timedelta(seconds=1))
			bars = [b for b in bars_of(result, "1m") if f.ts - 2 * MINUTE <= b.ts <= f.ts]
		except Exception as exc:
			self.log.info("bar %s %s: %s", symbol, f.ts.strftime("%H:%M"), exc)
		if symbol not in self._bar_refs:
			return
		if not any(b.ts == f.ts for b in bars):      # Yahoo has no bar of that minute (yet): the one from the prices
			self.publish("bar", symbol, f.bar(symbol, complete=True))
		for b in bars:                               # its minute, and the two before it again (corrections)
			self.publish("bar", symbol, Bar(symbol=symbol, ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close,
				volume=b.volume, complete=True))

	async def _flush_loop(self) -> None:
		"""Quotes every ``quote_interval`` s (the newest per symbol), changed forming bars every ``bar_interval``
		s; a forming bar whose minute is over without a newer price is finished here."""
		loop = asyncio.get_running_loop()
		next_quotes = next_bars = loop.time()
		while True:
			await asyncio.sleep(max(0.005, min(next_quotes, next_bars) - loop.time()))
			now = loop.time()
			if now >= next_quotes:
				next_quotes = now + self.quote_interval
				quotes, self._quotes = self._quotes, {}
				for symbol, q in quotes.items():
					self.publish("quote", symbol, q)
			if now >= next_bars:
				next_bars = now + self.bar_interval
				minute = self.clock().replace(second=0, microsecond=0)
				for symbol, f in list(self._forming.items()):
					if f.dirty:
						f.dirty = False
						self.publish("bar", symbol, f.bar(symbol))
					if f.ts < minute:
						del self._forming[symbol]
						self._finish(symbol, f)


def interval_timeframe(timeframe: str) -> str:
	"""The timeframe whose bars the chart endpoint is asked for (1h: 30m bars)."""
	return "30m" if timeframe == "1h" else timeframe
