"""Websocket messages.

The stock data stream sends arrays of objects tagged by ``"T"``: a msgspec tagged union
(``StockMessage``) decodes a whole frame into typed objects in one call. The trading stream
tags its messages by ``"stream"``.

``StreamConnected`` / ``StreamReady`` / ``StreamDisconnected`` never come over the wire: the
streams dispatch them to handlers like any other message.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import msgspec

from .base import Model
from .market import BarData, QuoteData, TradeData
from .trading import Order


# -- connection lifecycle (local) ------------------------------------------------

class StreamConnected(Model, kw_only=True):
	url: str


class StreamReady(Model, kw_only=True):
	"""Authenticated (trading stream: and listening to trade_updates)."""
	url: str


class StreamDisconnected(Model, kw_only=True):
	url: str


# -- stock data ------------------------------------------------------------------

class Trade(TradeData, tag="t", tag_field="T", kw_only=True):
	symbol: str = msgspec.field(name="S")


class Quote(QuoteData, tag="q", tag_field="T", kw_only=True):
	symbol: str = msgspec.field(name="S")


class Bar(BarData, tag="b", tag_field="T", kw_only=True):
	"""Minute bar, sent just after the minute."""
	symbol: str = msgspec.field(name="S")


class DailyBar(BarData, tag="d", tag_field="T", kw_only=True):
	symbol: str = msgspec.field(name="S")


class UpdatedBar(BarData, tag="u", tag_field="T", kw_only=True):
	"""Correction of a minute bar already sent (late trades)."""
	symbol: str = msgspec.field(name="S")


class TradingStatus(Model, tag="s", tag_field="T", kw_only=True):
	symbol: str = msgspec.field(name="S")
	status_code: str = msgspec.field(name="sc")
	status_message: str = msgspec.field(default="", name="sm")
	reason_code: str = msgspec.field(default="", name="rc")
	reason_message: str = msgspec.field(default="", name="rm")
	timestamp: datetime = msgspec.field(name="t")
	tape: str | None = msgspec.field(default=None, name="z")


class LULD(Model, tag="l", tag_field="T", kw_only=True):
	symbol: str = msgspec.field(name="S")
	limit_up: float = msgspec.field(name="u")
	limit_down: float = msgspec.field(name="d")
	indicator: str = msgspec.field(default="", name="i")
	timestamp: datetime = msgspec.field(name="t")
	tape: str | None = msgspec.field(default=None, name="z")


class Success(Model, tag="success", tag_field="T", kw_only=True):
	msg: str


class Error(Model, tag="error", tag_field="T", kw_only=True):
	code: int
	msg: str


class Subscription(Model, tag="subscription", tag_field="T", kw_only=True, rename="camel"):
	"""The server's confirmation of all current subscriptions."""
	trades: list[str] = msgspec.field(default_factory=list)
	quotes: list[str] = msgspec.field(default_factory=list)
	bars: list[str] = msgspec.field(default_factory=list)
	daily_bars: list[str] = msgspec.field(default_factory=list)
	updated_bars: list[str] = msgspec.field(default_factory=list)
	statuses: list[str] = msgspec.field(default_factory=list)
	lulds: list[str] = msgspec.field(default_factory=list)


StockMessage = (
	Trade | Quote | Bar | DailyBar | UpdatedBar | TradingStatus | LULD
	| Success | Error | Subscription
)

# message type -> the channel that subscribes it (wire name)
CHANNELS: dict[type, str] = {
	Trade: "trades",
	Quote: "quotes",
	Bar: "bars",
	DailyBar: "dailyBars",
	UpdatedBar: "updatedBars",
	TradingStatus: "statuses",
	LULD: "lulds",
}


# -- trading stream --------------------------------------------------------------

class TradeUpdate(Model, kw_only=True):
	"""An order event from the ``trade_updates`` stream."""
	event: str                            # TradeEvent
	order: Order
	timestamp: datetime | None = None
	execution_id: str | None = None
	price: Decimal | None = None          # fill / partial_fill
	qty: Decimal | None = None            # fill / partial_fill
	position_qty: Decimal | None = None   # fill / partial_fill

	@property
	def symbol(self) -> str:
		return self.order.symbol


class _AuthorizationData(Model, kw_only=True):
	status: str
	action: str | None = None


class _ListeningData(Model, kw_only=True):
	streams: list[str] = msgspec.field(default_factory=list)
	error: str | None = None


class AuthorizationMsg(Model, tag="authorization", tag_field="stream", kw_only=True):
	data: _AuthorizationData


class ListeningMsg(Model, tag="listening", tag_field="stream", kw_only=True):
	data: _ListeningData


class TradeUpdateMsg(Model, tag="trade_updates", tag_field="stream", kw_only=True):
	data: TradeUpdate


TradingMessage = AuthorizationMsg | ListeningMsg | TradeUpdateMsg
