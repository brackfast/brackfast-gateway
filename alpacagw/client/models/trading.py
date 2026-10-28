"""Trading API v2: enums, responses and requests.

Money and quantities are Decimal (Alpaca sends strings; msgspec encodes Decimals back to strings).
Enum-like response fields are plain ``str`` so a value Alpaca adds later still decodes; the StrEnums
below compare equal to those strings.
"""
from __future__ import annotations

from datetime import date as Date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import msgspec

from .base import Model, Request, dec


# -- enums -----------------------------------------------------------------------

class OrderSide(StrEnum):
	BUY = "buy"
	SELL = "sell"


class OrderType(StrEnum):
	MARKET = "market"
	LIMIT = "limit"
	STOP = "stop"
	STOP_LIMIT = "stop_limit"
	TRAILING_STOP = "trailing_stop"


class TimeInForce(StrEnum):
	DAY = "day"
	GTC = "gtc"
	OPG = "opg"
	CLS = "cls"
	IOC = "ioc"
	FOK = "fok"


class OrderClass(StrEnum):
	SIMPLE = "simple"
	BRACKET = "bracket"
	OCO = "oco"
	OTO = "oto"


class OrderStatus(StrEnum):
	NEW = "new"
	PARTIALLY_FILLED = "partially_filled"
	FILLED = "filled"
	DONE_FOR_DAY = "done_for_day"
	CANCELED = "canceled"
	EXPIRED = "expired"
	REPLACED = "replaced"
	PENDING_CANCEL = "pending_cancel"
	PENDING_REPLACE = "pending_replace"
	PENDING_NEW = "pending_new"
	ACCEPTED = "accepted"
	ACCEPTED_FOR_BIDDING = "accepted_for_bidding"
	STOPPED = "stopped"
	REJECTED = "rejected"
	SUSPENDED = "suspended"
	CALCULATED = "calculated"
	HELD = "held"


FINAL_STATUSES = frozenset({
	OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
	OrderStatus.REPLACED, OrderStatus.REJECTED, OrderStatus.DONE_FOR_DAY,
})


class QueryOrderStatus(StrEnum):
	OPEN = "open"
	CLOSED = "closed"
	ALL = "all"


class TradeEvent(StrEnum):
	NEW = "new"
	FILL = "fill"
	PARTIAL_FILL = "partial_fill"
	CANCELED = "canceled"
	EXPIRED = "expired"
	DONE_FOR_DAY = "done_for_day"
	REPLACED = "replaced"
	REJECTED = "rejected"
	PENDING_NEW = "pending_new"
	STOPPED = "stopped"
	PENDING_CANCEL = "pending_cancel"
	PENDING_REPLACE = "pending_replace"
	CALCULATED = "calculated"
	SUSPENDED = "suspended"
	ORDER_REPLACE_REJECTED = "order_replace_rejected"
	ORDER_CANCEL_REJECTED = "order_cancel_rejected"


# -- responses -------------------------------------------------------------------

class Account(Model, kw_only=True):
	id: str
	account_number: str
	status: str
	currency: str = "USD"
	cash: Decimal | None = None
	equity: Decimal | None = None
	last_equity: Decimal | None = None
	portfolio_value: Decimal | None = None
	buying_power: Decimal | None = None
	regt_buying_power: Decimal | None = None
	daytrading_buying_power: Decimal | None = None
	non_marginable_buying_power: Decimal | None = None
	long_market_value: Decimal | None = None
	short_market_value: Decimal | None = None
	initial_margin: Decimal | None = None
	maintenance_margin: Decimal | None = None
	multiplier: Decimal | None = None
	daytrade_count: int | None = None
	pattern_day_trader: bool = False
	trading_blocked: bool = False
	transfers_blocked: bool = False
	account_blocked: bool = False
	trade_suspended_by_user: bool = False
	shorting_enabled: bool = False
	created_at: datetime | None = None


class Asset(Model, kw_only=True):
	id: str
	symbol: str
	asset_class: str = msgspec.field(name="class")
	exchange: str | None = None
	name: str | None = None
	status: str | None = None
	tradable: bool = False
	marginable: bool = False
	shortable: bool = False
	easy_to_borrow: bool = False
	fractionable: bool = False
	min_order_size: Decimal | None = None
	min_trade_increment: Decimal | None = None
	price_increment: Decimal | None = None
	attributes: list[str] = msgspec.field(default_factory=list)


class Order(Model, kw_only=True):
	id: str
	client_order_id: str
	symbol: str
	status: str
	side: str
	type: str
	time_in_force: str
	created_at: datetime
	updated_at: datetime | None = None
	submitted_at: datetime | None = None
	filled_at: datetime | None = None
	expired_at: datetime | None = None
	canceled_at: datetime | None = None
	failed_at: datetime | None = None
	replaced_at: datetime | None = None
	replaced_by: str | None = None
	replaces: str | None = None
	asset_id: str | None = None
	asset_class: str | None = None
	order_class: str | None = None
	position_intent: str | None = None
	qty: Decimal | None = None
	notional: Decimal | None = None
	filled_qty: Decimal = Decimal(0)
	filled_avg_price: Decimal | None = None
	limit_price: Decimal | None = None
	stop_price: Decimal | None = None
	trail_price: Decimal | None = None
	trail_percent: Decimal | None = None
	hwm: Decimal | None = None
	extended_hours: bool = False
	legs: list[Order] | None = None

	@property
	def is_open(self) -> bool:
		return self.status not in FINAL_STATUSES


class Position(Model, kw_only=True):
	asset_id: str
	symbol: str
	side: str
	qty: Decimal
	exchange: str | None = None
	asset_class: str | None = None
	avg_entry_price: Decimal | None = None
	qty_available: Decimal | None = None
	market_value: Decimal | None = None
	cost_basis: Decimal | None = None
	unrealized_pl: Decimal | None = None
	unrealized_plpc: Decimal | None = None
	unrealized_intraday_pl: Decimal | None = None
	unrealized_intraday_plpc: Decimal | None = None
	current_price: Decimal | None = None
	lastday_price: Decimal | None = None
	change_today: Decimal | None = None


class Clock(Model, kw_only=True):
	timestamp: datetime
	is_open: bool
	next_open: datetime
	next_close: datetime


class CalendarDay(Model, kw_only=True):
	date: Date
	open: str                             # "09:30", New York time
	close: str                            # "16:00" (earlier on early closes)
	session_open: str | None = None
	session_close: str | None = None
	settlement_date: Date | None = None


class MultiStatusResult(Model, kw_only=True):
	"""One entry of a 207 Multi-Status reply (cancel all orders, close all positions)."""
	status: int
	id: str | None = None                 # order id (cancel all)
	symbol: str | None = None             # symbol (close all)
	body: dict[str, Any] | None = None

	def order(self) -> Order | None:
		"""``body`` as an Order when this entry succeeded."""
		if self.body is None or self.status >= 400:
			return None
		return msgspec.convert(self.body, Order, strict=False)


# -- requests --------------------------------------------------------------------

class TakeProfit(Request, kw_only=True):
	limit_price: Decimal


class StopLoss(Request, kw_only=True):
	stop_price: Decimal
	limit_price: Decimal | None = None


class OrderRequest(Request, kw_only=True):
	"""Body of POST /v2/orders. Fields Alpaca requires have no default, so they are always sent."""
	symbol: str
	side: OrderSide
	type: OrderType
	time_in_force: TimeInForce
	qty: Decimal | None = None
	notional: Decimal | None = None
	limit_price: Decimal | None = None
	stop_price: Decimal | None = None
	trail_price: Decimal | None = None
	trail_percent: Decimal | None = None
	extended_hours: bool = False
	client_order_id: str | None = None
	order_class: OrderClass | None = None
	take_profit: TakeProfit | None = None
	stop_loss: StopLoss | None = None

	@classmethod
	def market(cls, symbol: str, side: OrderSide | str, qty=None, *, notional=None,
			time_in_force: TimeInForce | str = TimeInForce.DAY, **kw) -> OrderRequest:
		if (qty is None) == (notional is None):
			raise ValueError("pass exactly one of qty / notional")
		return cls(symbol=symbol, side=OrderSide(side), type=OrderType.MARKET, qty=dec(qty),
			notional=dec(notional), time_in_force=TimeInForce(time_in_force), **kw)

	@classmethod
	def limit(cls, symbol: str, side: OrderSide | str, qty, limit_price, *,
			time_in_force: TimeInForce | str = TimeInForce.DAY, **kw) -> OrderRequest:
		return cls(symbol=symbol, side=OrderSide(side), type=OrderType.LIMIT, qty=dec(qty),
			limit_price=dec(limit_price), time_in_force=TimeInForce(time_in_force), **kw)

	@classmethod
	def stop(cls, symbol: str, side: OrderSide | str, qty, stop_price, *,
			time_in_force: TimeInForce | str = TimeInForce.DAY, **kw) -> OrderRequest:
		return cls(symbol=symbol, side=OrderSide(side), type=OrderType.STOP, qty=dec(qty),
			stop_price=dec(stop_price), time_in_force=TimeInForce(time_in_force), **kw)

	@classmethod
	def stop_limit(cls, symbol: str, side: OrderSide | str, qty, stop_price, limit_price, *,
			time_in_force: TimeInForce | str = TimeInForce.DAY, **kw) -> OrderRequest:
		return cls(symbol=symbol, side=OrderSide(side), type=OrderType.STOP_LIMIT, qty=dec(qty),
			stop_price=dec(stop_price), limit_price=dec(limit_price),
			time_in_force=TimeInForce(time_in_force), **kw)

	@classmethod
	def bracket(cls, symbol: str, side: OrderSide | str, qty, *, take_profit, stop_loss,
			limit_price=None, stop_loss_limit=None,
			time_in_force: TimeInForce | str = TimeInForce.GTC, **kw) -> OrderRequest:
		"""Entry (market, or limit with ``limit_price``) with a take profit and a stop loss leg."""
		return cls(
			symbol=symbol, side=OrderSide(side), qty=dec(qty),
			type=OrderType.MARKET if limit_price is None else OrderType.LIMIT,
			limit_price=dec(limit_price), time_in_force=TimeInForce(time_in_force),
			order_class=OrderClass.BRACKET,
			take_profit=TakeProfit(limit_price=dec(take_profit)),
			stop_loss=StopLoss(stop_price=dec(stop_loss), limit_price=dec(stop_loss_limit)),
			**kw,
		)


class ReplaceOrderRequest(Request, kw_only=True):
	"""Body of PATCH /v2/orders/{id}: only the fields set are changed."""
	qty: Decimal | None = None
	time_in_force: TimeInForce | None = None
	limit_price: Decimal | None = None
	stop_price: Decimal | None = None
	trail: Decimal | None = None
	client_order_id: str | None = None


class GetOrdersParams(Request, kw_only=True):
	status: QueryOrderStatus | None = None
	limit: int | None = None              # max 500
	after: datetime | None = None
	until: datetime | None = None
	direction: str | None = None          # "asc" | "desc"
	nested: bool | None = None            # bracket legs inside their entry
	symbols: list[str] | None = None
	side: OrderSide | None = None


class GetAssetsParams(Request, kw_only=True):
	status: str | None = None             # "active" | "inactive"
	asset_class: str | None = None        # "us_equity"
	exchange: str | None = None
	attributes: list[str] | None = None
