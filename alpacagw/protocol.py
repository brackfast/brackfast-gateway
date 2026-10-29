"""The gateway protocol: msgpack messages over a websocket. See PROTOCOL.md.

Every message is a msgpack map with a ``msg`` key naming its kind. Times are msgpack
timestamps (extension -1), always UTC. Prices and quantities are floats. The protocol does
not depend on the broker: the same messages work for any broker a gateway connects to.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import msgspec

PROTOCOL_VERSION = 2                    # 2: SubmitBracket's stop_price (stop / stop limit entries)
PAPER_PORT = 4102                       # default ports (IB Gateway: 4002 paper, 4001 live)
LIVE_PORT = 4101


class Message(msgspec.Struct, kw_only=True, tag_field="msg", omit_defaults=True):
	"""Base of all protocol messages."""


# == client -> gateway ==============================================================

class Hello(Message, tag="hello"):
	"""First message of every connection. The gateway closes the connection if it does not
	come within 5 s or if ``protocol`` is not supported."""
	protocol: int
	client: str                         # a name for logs and the gateway's client list


class SubmitBracket(Message, tag="submit_bracket"):
	"""Entry with a take profit and a stop loss. The legs get the ids ``<id>.tp`` and ``<id>.sl``.

	The entry: market (neither price), limit (``limit_price``), stop (``stop_price``) or stop limit (both)."""
	id: str                             # chosen by the client, unique, at most 48 characters
	symbol: str
	side: str                           # "buy" | "sell" (of the entry)
	qty: float                          # whole shares
	take_profit: float                  # limit price of the take profit leg
	stop_loss: float                    # stop price of the stop loss leg
	limit_price: float | None = None    # the entry's limit price
	stop_price: float | None = None     # the entry's stop price (a breakout entry)
	stop_loss_limit: float | None = None    # stop limit instead of stop for the stop loss leg
	tif: str = "gtc"                    # "day" | "gtc"


class SubmitOrder(Message, tag="submit_order"):
	"""A single order."""
	id: str
	symbol: str
	side: str
	qty: float
	type: str                           # "market" | "limit" | "stop" | "stop_limit"
	limit_price: float | None = None
	stop_price: float | None = None
	tif: str = "day"
	extended_hours: bool = False        # limit orders only


class ReplaceOrder(Message, tag="replace_order"):
	"""New prices / qty of a working order; the order keeps its id. None: unchanged."""
	id: str
	limit_price: float | None = None
	stop_price: float | None = None
	qty: float | None = None


class CancelOrder(Message, tag="cancel_order"):
	"""Cancelling an entry cancels its legs."""
	id: str


class CancelAll(Message, tag="cancel_all"):
	symbol: str = ""                    # "": every working order of the account


class ClosePosition(Message, tag="close_position"):
	"""Cancel the symbol's working orders, then close its position at market."""
	symbol: str


class FlattenAll(Message, tag="flatten_all"):
	"""Cancel all orders and close all positions."""


class GetBars(Message, tag="get_bars"):
	"""Historical bars, answered with ``Bars`` (same ``request_id``)."""
	request_id: int
	symbol: str
	timeframe: str                      # "1m" "5m" "15m" "30m" "1h" "1D" "1W" "1M"
	start: datetime | None = None
	end: datetime | None = None         # None: now
	limit: int = 1000                   # the newest ``limit`` bars up to ``end``


class Subscribe(Message, tag="subscribe"):
	"""Live data for symbols: ``Quote`` and / or ``Bar`` (1m, forming and complete)."""
	symbols: list[str]
	quotes: bool = True
	bars: bool = True


class Unsubscribe(Message, tag="unsubscribe"):
	symbols: list[str]


# == gateway -> client ==============================================================

class Welcome(Message, tag="welcome"):
	"""Answer to Hello. Then the current state follows: Status, Account, every Position,
	every working Order (and the orders that finished today)."""
	protocol: int
	gateway: str                        # name and version, e.g. "alpacagw 0.1.0"
	broker: str                         # "alpaca"
	environment: str                    # "paper" | "live"
	account: str                        # account number


class Status(Message, tag="status"):
	state: str                          # "starting" | "ready" | "error" | "stopping"
	trading_connected: bool = False     # order updates from the broker
	market_data_connected: bool = False
	clients: list[str] = msgspec.field(default_factory=list)
	text: str = ""


class Account(Message, tag="account"):
	account: str
	currency: str = "USD"
	equity: float = 0.0
	last_equity: float = 0.0            # equity at the previous close
	cash: float = 0.0
	buying_power: float = 0.0
	daytrade_count: int = 0
	pattern_day_trader: bool = False
	ts: datetime | None = None


class Position(Message, tag="position"):
	"""qty > 0 long, < 0 short, 0: the position is closed (sent once)."""
	symbol: str
	qty: float
	avg_price: float = 0.0
	current_price: float = 0.0
	market_value: float = 0.0
	unrealized_pl: float = 0.0


class Order(Message, tag="order"):
	"""The state of an order after every change.

	Status: "pending" (not acknowledged yet) | "open" | "held" (a bracket leg waiting for its
	entry) | "partially_filled" | "filled" | "canceled" | "expired" | "rejected". The last four
	are final. Role: "simple" | "entry" | "take_profit" | "stop_loss". Orders placed elsewhere
	(the broker's web site, other apps) are reported too, with origin "external".
	"""
	id: str
	symbol: str
	side: str
	type: str
	qty: float
	status: str
	role: str = "simple"
	parent_id: str = ""                 # legs: the entry's id
	leg_ids: list[str] = msgspec.field(default_factory=list)
	limit_price: float | None = None
	stop_price: float | None = None
	tif: str = "day"
	filled_qty: float = 0.0
	avg_fill_price: float | None = None
	origin: str = ""                    # client name, or "external"
	reason: str = ""                    # why rejected / why a replace failed
	broker_id: str = ""
	created_at: datetime | None = None
	updated_at: datetime | None = None


FINAL = frozenset({"filled", "canceled", "expired", "rejected"})


class Fill(Message, tag="fill"):
	order_id: str
	symbol: str
	side: str
	qty: float                          # this fill
	price: float
	ts: datetime
	role: str = "simple"


class Reply(Message, tag="reply"):
	"""Result of a command: ``id`` is the order id (or symbol) it was about."""
	command: str                        # the command's msg, e.g. "submit_bracket"
	id: str
	ok: bool
	text: str = ""


class BarData(msgspec.Struct, array_like=True, gc=False):
	"""One bar as an array [ts, open, high, low, close, volume]; ts = start of the bar."""
	ts: datetime
	open: float
	high: float
	low: float
	close: float
	volume: float


class Bars(Message, tag="bars"):
	"""Answer to GetBars, oldest first."""
	request_id: int
	symbol: str
	timeframe: str
	bars: list[BarData]
	text: str = ""                      # error text when the request failed (bars empty)


class Bar(Message, tag="bar"):
	"""Live 1m bar: forming (complete False, sent again as it changes) or complete."""
	symbol: str
	ts: datetime
	open: float
	high: float
	low: float
	close: float
	volume: float
	complete: bool = False


class Quote(Message, tag="quote"):
	symbol: str
	ts: datetime
	bid: float
	bid_size: float
	ask: float
	ask_size: float


class Event(Message, tag="event"):
	"""Log line for the client's user: commands, broker events, errors."""
	ts: datetime
	level: str                          # "info" | "warning" | "error"
	text: str
	client: str = ""


ClientMessage = (
	Hello | SubmitBracket | SubmitOrder | ReplaceOrder | CancelOrder | CancelAll | ClosePosition
	| FlattenAll | GetBars | Subscribe | Unsubscribe
)
GatewayMessage = Welcome | Status | Account | Position | Order | Fill | Reply | Bars | Bar | Quote | Event

_encoder = msgspec.msgpack.Encoder()
_client_decoder = msgspec.msgpack.Decoder(ClientMessage)
_gateway_decoder = msgspec.msgpack.Decoder(GatewayMessage)


def encode(msg: Message) -> bytes:
	return _encoder.encode(msg)


def decode_client(data: bytes) -> Any:
	return _client_decoder.decode(data)


def decode_gateway(data: bytes) -> Any:
	return _gateway_decoder.decode(data)
