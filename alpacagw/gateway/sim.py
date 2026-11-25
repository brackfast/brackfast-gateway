"""SimBroker: a simulated account. Orders stay in the gateway and fill from the quotes of the market data.

It knows nothing of Alpaca: it needs a quote feed (``add`` / ``remove`` of a symbol's quotes, ``latest_quote``) and
gets every quote through ``on_quote``. For clients it is a paper account (Welcome: broker "simulated", environment
"paper", account "SIMULATED").

How orders fill (whole orders, no partial fills, no queue, no slippage beyond the spread):

- A buy fills at the ask, a sell at the bid.
- Market: at the next quote. Limit: once the ask is at or below a buy's limit (the bid at or above a sell's); at
  the limit price, or at the ask / bid when the order could fill the moment it arrived or was replaced.
- Stop: once the ask is at or above a buy's stop (the bid at or below a sell's), at the ask / bid then (a gap costs
  what it costs). Stop limit: a limit order from then on.
- Regular hours only (09:30 to 16:00 New York, Monday to Friday; holidays have no quotes), limit orders with
  extended hours 04:00 to 20:00; ``all_hours``: whenever there is a quote, also the last one of a closed market.
- tif day: expires at 16:00 New York (20:00 with extended hours); ioc / fok: canceled when not filled at once.
- Bracket: the legs are held until the entry filled, then one cancels the other. OCO: needs the position's shares
  not held by other exit orders, as at a broker. Cancelling one of two exits cancels both.

The account: cash, positions at the average price, equity = cash + positions at the quotes' middle; ``last_equity``
is the equity when the New York date changed (the day's profit / loss). No margin rules, no fees, no buying power
check. Kept in a JSON file (positions, working orders, today's finished ones), so it survives a restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import msgspec

from ..protocol import FINAL, Account, Fill, Order, Position, Quote

NEW_YORK = ZoneInfo("America/New_York")
ACCOUNT = "SIMULATED"
CASH = 100_000.0
REGULAR = (time(9, 30), time(16, 0))
EXTENDED = (time(4, 0), time(20, 0))


class Pos(msgspec.Struct):
	qty: float                              # > 0 long, < 0 short
	avg: float
	price: float = 0.0                      # the last quote's middle (0: none yet)


class State(msgspec.Struct, kw_only=True):
	"""The file."""
	version: int = 1
	cash: float = CASH
	last_equity: float = CASH
	day: str = ""                           # the New York date last_equity belongs to
	seq: int = 0                            # ids of the simulator's own orders (close position): sim-<seq>
	positions: dict[str, Pos] = msgspec.field(default_factory=dict)
	orders: list[Order] = msgspec.field(default_factory=list)
	triggered: list[str] = msgspec.field(default_factory=list)    # stop limit orders that are limit orders now
	extended: list[str] = msgspec.field(default_factory=list)     # orders with extended hours


class Simulation(msgspec.Struct, kw_only=True):
	"""How ``GatewayCore`` is told to simulate the account."""
	file: Path | None = None                # None: nothing kept (tests)
	cash: float = CASH                      # of a new account, and after a reset
	all_hours: bool = False


def _now() -> datetime:
	return datetime.now(timezone.utc)


def day_end(created: datetime, extended: bool = False) -> datetime:
	"""When a tif day order placed at ``created`` expires: that New York day's close, the next trading day's when
	placed after it or on a weekend (holidays are not known: such an order lasts until the next day's close)."""
	ny = created.astimezone(NEW_YORK)
	end = (EXTENDED if extended else REGULAR)[1]
	day = ny.date()
	if ny.time() >= end:
		day += timedelta(days=1)
	while day.weekday() >= 5:
		day += timedelta(days=1)
	return datetime.combine(day, end, NEW_YORK)


class SimBroker:
	name = "simulated"
	environment = "paper"

	def __init__(self, feed: Any, *, on_order: Callable[[Order], None], on_fill: Callable[[Order, Fill], None],
			file: Path | None = None, cash: float = CASH, all_hours: bool = False,
			clock: Callable[[], datetime] = _now, log: logging.Logger | None = None) -> None:
		self.feed = feed                        # the market data: add / remove (quotes of a symbol), latest_quote
		self.on_order = on_order
		self.on_fill = on_fill
		self.file = file
		self.start_cash = cash
		self.all_hours = all_hours
		self.clock = clock
		self.log = log or logging.getLogger("alpacagw.sim")
		self.orders: dict[str, Order] = {}
		self.pos: dict[str, Pos] = {}
		self.cash = cash
		self.last_equity = cash
		self.day = ""
		self.seq = 0
		self.connected = False
		self._quotes: dict[str, tuple[float, float]] = {}     # symbol -> (bid, ask), the newest
		self._watched: set[str] = set()         # symbols with a position or a working order: their quotes are subscribed
		self._fresh: set[str] = set()           # orders no quote has been held against since they arrived / changed
		self._triggered: set[str] = set()
		self._extended: set[str] = set()
		self._tasks: set[asyncio.Task] = set()

	# -- lifecycle ------------------------------------------------------------------

	async def start(self) -> None:
		self._load()
		for symbol in {o.symbol for o in self.working()} | self.pos.keys():
			self._watch(symbol)
		self.connected = True
		self.log.info("simulated account: cash %.2f, %d position(s), %d working order(s)%s", self.cash, len(self.pos),
			len(self.working()), ", fills at all hours" if self.all_hours else "")

	async def close(self) -> None:
		self.connected = False
		for task in list(self._tasks):
			task.cancel()
		self._save()

	def _load(self) -> None:
		if self.file is None or not self.file.is_file():
			return
		try:
			state = msgspec.json.decode(self.file.read_bytes(), type=State)
		except (OSError, msgspec.DecodeError, msgspec.ValidationError) as exc:
			try:
				copy = self.file.with_name(self.file.name + ".unreadable")
				if not copy.exists():
					copy.write_bytes(self.file.read_bytes())
			except OSError:
				pass
			self.log.warning("%s unreadable (%s): a new simulated account, the file kept as %s.unreadable", self.file,
				exc, self.file.name)
			return
		self.cash, self.last_equity, self.day, self.seq = state.cash, state.last_equity, state.day, state.seq
		self.pos = state.positions
		self.orders = {o.id: o for o in state.orders}
		self._triggered = set(state.triggered) & self.orders.keys()
		self._extended = set(state.extended) & self.orders.keys()

	def _save(self) -> None:
		"""Positions, the working orders and the ones finished today (Welcome sends those), through a temporary
		file."""
		if self.file is None:
			return
		today = self.clock().date()
		orders = [o for o in self.orders.values()
			if o.status not in FINAL or (o.updated_at is not None and o.updated_at.date() == today)]
		ids = {o.id for o in orders}
		state = State(cash=self.cash, last_equity=self.last_equity, day=self.day, seq=self.seq, positions=self.pos,
			orders=orders, triggered=sorted(self._triggered & ids), extended=sorted(self._extended & ids))
		try:
			self.file.parent.mkdir(parents=True, exist_ok=True)
			tmp = self.file.with_name(self.file.name + ".tmp")
			tmp.write_bytes(msgspec.json.format(msgspec.json.encode(state)))
			os.replace(tmp, self.file)
		except OSError as exc:
			self.log.warning("simulated account not saved: %s", exc)

	async def reset(self) -> None:
		"""A new account: working orders canceled, no positions, the starting cash."""
		for o in self.working():
			self._end(o, "canceled", "simulated account reset")
		self.pos.clear()
		self.cash = self.last_equity = self.start_cash
		self.day = ""
		for symbol in list(self._watched):
			self._settle(symbol)
		self._save()
		self.log.info("simulated account reset: cash %.2f", self.cash)

	# -- quotes ---------------------------------------------------------------------

	def _watch(self, symbol: str) -> None:
		if symbol in self._watched:
			return
		self._watched.add(symbol)
		self.feed.add([symbol], quotes=True, bars=False)
		task = asyncio.get_running_loop().create_task(self._seed(symbol))
		self._tasks.add(task)
		task.add_done_callback(self._tasks.discard)

	async def _seed(self, symbol: str) -> None:
		"""The stream sends a quote only when it changes: the newest one is asked for, unless one came meanwhile."""
		try:
			quote = await self.feed.latest_quote(symbol)
		except Exception as exc:
			self.log.info("latest quote %s: %s", symbol, exc)
			return
		if quote is not None and symbol in self._watched and symbol not in self._quotes:
			self.on_quote(quote)

	def _settle(self, symbol: str) -> None:
		"""No position and no working order left: the symbol's quotes are no longer needed."""
		if symbol in self._watched and symbol not in self.pos and not self.working(symbol):
			self._watched.discard(symbol)
			self._quotes.pop(symbol, None)
			self.feed.remove([symbol], quotes=True, bars=False)

	def on_quote(self, quote: Quote) -> None:
		symbol = quote.symbol
		if symbol not in self._watched or not (quote.bid > 0 and quote.ask > 0):
			return
		self._quotes[symbol] = (quote.bid, quote.ask)
		p = self.pos.get(symbol)
		if p is not None:
			p.price = (quote.bid + quote.ask) / 2
		changed = False
		for o in self.working(symbol):
			changed |= self._evaluate(o)
		if changed:
			self._settle(symbol)
			self._save()

	def _in_session(self, order: Order) -> bool:
		if self.all_hours:
			return True
		ny = self.clock().astimezone(NEW_YORK)
		start, end = EXTENDED if order.id in self._extended else REGULAR
		return ny.weekday() < 5 and start <= ny.time() < end

	def _evaluate(self, o: Order) -> bool:
		"""Hold a working order against its symbol's quote; True when it changed (filled, canceled)."""
		quote = self._quotes.get(o.symbol)
		if o.status != "open" or quote is None or not self._in_session(o):
			return False
		buy = o.side == "buy"
		price = quote[1] if buy else quote[0]
		fresh = o.id in self._fresh
		self._fresh.discard(o.id)
		if o.type in ("stop", "stop_limit") and o.id not in self._triggered:
			if (price >= o.stop_price) if buy else (price <= o.stop_price):
				if o.type == "stop":
					self._fill(o, price)
					return True
				self._triggered.add(o.id)       # a limit order from here on
				fresh = True
			else:
				return self._missed(o, fresh)
		if o.type == "market":
			self._fill(o, price)
			return True
		if (price <= o.limit_price) if buy else (price >= o.limit_price):
			self._fill(o, price if fresh else o.limit_price)
			return True
		return self._missed(o, fresh)

	def _missed(self, o: Order, fresh: bool) -> bool:
		if fresh and o.tif in ("ioc", "fok"):
			self._end(o, "canceled", "not filled at once")
			return True
		return False

	# -- orders ---------------------------------------------------------------------

	def working(self, symbol: str | None = None) -> list[Order]:
		return [o for o in self.orders.values() if o.status not in FINAL and (symbol is None or o.symbol == symbol)]

	def _publish(self, order: Order) -> None:
		order.updated_at = self.clock()
		self.on_order(order)

	def _partner(self, o: Order) -> Order | None:
		"""The other exit of a pair: a bracket's other leg, an OCO's other order."""
		if o.parent_id:
			parent = self.orders.get(o.parent_id)
			if parent is None or parent.role != "entry":
				return parent                   # an OCO's stop loss: its take profit
			return next((self.orders[i] for i in parent.leg_ids if i != o.id and i in self.orders), None)
		if o.role == "take_profit" and o.leg_ids:
			return self.orders.get(o.leg_ids[0])
		return None

	def _legs(self, entry: Order) -> list[Order]:
		return [self.orders[i] for i in entry.leg_ids if i in self.orders] if entry.role == "entry" else []

	def _end(self, o: Order, status: str, reason: str = "") -> None:
		o.status, o.reason = status, reason
		self._fresh.discard(o.id)
		self._triggered.discard(o.id)
		self._publish(o)
		for leg in self._legs(o):
			if leg.status not in FINAL:
				self._end(leg, "canceled", "")

	def _fill(self, o: Order, price: float) -> None:
		qty = o.qty - o.filled_qty
		o.filled_qty, o.avg_fill_price, o.status = o.qty, price, "filled"
		self._triggered.discard(o.id)
		self._book(o.symbol, qty if o.side == "buy" else -qty, price)
		self._publish(o)
		self.on_fill(o, Fill(order_id=o.id, symbol=o.symbol, side=o.side, qty=qty, price=price, ts=self.clock(),
			role=o.role))
		legs = self._legs(o)
		for leg in legs:
			if leg.status == "held":            # the exits work from now on
				leg.status = "open"
				self._fresh.add(leg.id)
				self._publish(leg)
		for leg in legs:
			self._evaluate(leg)
		partner = self._partner(o)
		if partner is not None and partner.status not in FINAL:
			self._end(partner, "canceled", "")

	def _book(self, symbol: str, signed: float, price: float) -> None:
		self.cash -= signed * price
		p = self.pos.get(symbol)
		if p is None:
			bid, ask = self._quotes.get(symbol, (price, price))
			self.pos[symbol] = Pos(qty=signed, avg=price, price=(bid + ask) / 2)
			return
		new = p.qty + signed
		if p.qty * signed > 0:                  # added to
			p.avg = (p.avg * abs(p.qty) + price * abs(signed)) / abs(new)
		elif abs(signed) > abs(p.qty):          # through zero: the rest is a new position
			p.avg = price
		p.qty = new
		if abs(new) < 1e-9:
			del self.pos[symbol]

	def _accept(self, orders: list[Order]) -> None:
		for o in orders:
			self.orders[o.id] = o
			if o.status == "open":
				self._fresh.add(o.id)
			self._publish(o)
		self._watch(orders[0].symbol)
		self._evaluate(orders[0])
		self._settle(orders[0].symbol)
		self._save()

	def _reject(self, orders: list[Order], reason: str) -> str:
		for o in orders:
			self.orders[o.id] = o
			o.status, o.reason = "rejected", reason
			self._publish(o)
		return reason

	async def submit_bracket(self, entry: Order, tp: Order, sl: Order, stop_loss_limit: float | None) -> str | None:
		entry.status, tp.status, sl.status = "open", "held", "held"
		self._accept([entry, tp, sl])
		return None

	async def submit_oco(self, tp: Order, sl: Order, stop_loss_limit: float | None) -> str | None:
		p = self.pos.get(tp.symbol)
		held = p.qty if p is not None else 0.0
		shares = max(0.0, held if tp.side == "sell" else -held)      # of the position these exits close
		taken = sum(o.qty for o in self.working(tp.symbol) if o.status == "open" and o.side == tp.side
			and o.role != "stop_loss")          # an exit pair holds its shares once
		if tp.qty > shares - taken + 1e-9:
			return self._reject([tp, sl], f"insufficient qty available for order (requested: {tp.qty:g}, "
				f"available: {max(0.0, shares - taken):g})")
		tp.status = sl.status = "open"
		self.orders[sl.id] = sl
		self._fresh.add(sl.id)
		self._publish(sl)
		self._accept([tp])
		self._evaluate(sl)
		self._settle(tp.symbol)
		self._save()
		return None

	async def submit_order(self, order: Order, extended_hours: bool = False) -> str | None:
		if order.tif in ("opg", "cls"):
			return self._reject([order], f"tif {order.tif} (the opening / closing auction) is not simulated")
		order.status = "open"
		if extended_hours:
			self._extended.add(order.id)
		self._accept([order])
		return None

	async def replace(self, order: Order, *, qty: float | None, limit_price: float | None,
			stop_price: float | None) -> str | None:
		if order.status in FINAL:
			return "not working"
		if qty is not None:
			if not float(qty).is_integer():
				return "qty must be a whole number of shares"
			order.qty = qty
			for leg in self._legs(order):
				leg.qty = qty
				self._publish(leg)
		if limit_price is not None:
			order.limit_price = limit_price
		if stop_price is not None:
			order.stop_price = stop_price
		order.reason = ""
		if order.status == "open":
			self._fresh.add(order.id)           # moved through the market: fills at the market, as a new order would
		self._publish(order)
		self._evaluate(order)
		self._settle(order.symbol)
		self._save()
		return None

	async def cancel(self, order: Order) -> str | None:
		if order.status == "canceled":
			return None                         # with its partner already
		if order.status in FINAL:
			return f"already {order.status}"
		parent = self.orders.get(order.parent_id) if order.parent_id else None
		if parent is not None and parent.role == "entry" and parent.status not in FINAL:
			return "a leg of a working entry: cancel the entry"
		partner = self._partner(order)
		self._end(order, "canceled", "")
		if partner is not None and partner.status not in FINAL:
			self._end(partner, "canceled", "")
		self._settle(order.symbol)
		self._save()
		return None

	async def wait_canceled(self, orders: list[Order], timeout: float = 10.0) -> bool:
		return True                             # a cancel is done when ``cancel`` returns

	async def close_position(self, symbol: str, origin: str) -> Order:
		p = self.pos.get(symbol)
		if p is None:
			raise RuntimeError(f"no position in {symbol}")
		self.seq += 1
		order = Order(id=f"sim-{self.seq}", symbol=symbol, side="sell" if p.qty > 0 else "buy", type="market",
			qty=abs(p.qty), status="open", tif="day", origin=origin, created_at=self.clock())
		self._accept([order])
		return order

	async def flatten(self) -> int:
		for o in self.working():
			if o.status not in FINAL:
				await self.cancel(o)
		symbols = list(self.pos)
		for symbol in symbols:
			await self.close_position(symbol, "gateway")
		return len(symbols)

	async def poll(self, fetch_missing: bool = True) -> None:
		"""Every second: day orders expire at the close; orders waiting for the session are held against the
		quote there is (it may not change at the open)."""
		now = self.clock()
		changed = False
		for o in self.working():
			if o.status in FINAL:
				continue                        # went with its entry in this loop
			if o.tif == "day" and now >= day_end(o.created_at or now, o.id in self._extended):
				self._end(o, "expired", "")
				changed = True
			else:
				changed |= self._evaluate(o)
		if changed:
			for symbol in list(self._watched):
				self._settle(symbol)
			self._save()

	# -- account ---------------------------------------------------------------------

	async def account(self) -> Account:
		gross = sum(abs(p.qty) * (p.price or p.avg) for p in self.pos.values())
		equity = self.cash + sum(p.qty * (p.price or p.avg) for p in self.pos.values())
		today = self.clock().astimezone(NEW_YORK).date().isoformat()
		if self.day != today:                   # a new day: what the account was worth is the day's base
			self.last_equity = equity if self.day else self.last_equity
			self.day = today
			self._save()
		return Account(account=ACCOUNT, equity=round(equity, 2), last_equity=round(self.last_equity, 2),
			cash=round(self.cash, 2), buying_power=round(max(0.0, 2 * equity - gross), 2))

	async def positions(self) -> dict[str, Position]:
		out = {}
		for symbol, p in self.pos.items():
			price = p.price or p.avg
			out[symbol] = Position(symbol=symbol, qty=p.qty, avg_price=round(p.avg, 4), current_price=round(price, 4),
				market_value=round(p.qty * price, 2), unrealized_pl=round((price - p.avg) * p.qty, 2))
		return out
