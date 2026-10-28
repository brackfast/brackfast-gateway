"""Market Data API v2 (stocks): bars, trades, quotes, snapshots.

Alpaca's wire format uses one-letter keys; the structs map them to readable names. Prices and
sizes are floats (market data volume makes Decimal too slow). The stream messages in
``stream.py`` subclass these structs.
"""
from __future__ import annotations

from datetime import datetime

import msgspec

from .base import Model, Request


class TradeData(Model, kw_only=True):
	timestamp: datetime = msgspec.field(name="t")
	price: float = msgspec.field(name="p")
	size: float = msgspec.field(name="s")
	id: int | None = msgspec.field(default=None, name="i")
	exchange: str | None = msgspec.field(default=None, name="x")
	conditions: list[str] = msgspec.field(default_factory=list, name="c")
	tape: str | None = msgspec.field(default=None, name="z")


class QuoteData(Model, kw_only=True):
	timestamp: datetime = msgspec.field(name="t")
	bid_price: float = msgspec.field(name="bp")
	bid_size: float = msgspec.field(name="bs")
	ask_price: float = msgspec.field(name="ap")
	ask_size: float = msgspec.field(name="as")
	bid_exchange: str | None = msgspec.field(default=None, name="bx")
	ask_exchange: str | None = msgspec.field(default=None, name="ax")
	conditions: list[str] = msgspec.field(default_factory=list, name="c")
	tape: str | None = msgspec.field(default=None, name="z")

	@property
	def mid(self) -> float:
		return (self.bid_price + self.ask_price) * 0.5

	@property
	def spread(self) -> float:
		return self.ask_price - self.bid_price


class BarData(Model, kw_only=True):
	timestamp: datetime = msgspec.field(name="t")         # start of the bar
	open: float = msgspec.field(name="o")
	high: float = msgspec.field(name="h")
	low: float = msgspec.field(name="l")
	close: float = msgspec.field(name="c")
	volume: float = msgspec.field(name="v")
	trade_count: int | None = msgspec.field(default=None, name="n")
	vwap: float | None = msgspec.field(default=None, name="vw")


class Snapshot(Model, kw_only=True, rename="camel"):
	latest_trade: TradeData | None = None
	latest_quote: QuoteData | None = None
	minute_bar: BarData | None = None
	daily_bar: BarData | None = None
	prev_daily_bar: BarData | None = None


# -- responses with pages / keyed by symbol --------------------------------------

class BarsPage(Model, kw_only=True):
	data: dict[str, list[BarData]] | None = msgspec.field(default=None, name="bars")
	next_page_token: str | None = None


class TradesPage(Model, kw_only=True):
	data: dict[str, list[TradeData]] | None = msgspec.field(default=None, name="trades")
	next_page_token: str | None = None


class QuotesPage(Model, kw_only=True):
	data: dict[str, list[QuoteData]] | None = msgspec.field(default=None, name="quotes")
	next_page_token: str | None = None


class LatestTrades(Model, kw_only=True):
	trades: dict[str, TradeData] = msgspec.field(default_factory=dict)


class LatestQuotes(Model, kw_only=True):
	quotes: dict[str, QuoteData] = msgspec.field(default_factory=dict)


class LatestBars(Model, kw_only=True):
	bars: dict[str, BarData] = msgspec.field(default_factory=dict)


# -- request parameters ----------------------------------------------------------

class _HistoricalParams(Request, kw_only=True, omit_defaults=False):
	# omit_defaults=False: the feed / adjustment defaults are sent, since Alpaca's own defaults
	# differ. None is dropped from the query, so feed=None falls back to Alpaca's default.
	symbols: list[str]
	start: datetime | None = None         # timezone-aware
	end: datetime | None = None
	limit: int | None = None              # page size, max 10000
	sort: str | None = None               # "asc" | "desc"
	feed: str | None = None               # "sip" | "iex" | "delayed_sip"; None: the client's feed
	asof: str | None = None
	currency: str | None = None


class BarsParams(_HistoricalParams, kw_only=True, omit_defaults=False):
	timeframe: str                        # "1Min", "5Min", "15Min", "1Hour", "1Day", "1Week", "1Month"
	adjustment: str | None = "split"      # "raw" | "split" | "dividend" | "all"


class TradesParams(_HistoricalParams, kw_only=True, omit_defaults=False):
	pass


class QuotesParams(_HistoricalParams, kw_only=True, omit_defaults=False):
	pass
