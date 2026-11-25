"""In-memory stand-ins for the Alpaca client, for the gateway tests: orders behave like Alpaca's
(brackets with held legs, OCOs, replace = new order, cancel of an entry / OCO cancels its legs)."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from decimal import Decimal

import msgspec

from alpacagw.client import Account, BarData, BarsPage, Order, Position, TradeUpdate
from alpacagw.client.models.trading import FINAL_STATUSES, Asset

_ASSET = msgspec.convert({"id": "a1", "class": "us_equity", "exchange": "ARCA", "symbol": "SPY",
	"name": "SPDR S&P 500 ETF Trust", "status": "active", "tradable": True}, Asset)

_ids = itertools.count(1)


def _now() -> datetime:
	return datetime.now(timezone.utc)


class FakeTrading:
	def __init__(self) -> None:
		self.orders: dict[str, Order] = {}
		self.legs: dict[str, list[str]] = {}         # entry id -> leg ids
		self.positions: list[Position] = []
		self.calls: list[str] = []
		self.reject: str | None = None

	def _new(self, **kw) -> Order:
		oid = f"a{next(_ids):04d}"
		kw.setdefault("client_order_id", oid)
		order = Order(id=oid, created_at=_now(), updated_at=_now(), **kw)
		self.orders[oid] = order
		return order

	def _nested(self, order: Order) -> Order:
		legs = [self.orders[i] for i in self.legs.get(order.id, ())]
		return msgspec.structs.replace(order, legs=legs or None)

	# -- the TradingClient methods the gateway uses -----------------------------

	async def get_asset(self, symbol: str):
		self.calls.append(f"asset {symbol}")
		return msgspec.structs.replace(_ASSET, symbol=symbol.upper())

	async def get_account(self) -> Account:
		return Account(id="acc", account_number="PA123", status="ACTIVE", equity=Decimal(1000), last_equity=Decimal(990),
			cash=Decimal(1000), buying_power=Decimal(4000))

	async def get_positions(self) -> list[Position]:
		return list(self.positions)

	async def submit_order(self, req) -> Order:
		self.calls.append(f"submit {req.client_order_id}")
		if self.reject:
			from alpacagw.client import AlpacaError
			raise AlpacaError(403, self.reject)
		entry = self._new(client_order_id=req.client_order_id, symbol=req.symbol, side=str(req.side), type=str(req.type),
			time_in_force=str(req.time_in_force), status="new", qty=req.qty, limit_price=req.limit_price,
			stop_price=req.stop_price, order_class=str(req.order_class or "simple"))
		if req.order_class == "bracket":
			exit_side = "sell" if req.side == "buy" else "buy"
			common = dict(symbol=req.symbol, side=exit_side, time_in_force=str(req.time_in_force), status="held",
				qty=req.qty, order_class="bracket")
			tp = self._new(type="limit", limit_price=req.take_profit.limit_price, **common)
			sl = self._new(type="stop", stop_price=req.stop_loss.stop_price, **common)
			self.legs[entry.id] = [tp.id, sl.id]
		elif req.order_class == "oco":                # the take profit is the order, the stop loss its leg (working)
			entry = self.set(entry.id, limit_price=req.take_profit.limit_price)
			sl = self._new(symbol=req.symbol, side=str(req.side), type="stop", stop_price=req.stop_loss.stop_price,
				time_in_force=str(req.time_in_force), status="new", qty=req.qty, order_class="oco")
			self.legs[entry.id] = [sl.id]
		return self._nested(entry)

	async def get_order(self, order_id: str, *, nested: bool = False) -> Order:
		self.calls.append(f"get {order_id}")
		order = self.orders[order_id]
		return self._nested(order) if nested else order

	async def get_orders(self, params=None) -> list[Order]:
		legs = {i for ids in self.legs.values() for i in ids}
		out = []
		for order in self.orders.values():
			if order.id in legs:
				continue
			tree = [order, *(self.orders[i] for i in self.legs.get(order.id, ()))]
			if params is not None and params.status == "open":
				# Alpaca's open list: held legs are left out (that is why the gateway polls them)
				if order.status in FINAL_STATUSES or order.status == "held":
					if not any(x.status not in FINAL_STATUSES and x.status != "held" for x in tree):
						continue
			out.append(self._nested(order))
		return out

	async def replace_order(self, order_id: str, req) -> Order:
		self.calls.append(f"replace {order_id}")
		return self.replace(order_id, limit_price=req.limit_price, stop_price=req.stop_price, qty=req.qty)

	async def cancel_order(self, order_id: str) -> None:
		self.calls.append(f"cancel {order_id}")
		self.set(order_id, status="canceled")
		for leg in self.legs.get(order_id, ()):
			if self.orders[leg].status not in FINAL_STATUSES:
				self.set(leg, status="canceled")

	async def close_position(self, symbol: str, **kw) -> Order:
		pos = next(p for p in self.positions if p.symbol == symbol)
		return self._new(symbol=symbol, side="sell" if pos.side == "long" else "buy", type="market", time_in_force="day",
			status="new", qty=abs(pos.qty))

	async def close_all_positions(self, *, cancel_orders: bool = False) -> list:
		return []

	# -- what happens at the broker ---------------------------------------------

	def set(self, order_id: str, **changes) -> Order:
		self.orders[order_id] = msgspec.structs.replace(self.orders[order_id], updated_at=_now(), **changes)
		return self.orders[order_id]

	def fill(self, order_id: str, qty: float, price: float) -> Order:
		o = self.orders[order_id]
		cum = float(o.filled_qty) + qty
		avg = price if not o.filled_avg_price else (float(o.filled_avg_price) * float(o.filled_qty) + price * qty) / cum
		status = "filled" if cum >= float(o.qty) else "partially_filled"
		o = self.set(order_id, filled_qty=Decimal(str(cum)), filled_avg_price=Decimal(str(round(avg, 4))), status=status)
		if status == "filled":
			for leg in self.legs.get(order_id, ()):
				self.set(leg, status="new")
		return o

	def replace(self, order_id: str, *, limit_price=None, stop_price=None, qty=None) -> Order:
		"""A replace, as Alpaca does it: a new order, the old one "replaced"."""
		old = self.orders[order_id]
		new = self._new(symbol=old.symbol, side=old.side, type=old.type, time_in_force=old.time_in_force, status=old.status,
			qty=qty if qty is not None else old.qty, limit_price=limit_price if limit_price is not None else old.limit_price,
			stop_price=stop_price if stop_price is not None else old.stop_price, order_class=old.order_class,
			replaces=old.id)
		self.set(order_id, status="replaced", replaced_by=new.id)
		for entry, legs in self.legs.items():
			if order_id in legs:
				self.legs[entry] = [new.id if i == order_id else i for i in legs]
		return new

	def update(self, order_id: str, event: str, **kw) -> TradeUpdate:
		return TradeUpdate(event=event, order=self.orders[order_id], timestamp=_now(), **kw)


class FakeStream:
	def __init__(self) -> None:
		self.handlers: dict[type, list] = {}
		self.ready: list = []
		self.gone: list = []
		self.subs: dict[str, set[str]] = {}
		self.is_ready = False

	def on(self, msg_type, handler=None, **kw):
		self.handlers.setdefault(msg_type, []).append(handler)

	def on_trade_update(self, handler):
		self.on(TradeUpdate, handler)

	def on_ready(self, handler):
		self.ready.append(handler)

	def on_disconnected(self, handler):
		self.gone.append(handler)

	def subscribe(self, channel, *symbols):
		self.subs.setdefault(channel, set()).update(symbols)

	def unsubscribe(self, channel, *symbols):
		self.subs.setdefault(channel, set()).difference_update(symbols)

	def emit(self, msg) -> None:
		for h in self.handlers.get(type(msg), ()):
			h(msg)

	async def start(self) -> None:
		self.is_ready = True
		for h in self.ready:
			h(None)

	async def stop(self) -> None:
		self.is_ready = False


class FakeData:
	def __init__(self) -> None:
		self.requests = []
		self.trades: dict[str, list] = {}           # get_trades' answer, {symbol: [TradeData]}
		self.latest: dict = {}                      # get_latest_quotes' answer, {symbol: QuoteData}
		self.latest_trades: dict = {}               # get_latest_trades' answer, {symbol: TradeData}

	async def get_trades(self, params, *, max_pages=None):
		self.requests.append(params)
		return {s: [t for t in self.trades.get(s, ()) if params.start <= t.timestamp <= params.end]
			for s in params.symbols}

	async def get_latest_quotes(self, symbols, *, feed=None):
		self.requests.append(("latest quotes", symbols))
		return dict(self.latest)

	async def get_latest_trades(self, symbols, *, feed=None):
		self.requests.append(("latest trades", symbols))
		return dict(self.latest_trades)

	async def iter_bars(self, params):
		self.requests.append(params)
		start = datetime(2026, 10, 27, 13, 30, tzinfo=timezone.utc)
		bars = [BarData(timestamp=start.replace(minute=30 + i), open=10 + i, high=11 + i, low=9 + i, close=10.5 + i,
			volume=100) for i in range(5)]
		yield BarsPage(data={params.symbols[0]: list(reversed(bars))})    # sort desc: newest first


class FakeAlpaca:
	environment = "paper"

	def __init__(self) -> None:
		self.trading = FakeTrading()
		self.data = FakeData()
		self.orders = FakeStream()
		self.stocks = FakeStream()

	async def close(self) -> None:
		pass
