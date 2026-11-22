"""Market data for the clients: historical bars, live 1m bars and quotes.

Live bars: the forming minute is built from trades and sent at most every ``bar_interval``
seconds while it changes; Alpaca's own minute bar (shortly after the minute) is sent as the
complete bar and replaces it, its corrections (updated bars) are sent again as complete bars.
Quotes are sent at most every ``quote_interval`` seconds per symbol (the newest one).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..client import Bar as AlpacaBar, BarsParams, Quote as AlpacaQuote, Trade, UpdatedBar
from ..protocol import Bar, BarData, Bars, GetBars, Quote

TIMEFRAMES = {
	"1m": ("1Min", timedelta(minutes=1)),
	"2m": ("2Min", timedelta(minutes=2)),
	"5m": ("5Min", timedelta(minutes=5)),
	"15m": ("15Min", timedelta(minutes=15)),
	"30m": ("30Min", timedelta(minutes=30)),
	"1h": ("1Hour", timedelta(hours=1)),
	"1D": ("1Day", timedelta(days=1)),
	"1W": ("1Week", timedelta(weeks=1)),
	"1M": ("1Month", timedelta(days=31)),
}

MAX_BARS = 50_000


def lookback(timeframe: str, limit: int) -> timedelta:
	"""How far back ``limit`` bars reach at most: trading hours are a fraction of the day
	(16 of 24 h with extended hours), plus weekends and holidays."""
	step = TIMEFRAMES[timeframe][1]
	if step < timedelta(days=1):
		return max(step * limit * 3, timedelta(days=5))
	return step * limit * 1.6 + timedelta(days=7)


# Trade conditions that do not set a bar's open, high, low or close (the consolidated tape's rules, as Alpaca builds
# its minute bars): odd lot (I), average price (W, B), derivatively priced (4), qualified contingent (7, V), sold
# out of sequence (Z, U), cash (C), next day (N), seller (R), price variation (H), prior reference price (P),
# official open / close (Q, M), corrected close (9). Their volume counts. Checked against Alpaca's 1m bars (NVDA,
# 2026-11-19): without them the trades give the bars' open, high, low and close exactly, all trades their volume;
# with them, odd lots at old prices and derivatively priced trades drew wicks into the forming bar.
UNPRICED = frozenset("IWB47VZUCNRHPQM9")


def priced(t: Trade) -> bool:
	return not UNPRICED.intersection(t.conditions)


class _Forming:
	"""The forming minute from the trades: open / high / low / close from the priced ones (None until the first),
	the volume from all."""
	__slots__ = ("ts", "open", "high", "low", "close", "volume", "dirty")

	def __init__(self, ts: datetime) -> None:
		self.ts = ts
		self.open = self.high = self.low = self.close = None
		self.volume = 0.0
		self.dirty = False

	def add(self, t: Trade) -> None:
		self.volume += t.size
		if priced(t):
			if self.open is None:
				self.open = self.high = self.low = t.price
			else:
				self.high = max(self.high, t.price)
				self.low = min(self.low, t.price)
			self.close = t.price
		self.dirty = self.open is not None           # nothing to send before the first priced trade


def _minute(ts: datetime) -> datetime:
	return ts.replace(second=0, microsecond=0)


class MarketRelay:
	def __init__(self, alpaca: Any, *, publish: Callable[[str, str, Any], None], log: logging.Logger | None = None,
			bar_interval: float = 0.25, quote_interval: float = 0.1) -> None:
		self.alpaca = alpaca
		self.publish = publish                    # (kind "bar" / "quote", symbol, message)
		self.log = log or logging.getLogger("alpacagw.market")
		self.bar_interval = bar_interval
		self.quote_interval = quote_interval
		self._quote_refs: dict[str, int] = {}
		self._bar_refs: dict[str, int] = {}
		self._forming: dict[str, _Forming] = {}
		self._complete: dict[str, datetime] = {}      # per symbol: the newest minute Alpaca sent its bar for
		self._quotes: dict[str, AlpacaQuote] = {}     # newest quote not sent yet
		self._names: dict[str, str] = {}              # symbol -> the asset's name (Bars.name), asked once
		self._started = False
		self._flusher: asyncio.Task | None = None
		stocks = alpaca.stocks
		stocks.on(Trade, self._on_trade)
		stocks.on(AlpacaQuote, self._on_quote)
		stocks.on(AlpacaBar, self._on_bar)
		stocks.on(UpdatedBar, self._on_bar)

	@property
	def connected(self) -> bool:
		return self.alpaca.stocks.is_ready

	@property
	def symbols(self) -> int:
		"""How many symbols the clients have subscribed (quotes or bars)."""
		return len(self._quote_refs.keys() | self._bar_refs.keys())

	async def start(self) -> None:
		self._flusher = asyncio.get_running_loop().create_task(self._flush_loop())

	async def stop(self) -> None:
		if self._flusher is not None:
			self._flusher.cancel()
		if self._started:
			await self.alpaca.stocks.stop()

	# -- historical ---------------------------------------------------------------

	async def get_bars(self, req: GetBars) -> Bars:
		if req.timeframe not in TIMEFRAMES:
			return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=[],
				text=f"unknown timeframe {req.timeframe!r}; one of {', '.join(TIMEFRAMES)}")
		limit = max(1, min(req.limit, MAX_BARS))
		end = req.end or datetime.now(timezone.utc)
		start = req.start or end - lookback(req.timeframe, limit)
		params = BarsParams(symbols=[req.symbol], timeframe=TIMEFRAMES[req.timeframe][0], start=start, end=end,
			sort="desc", limit=min(limit, 10_000))
		rows: list[BarData] = []
		try:
			async for page in self.alpaca.data.iter_bars(params):
				for b in (page.data or {}).get(req.symbol, ()):
					rows.append(BarData(b.timestamp, b.open, b.high, b.low, b.close, b.volume))
				if len(rows) >= limit:
					break
		except Exception as exc:
			self.log.warning("bars %s %s failed: %s", req.symbol, req.timeframe, exc)
			return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=[], text=str(exc))
		rows = rows[:limit]
		rows.reverse()
		return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=rows,
			name=await self.name(req.symbol))

	async def name(self, symbol: str) -> str:
		"""The asset's name from Alpaca (once per symbol); "" when Alpaca has none or is not reached (asked again)."""
		if symbol not in self._names:
			try:
				self._names[symbol] = (await self.alpaca.trading.get_asset(symbol)).name or ""
			except Exception as exc:
				self.log.info("asset %s: %s", symbol, exc)
				return ""
		return self._names[symbol]

	# -- subscriptions (reference counted over all clients) --------------------------

	def add(self, symbols: list[str], *, quotes: bool, bars: bool) -> None:
		stocks = self.alpaca.stocks
		for symbol in symbols:
			if quotes and self._ref(self._quote_refs, symbol, 1) == 1:
				stocks.subscribe("quotes", symbol)
			if bars and self._ref(self._bar_refs, symbol, 1) == 1:
				stocks.subscribe("trades", symbol)
				stocks.subscribe("bars", symbol)
				stocks.subscribe("updatedBars", symbol)
		if not self._started and (self._quote_refs or self._bar_refs):
			self._started = True
			asyncio.get_running_loop().create_task(self._start_stream())

	def remove(self, symbols: list[str], *, quotes: bool, bars: bool) -> None:
		stocks = self.alpaca.stocks
		for symbol in symbols:
			if quotes and self._ref(self._quote_refs, symbol, -1) == 0:
				stocks.unsubscribe("quotes", symbol)
				self._quotes.pop(symbol, None)
			if bars and self._ref(self._bar_refs, symbol, -1) == 0:
				stocks.unsubscribe("trades", symbol)
				stocks.unsubscribe("bars", symbol)
				stocks.unsubscribe("updatedBars", symbol)
				self._forming.pop(symbol, None)
				self._complete.pop(symbol, None)

	@staticmethod
	def _ref(refs: dict[str, int], symbol: str, delta: int) -> int:
		n = refs.get(symbol, 0) + delta
		if n > 0:
			refs[symbol] = n
		else:
			refs.pop(symbol, None)
			n = 0
		return n

	async def _start_stream(self) -> None:
		try:
			await self.alpaca.stocks.start()
		except Exception as exc:
			self.log.error("market data stream: %s", exc)

	# -- stream ---------------------------------------------------------------------

	def _on_trade(self, t: Trade) -> None:
		if t.symbol not in self._bar_refs:
			return
		minute = _minute(t.timestamp)
		done = self._complete.get(t.symbol)
		if done is not None and minute <= done:
			return                       # the official bar is out: it (or its correction) has this trade
		f = self._forming.get(t.symbol)
		if f is None or minute > f.ts:
			f = self._forming[t.symbol] = _Forming(minute)
		elif minute < f.ts:
			return                       # late trade of a finished minute: the official bar has it
		f.add(t)

	def _on_quote(self, q: AlpacaQuote) -> None:
		if q.symbol in self._quote_refs:
			self._quotes[q.symbol] = q

	def _on_bar(self, b: AlpacaBar | UpdatedBar) -> None:
		if b.symbol not in self._bar_refs:
			return
		f = self._forming.get(b.symbol)
		if f is not None and f.ts <= b.timestamp:
			del self._forming[b.symbol]  # the official bar replaces what the trades built
		done = self._complete.get(b.symbol)
		if done is None or b.timestamp > done:
			self._complete[b.symbol] = b.timestamp      # later trades of this minute start no forming bar again
		self.publish("bar", b.symbol, Bar(symbol=b.symbol, ts=b.timestamp, open=b.open, high=b.high, low=b.low,
			close=b.close, volume=b.volume, complete=True))

	async def _flush_loop(self) -> None:
		"""Quotes every ``quote_interval`` s (the newest per symbol), changed forming bars every
		``bar_interval`` s; each on its own clock."""
		loop = asyncio.get_running_loop()
		next_quotes = next_bars = loop.time()
		while True:
			await asyncio.sleep(max(0.005, min(next_quotes, next_bars) - loop.time()))
			now = loop.time()
			send_quotes, send_bars = now >= next_quotes, now >= next_bars
			if send_quotes:
				next_quotes = now + self.quote_interval
			if send_bars:
				next_bars = now + self.bar_interval
			if send_quotes and self._quotes:
				quotes, self._quotes = self._quotes, {}
				for symbol, q in quotes.items():
					self.publish("quote", symbol, Quote(symbol=symbol, ts=q.timestamp, bid=q.bid_price,
						bid_size=q.bid_size, ask=q.ask_price, ask_size=q.ask_size))
			if send_bars:
				for symbol, f in self._forming.items():
					if f.dirty:
						f.dirty = False
						self.publish("bar", symbol, Bar(symbol=symbol, ts=f.ts, open=f.open, high=f.high, low=f.low,
							close=f.close, volume=f.volume))
