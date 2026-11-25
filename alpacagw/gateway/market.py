"""Market data for the clients: historical bars, live 1m bars and quotes.

Live bars: the forming minute is built from trades and sent at most every ``bar_interval``
seconds while it changes; Alpaca's own minute bar (shortly after the minute) is sent as the
complete bar and replaces it, its corrections (updated bars) are sent again as complete bars.
A symbol's first bars subscription also fetches the running minute's earlier trades (``_fill``): before, a chart
opened mid-minute showed that minute from its first trade after the subscription (a gap from the previous close,
too short a range) until Alpaca's bar replaced it.
Quotes are sent at most every ``quote_interval`` seconds per symbol (the newest one).
History comes through the bar cache when there is one (``barcache.py``): Alpaca is asked only for what is missing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..client import Bar as AlpacaBar, BarsParams, Quote as AlpacaQuote, Trade, TradeData, TradesParams, UpdatedBar
from ..protocol import Bar, BarData, Bars, GetBars, Quote
from .barcache import BarCache

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
WIDE = 0.01                      # latest_quote: a spread above 1 % of the price is no market


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


FILL_DELAY = 1.0                 # s after the subscription before the minute's earlier trades are fetched: the stream
                                 # runs by then, so the fetched ones and the streamed ones overlap instead of leaving a gap
FILL_PAGES = 20                  # pages of 10,000 trades at most (NVDA: about 60,000 a minute in the morning, 3 s)


def priced(t: TradeData) -> bool:
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

	def add(self, t: TradeData) -> None:
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
			bar_interval: float = 0.25, quote_interval: float = 0.1, fill_delay: float = FILL_DELAY,
			clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), cache: BarCache | None = None) -> None:
		self.alpaca = alpaca
		self.cache = cache                        # historical bars kept on disk; None: every request goes to Alpaca
		# the data source's name: the cache's file. Each feed has its own (their bars differ)
		self.source = f"alpaca-{getattr(alpaca, 'stock_feed', '') or 'iex'}"
		self.publish = publish                    # (kind "bar" / "quote", symbol, message)
		self.log = log or logging.getLogger("alpacagw.market")
		self.bar_interval = bar_interval
		self.quote_interval = quote_interval
		self._quote_refs: dict[str, int] = {}
		self._bar_refs: dict[str, int] = {}
		self._forming: dict[str, _Forming] = {}
		self._complete: dict[str, datetime] = {}      # per symbol: the newest minute Alpaca sent its bar for
		self.fill_delay = fill_delay
		self.clock = clock
		self._filling: dict[str, list[Trade]] = {}    # symbol -> its streamed trades while the minute's earlier ones load
		self._fills: set[asyncio.Task] = set()
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
		for task in list(self._fills):
			task.cancel()
		if self._started:
			await self.alpaca.stocks.stop()
		if self.cache is not None:
			self.cache.close()

	# -- historical ---------------------------------------------------------------

	async def get_bars(self, req: GetBars) -> Bars:
		if req.timeframe not in TIMEFRAMES:
			return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=[],
				text=f"unknown timeframe {req.timeframe!r}; one of {', '.join(TIMEFRAMES)}")
		limit = max(1, min(req.limit, MAX_BARS))
		end = req.end or datetime.now(timezone.utc)
		start = req.start or end - lookback(req.timeframe, limit)
		fetch = lambda lo, hi, n: self._fetch(req.symbol, req.timeframe, lo, hi, n)
		try:
			if self.cache is None:
				rows = await fetch(start, end, limit)
			else:
				step = TIMEFRAMES[req.timeframe][1]  # Alpaca's intraday bars start at multiples of it (UTC)
				rows = await self.cache.get(self.source, req.symbol, req.timeframe, step, start, end, limit, fetch,
					aligned=step < timedelta(days=1))
		except Exception as exc:
			self.log.warning("bars %s %s failed: %s", req.symbol, req.timeframe, exc)
			return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=[], text=str(exc))
		rows.reverse()
		return Bars(request_id=req.request_id, symbol=req.symbol, timeframe=req.timeframe, bars=rows,
			name=await self.name(req.symbol))

	async def _fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int) -> list[BarData]:
		"""From Alpaca: the newest ``limit`` bars with start <= ts <= end, newest first."""
		params = BarsParams(symbols=[symbol], timeframe=TIMEFRAMES[timeframe][0], start=start, end=end, sort="desc",
			limit=min(limit, 10_000))
		rows: list[BarData] = []
		async for page in self.alpaca.data.iter_bars(params):
			for b in (page.data or {}).get(symbol, ()):
				rows.append(BarData(b.timestamp, b.open, b.high, b.low, b.close, b.volume))
			if len(rows) >= limit:
				break
		return rows[:limit]

	async def name(self, symbol: str) -> str:
		"""The asset's name from Alpaca (once per symbol); "" when Alpaca has none or is not reached (asked again)."""
		if symbol not in self._names:
			try:
				self._names[symbol] = (await self.alpaca.trading.get_asset(symbol)).name or ""
			except Exception as exc:
				self.log.info("asset %s: %s", symbol, exc)
				return ""
		return self._names[symbol]

	async def latest_quote(self, symbol: str) -> Quote | None:
		"""The newest quote, asked from Alpaca (the stream sends one only when it changes); None: there is none."""
		q = (await self.alpaca.data.get_latest_quotes(symbol)).get(symbol)
		if q is None:
			return None
		if not (q.bid_price > 0 and q.ask_price > 0) or q.ask_price - q.bid_price > WIDE * q.ask_price:
			# no real market in it (a closed market's last quote: SPY 693.63 / 736.61 on IEX, 2026-11-25 at night):
			# the last trade's price as bid and ask
			t = (await self.alpaca.data.get_latest_trades(symbol)).get(symbol)
			if t is not None:
				return Quote(symbol=symbol, ts=t.timestamp, bid=t.price, bid_size=0.0, ask=t.price, ask_size=0.0)
		return Quote(symbol=symbol, ts=q.timestamp, bid=q.bid_price, bid_size=q.bid_size, ask=q.ask_price,
			ask_size=q.ask_size)

	async def check(self) -> None:
		"""The keys work (raises otherwise): asked when no account at Alpaca is used, which would show it."""
		await self.alpaca.trading.get_account()

	# -- subscriptions (reference counted over all clients and the simulated account) --------------------------

	def add(self, symbols: list[str], *, quotes: bool, bars: bool) -> None:
		stocks = self.alpaca.stocks
		for symbol in symbols:
			if quotes and self._ref(self._quote_refs, symbol, 1) == 1:
				stocks.subscribe("quotes", symbol)
			if bars and self._ref(self._bar_refs, symbol, 1) == 1:
				stocks.subscribe("trades", symbol)
				stocks.subscribe("bars", symbol)
				stocks.subscribe("updatedBars", symbol)
				streamed: list[Trade] = []
				self._filling[symbol] = streamed
				task = asyncio.get_running_loop().create_task(self._fill(symbol, streamed))
				self._fills.add(task)
				task.add_done_callback(self._fills.discard)
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
				self._filling.pop(symbol, None)

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
		streamed = self._filling.get(t.symbol)
		if streamed is not None:
			streamed.append(t)

	async def _fill(self, symbol: str, streamed: list[Trade]) -> None:
		"""The running minute's trades before the subscription, from Alpaca's trades API: the forming bar is built
		again from them and the trades streamed meanwhile (each once: the two overlap by ``fill_delay``), so a chart
		opened mid-minute gets that minute's open, high and low. Nothing when the minute is over by then (Alpaca's
		bar has it) or the symbol is no longer subscribed."""
		try:
			await asyncio.sleep(self.fill_delay)
			now = self.clock()
			minute = _minute(now)
			try:
				fetched = await self.alpaca.data.get_trades(TradesParams(symbols=[symbol], start=minute, end=now,
					limit=10_000, sort="asc"), max_pages=FILL_PAGES)
			except Exception as exc:
				self.log.info("trades %s since %s: %s", symbol, minute.strftime("%H:%M"), exc)
				return
			if symbol not in self._bar_refs or self._filling.get(symbol) is not streamed:
				return
			done = self._complete.get(symbol)
			f = self._forming.get(symbol)
			if (done is not None and done >= minute) or (f is not None and f.ts != minute):
				return                       # the minute is over: Alpaca's bar (or the next minute's trades) took over
			seen: set[tuple] = set()
			bar = _Forming(minute)
			# sorted keeps the order of equal times: the fetched ones (in Alpaca's order) before the streamed ones
			for t in sorted([*fetched.get(symbol, ()), *streamed], key=lambda t: t.timestamp):
				key = (t.timestamp, t.id, t.exchange, t.price, t.size)
				if _minute(t.timestamp) != minute or key in seen:
					continue
				seen.add(key)
				bar.add(t)
			if bar.volume:
				self._forming[symbol] = bar
		finally:
			if self._filling.get(symbol) is streamed:
				del self._filling[symbol]

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
