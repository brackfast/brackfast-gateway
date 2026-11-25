"""OrderTracker: the account's orders at Alpaca as gateway Orders.

Ids: an order a client submits keeps the client's id (sent to Alpaca as client_order_id); its
bracket legs are ``<id>.tp`` / ``<id>.sl``. Orders placed elsewhere (Alpaca's web site,
TradingView, other apps) are adopted under their client_order_id, origin "external".

Things Alpaca does that the tracker hides from its clients:

- A replace makes a new Alpaca order (new id, ``replaces`` -> the old one). The gateway Order
  keeps its id, role and bracket links and moves to the new Alpaca order (``_take_over``), also
  for replaces made elsewhere; a chain of replaces is followed (``_follow``, ``_link_back``).
- The trade-update stream does not report every change: a held bracket leg moved elsewhere
  sends nothing. ``poll`` (every second) reads Alpaca's open orders; held legs missing from that
  list are fetched one by one.
- An unknown order is adopted only after ``adopt_grace`` seconds: it may turn out to be the
  replacement of one of ours (two quick drags of a take profit), and adopting it at once would
  leave two Orders on one Alpaca order.
- Fills come from the cumulative filled qty / average price of each update, so a missed or
  repeated update never counts a fill twice.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..alpaca import (
	GetOrdersParams, OrderRequest, QueryOrderStatus, ReplaceOrderRequest, TradeUpdate,
)
from ..alpaca import Order as AlpacaOrder
from ..alpaca.models.base import dec
from ..protocol import FINAL, Fill, Order

STATUS = {
	"new": "open",
	"accepted": "open",
	"pending_new": "pending",
	"accepted_for_bidding": "open",
	"pending_cancel": "open",
	"pending_replace": "open",
	"calculated": "open",
	"stopped": "open",
	"suspended": "open",
	"held": "held",
	"partially_filled": "partially_filled",
	"filled": "filled",
	"canceled": "canceled",
	"expired": "expired",
	"done_for_day": "expired",
	"rejected": "rejected",
}

_OURS = object()        # _link_back: the chain belongs to one of our Orders, which is further on already


class _Polled:
	"""An order from the open-orders list, in the shape of a stream update."""
	__slots__ = ("order",)
	event = "polled"
	timestamp = None
	price = None

	def __init__(self, order: AlpacaOrder) -> None:
		self.order = order


def _f(x: Any) -> float | None:
	return None if x is None else float(x)


def _now() -> datetime:
	return datetime.now(timezone.utc)


def _is_final(order: Order) -> bool:
	return order.status in FINAL


class OrderTracker:
	def __init__(self, trading: Any, *, on_order: Callable[[Order], None], on_fill: Callable[[Order, Fill], None],
			log: logging.Logger | None = None, adopt_grace: float = 2.0) -> None:
		self.trading = trading                    # brackfastgw.alpaca.TradingClient (or a fake in tests)
		self.on_order = on_order
		self.on_fill = on_fill
		self.log = log or logging.getLogger("brackfastgw.tracker")
		self.adopt_grace = adopt_grace
		self.orders: dict[str, Order] = {}        # gateway id -> Order
		self._by_broker: dict[str, Order] = {}    # current Alpaca id -> Order
		self._former: dict[str, Order] = {}       # Alpaca ids an Order has left after a replace
		self._pending: dict[str, AlpacaOrder] = {}    # unknown Alpaca orders waiting for adoption
		self._acked: dict[str, asyncio.Event] = {}    # gateway id -> set once Alpaca answered the submit
		self._cancels: dict[str, asyncio.Event] = {}  # gateway id -> set once final after a cancel
		self._inflight = 0                        # submits / replaces waiting for Alpaca's answer
		self._parked: list[Any] = []              # updates of unknown orders meanwhile
		self._polling = False
		self._checked: dict[str, float] = {}      # Alpaca id -> monotonic time of its last single fetch
		self._unlisted: set[str] = set()
		self._replace_n = 0
		self._tasks: set[asyncio.Task] = set()

	# -- helpers ------------------------------------------------------------------

	def _spawn(self, coro) -> asyncio.Task:
		task = asyncio.get_running_loop().create_task(coro)
		self._tasks.add(task)
		task.add_done_callback(self._task_done)
		return task

	def _task_done(self, task: asyncio.Task) -> None:
		self._tasks.discard(task)
		if not task.cancelled() and task.exception() is not None:
			self.log.error("order task failed", exc_info=task.exception())

	async def close(self) -> None:
		for task in list(self._tasks):
			task.cancel()
		if self._tasks:
			await asyncio.gather(*self._tasks, return_exceptions=True)

	def working(self, symbol: str | None = None) -> list[Order]:
		return [o for o in self.orders.values() if not _is_final(o) and (symbol is None or o.symbol == symbol)]

	def _publish(self, order: Order) -> None:
		order.updated_at = _now()
		self.on_order(order)

	def _register(self, order: Order) -> None:
		self.orders[order.id] = order
		if order.broker_id:
			self._by_broker[order.broker_id] = order

	# -- startup ------------------------------------------------------------------

	async def load(self) -> None:
		"""Adopt the account's working orders. Brackets with a working piece come as brackets
		(entry + legs linked); the entry may be filled already, so closed orders are read too."""
		adopted: set[str] = set()
		for ao in await self.trading.get_orders(GetOrdersParams(status=QueryOrderStatus.ALL, nested=True, limit=500)):
			tree = [ao, *(ao.legs or ())]
			if ao.legs and any(x.status != "replaced" and STATUS.get(x.status, "open") not in FINAL for x in tree):
				self._adopt_bracket(ao)
				adopted.update(x.id for x in tree)
		for ao in await self.trading.get_orders(GetOrdersParams(status=QueryOrderStatus.OPEN, nested=True, limit=500)):
			if ao.id in adopted:
				continue
			if ao.legs:
				self._adopt_bracket(ao)
			else:
				self._adopt(ao)
		self.log.info("%d working orders at start", len(self.working()))

	# -- commands -----------------------------------------------------------------

	async def submit_bracket(self, entry: Order, tp: Order, sl: Order, stop_loss_limit: float | None) -> str | None:
		"""Send a checked bracket; returns the reason when Alpaca refuses it."""
		req = OrderRequest.bracket(entry.symbol, entry.side, entry.qty, take_profit=tp.limit_price,
			stop_loss=sl.stop_price, stop_loss_limit=stop_loss_limit, limit_price=entry.limit_price,
			stop_price=entry.stop_price,
			time_in_force=entry.tif, client_order_id=entry.id)
		return await self._submit(req, entry, [tp, sl])

	async def submit_oco(self, tp: Order, sl: Order, stop_loss_limit: float | None) -> str | None:
		"""Send checked exits (one cancels the other): Alpaca's order is the take profit, the stop loss its leg."""
		req = OrderRequest.oco(tp.symbol, tp.side, tp.qty, take_profit=tp.limit_price, stop_loss=sl.stop_price,
			stop_loss_limit=stop_loss_limit, time_in_force=tp.tif, client_order_id=tp.id)
		return await self._submit(req, tp, [sl])

	async def submit_order(self, order: Order, extended_hours: bool = False) -> str | None:
		kw = dict(time_in_force=order.tif, client_order_id=order.id)
		if extended_hours:
			kw["extended_hours"] = True
		match order.type:
			case "market":
				req = OrderRequest.market(order.symbol, order.side, order.qty, **kw)
			case "limit":
				req = OrderRequest.limit(order.symbol, order.side, order.qty, order.limit_price, **kw)
			case "stop":
				req = OrderRequest.stop(order.symbol, order.side, order.qty, order.stop_price, **kw)
			case _:
				req = OrderRequest.stop_limit(order.symbol, order.side, order.qty, order.stop_price,
					order.limit_price, **kw)
		return await self._submit(req, order, [])

	async def _submit(self, req: OrderRequest, order: Order, legs: list[Order]) -> str | None:
		for o in (order, *legs):
			self._register(o)
			self._publish(o)
		self._acked[order.id] = asyncio.Event()
		self._inflight += 1
		try:
			await self._wait_cancels(order.symbol)
			try:
				ao = await self.trading.submit_order(req)
			except Exception as exc:
				self.log.warning("order %s rejected: %s", order.id, exc)
				self._final_local(order, "rejected", str(exc))
				for leg in legs:
					self._final_local(leg, "rejected", "entry rejected")
				return str(exc)
			order.broker_id = ao.id
			self._by_broker[ao.id] = order
			for leg_ao in ao.legs or ():
				leg = next((l for l in legs if l.role == self._leg_role(leg_ao) and not l.broker_id), None)
				if leg is not None:
					leg.broker_id = leg_ao.id
					self._by_broker[leg_ao.id] = leg
					self._apply(leg, leg_ao, None)
			self._apply(order, ao, None)
			return None
		finally:
			self._acked[order.id].set()
			self._unpark()

	async def _wait_cancels(self, symbol: str) -> None:
		"""An exit right after cancels of the same symbol: Alpaca rejects it while the shares
		are still held by the canceled orders."""
		pending = [e for oid, e in self._cancels.items() if not e.is_set() and self.orders[oid].symbol == symbol]
		if pending:
			try:
				await asyncio.wait_for(asyncio.gather(*(e.wait() for e in pending)), 10.0)
			except TimeoutError:
				self.log.warning("%s: cancels still pending, submitting anyway", symbol)

	def _unpark(self) -> None:
		self._inflight -= 1
		parked, self._parked = self._parked, []
		for u in parked:
			self.on_update(u)

	async def _wait_ack(self, order: Order) -> bool:
		ev = self._acked.get(order.id)
		if ev is not None and not ev.is_set():
			try:
				await asyncio.wait_for(ev.wait(), 10.0)
			except TimeoutError:
				return False
		return bool(order.broker_id)

	async def cancel(self, order: Order) -> str | None:
		if _is_final(order):
			return f"already {order.status}"
		if order.parent_id and not order.broker_id:
			self._final_local(order, "canceled", "")    # a leg Alpaca has not created: goes with its entry
			return None
		self._cancels.setdefault(order.id, asyncio.Event())
		if not await self._wait_ack(order) or _is_final(order):
			self._cancels.pop(order.id, asyncio.Event()).set()
			return None if _is_final(order) else "not acknowledged by Alpaca"
		try:
			await self.trading.cancel_order(order.broker_id)
		except Exception as exc:
			self._cancels.pop(order.id, asyncio.Event()).set()
			return str(exc)
		return None                                     # the stream reports the cancel

	async def wait_canceled(self, orders: list[Order], timeout: float = 10.0) -> bool:
		events = [self._cancels[o.id] for o in orders if o.id in self._cancels]
		try:
			await asyncio.wait_for(asyncio.gather(*(e.wait() for e in events)), timeout)
			return True
		except TimeoutError:
			return False

	async def replace(self, order: Order, *, qty: float | None, limit_price: float | None,
			stop_price: float | None) -> str | None:
		self._inflight += 1
		try:
			if not await self._wait_ack(order) or _is_final(order):
				return "not working"
			self._replace_n += 1
			req = ReplaceOrderRequest(qty=dec(qty), limit_price=dec(limit_price), stop_price=dec(stop_price),
				client_order_id=f"{order.id}.r{self._replace_n}")
			old = order.broker_id        # the stream may move the Order to the new id before the answer
			try:
				ao = await self.trading.replace_order(old, req)
			except Exception as exc:
				order.reason = f"replace failed: {exc}"
				self._publish(order)
				return str(exc)
			if self._by_broker.get(old) is order:
				del self._by_broker[old]
			self._former[old] = order
			order.broker_id = ao.id
			self._by_broker[ao.id] = order
			if qty is not None:
				order.qty = qty
			if limit_price is not None:
				order.limit_price = limit_price
			if stop_price is not None:
				order.stop_price = stop_price
			order.reason = ""
			self._apply(order, ao, None)
			self._publish(order)         # always: a held leg gets no stream update
			return None
		finally:
			self._unpark()

	async def close_position(self, symbol: str, origin: str) -> Order:
		"""Alpaca's close position (a market order), tracked as an Order of ``origin``."""
		self._inflight += 1
		try:
			ao = await self.trading.close_position(symbol)
			order = self._by_broker.get(ao.id) or self._adopt(ao, origin=origin)
			self._apply(order, ao, None)
			return order
		finally:
			self._unpark()

	# -- updates ------------------------------------------------------------------

	def on_update(self, u: TradeUpdate | _Polled) -> None:
		ao = u.order
		order = self._by_broker.get(ao.id)
		if u.event != "polled":
			self.log.info("alpaca %s: %s %s %s %s qty %s limit %s stop %s, class %s, id %s%s%s%s", u.event,
				ao.symbol, ao.side, ao.type, ao.status, ao.qty, ao.limit_price, ao.stop_price, ao.order_class,
				ao.id[:8], f", replaces {ao.replaces[:8]}" if ao.replaces else "",
				f", replaced by {ao.replaced_by[:8]}" if ao.replaced_by else "",
				f" = {order.id}" if order is not None else " (unknown)")
		if order is None and ao.id in self._former:
			return                       # a late update of an Alpaca order one of ours has left
		if ao.status == "replaced":
			# the old order of a replace: the Order goes on as the new one (fetched; the new
			# one's own events may come first). Unknown: the new one is adopted with its events.
			if order is not None and ao.replaced_by:
				self._spawn(self._follow(order, ao.id, ao.replaced_by))
			return
		if order is None and ao.replaces:
			order = self._take_over(ao.replaces, ao)
		if order is None:
			if self._inflight:
				self._parked.append(u)   # maybe an order whose submit has not returned yet
			else:
				self._adopt_later(ao)
			return
		self._apply(order, ao, u)

	def _apply(self, order: Order, ao: AlpacaOrder, update: Any) -> None:
		changed = False
		cum = float(ao.filled_qty or 0)
		if cum > order.filled_qty + 1e-9:
			inc = cum - order.filled_qty
			avg = _f(ao.filled_avg_price)
			if avg is None and update is not None:
				avg = _f(update.price)
			if avg is None:
				avg = math.nan
			prev = order.avg_fill_price
			price = (avg * cum - prev * order.filled_qty) / inc if order.filled_qty > 0 and prev is not None else avg
			order.filled_qty = cum
			order.avg_fill_price = avg
			ts = update.timestamp if update is not None and update.timestamp is not None else _now()
			self.on_fill(order, Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=inc, price=price,
				ts=ts, role=order.role))
			changed = True
		status = STATUS.get(ao.status)
		if _is_final(order) and status not in FINAL:
			status = None                # an older update applied late
		if status is not None and status != order.status:
			if not (status == "open" and order.status == "partially_filled"):
				order.status = status
				changed = True
		if ao.qty is not None and float(ao.qty) != order.qty and order.role != "entry":
			order.qty = float(ao.qty)
			changed = True
		for name in ("limit_price", "stop_price"):     # changed by a replace, also one made elsewhere
			price = _f(getattr(ao, name))
			if price is not None and price != getattr(order, name):
				setattr(order, name, price)
				changed = True
		if changed:
			self._publish(order)
		if _is_final(order):
			ev = self._cancels.pop(order.id, None)
			if ev is not None:
				ev.set()

	def _final_local(self, order: Order, status: str, reason: str) -> None:
		order.status = status
		order.reason = reason
		self._publish(order)
		ev = self._cancels.pop(order.id, None)
		if ev is not None:
			ev.set()

	# -- replaces -------------------------------------------------------------------

	def _take_over(self, old_broker_id: str, ao: AlpacaOrder) -> Order | None:
		"""The Order on Alpaca order old_broker_id goes on as ao (its replacement). If another
		Order was adopted on ao meanwhile, the one with a role (entry / leg) stays."""
		order = self._by_broker.get(old_broker_id)
		if order is None or order.broker_id != old_broker_id:
			return None
		del self._by_broker[old_broker_id]
		self._former[old_broker_id] = order
		other = self._by_broker.get(ao.id)
		if other is not None and other is not order:
			if order.role == "simple" and other.role != "simple":
				self._retire(order, other)
				return other
			self._retire(other, order)
		self._pending.pop(ao.id, None)
		order.broker_id = ao.id
		self._by_broker[ao.id] = order
		self.log.info("order %s %s replaced at alpaca: limit %s stop %s qty %s", order.id, order.symbol,
			ao.limit_price, ao.stop_price, ao.qty)
		return order

	def _retire(self, dup: Order, keep: Order) -> None:
		if dup.filled_qty > keep.filled_qty:
			keep.filled_qty, keep.avg_fill_price = dup.filled_qty, dup.avg_fill_price
		self.log.warning("order %s was a duplicate of %s (alpaca %s): removed", dup.id, keep.id, dup.broker_id)
		self._final_local(dup, "canceled", f"duplicate of {keep.id}")

	async def _follow(self, order: Order, old_broker_id: str, new_broker_id: str) -> None:
		"""A replaced order: fetch its replacement (and theirs) and go on with it."""
		for _ in range(10):
			if order.broker_id == new_broker_id or _is_final(order):
				return
			try:
				ao = await self.trading.get_order(new_broker_id)
			except Exception as exc:
				self.log.warning("replacement %s of %s not found: %s", new_broker_id, order.id, exc)
				return
			if order.broker_id == new_broker_id:
				return
			kept = self._take_over(old_broker_id, ao)
			if kept is None:
				return
			if kept is not order:
				if ao.status != "replaced":
					self._apply(kept, ao, None)
				return
			if ao.status != "replaced" or not ao.replaced_by:
				self._apply(order, ao, None)
				return
			old_broker_id, new_broker_id = ao.id, ao.replaced_by

	async def _link_back(self, ao: AlpacaOrder) -> Order | None | object:
		"""Follow ``replaces`` back to one of ours, which then goes on as ao. _OURS: the chain
		reaches an Alpaca order one of ours has left already (ao is a stale step)."""
		cur = ao
		for _ in range(10):
			prev = cur.replaces
			if not prev:
				return None
			order = self._by_broker.get(prev)
			if order is not None and order.broker_id == prev:
				return self._take_over(prev, ao)
			if prev in self._former:
				return _OURS
			try:
				cur = await self.trading.get_order(prev)
			except Exception as exc:
				self.log.warning("order %s replaced by %s not found: %s", prev, ao.id, exc)
				return None
		return None

	# -- adoption -------------------------------------------------------------------

	def _adopt_later(self, ao: AlpacaOrder) -> None:
		first = ao.id not in self._pending
		self._pending[ao.id] = ao
		if first:
			self._spawn(self._adopt_after(ao.id))

	async def _adopt_after(self, broker_id: str) -> None:
		await asyncio.sleep(self.adopt_grace)
		ao = self._pending.pop(broker_id, None)
		if ao is None or broker_id in self._by_broker:
			return
		try:
			ao = await self.trading.get_order(broker_id)     # its state now
		except Exception as exc:
			self.log.warning("order %s to adopt not found: %s", broker_id, exc)
		if broker_id in self._by_broker or broker_id in self._former or ao.status == "replaced":
			return
		order = await self._link_back(ao)
		if order is _OURS:
			return
		if order is None:
			if STATUS.get(ao.status) in FINAL and not float(ao.filled_qty or 0):
				return                   # someone else's order, done without a fill
			if broker_id in self._by_broker:
				return
			if ao.order_class in ("bracket", "oco"):
				top = await self._bracket_of(ao)
				if top is not None:
					for piece, piece_ao in self._adopt_bracket(top, count_fills=True):
						self._apply(piece, piece_ao, None)
					return
			order = self._adopt(ao, count_fills=True)
		self._apply(order, ao, None)

	async def _bracket_of(self, ao: AlpacaOrder) -> AlpacaOrder | None:
		"""The bracket (entry with nested legs) Alpaca order ao belongs to."""
		if ao.legs:
			return ao
		try:
			tops = await self.trading.get_orders(GetOrdersParams(status=QueryOrderStatus.ALL, nested=True,
				symbols=[ao.symbol], after=ao.created_at - timedelta(minutes=1), limit=100))
		except Exception as exc:
			self.log.warning("bracket of %s %s not found: %s", ao.symbol, ao.id, exc)
			return None
		return next((t for t in tops if t.legs and (t.id == ao.id or any(l.id == ao.id for l in t.legs))), None)

	@staticmethod
	def _leg_role(ao: AlpacaOrder) -> str:
		return "take_profit" if ao.type == "limit" else "stop_loss"

	def _adopt_bracket(self, ao: AlpacaOrder, *, count_fills: bool = False) -> list[tuple[Order, AlpacaOrder]]:
		"""A bracket (entry with legs) or an OCO (the take profit with the stop loss as its leg)."""
		legs = list(ao.legs or ())
		role = self._leg_role(ao) if ao.order_class == "oco" else "entry"
		entry = self._adopt_piece(ao, role, "", [l.client_order_id or l.id for l in legs], count_fills)
		out = [(entry, ao)]
		for leg in legs:
			out.append((self._adopt_piece(leg, self._leg_role(leg), entry.id, [], count_fills), leg))
		return out

	def _adopt_piece(self, ao: AlpacaOrder, role: str, parent_id: str, leg_ids: list[str], count_fills: bool) -> Order:
		self._pending.pop(ao.id, None)
		order = self._by_broker.get(ao.id)
		if order is None:
			return self._adopt(ao, role=role, parent_id=parent_id, leg_ids=leg_ids, count_fills=count_fills)
		if order.role == "simple" and order.origin == "external":
			order.role, order.parent_id, order.leg_ids = role, parent_id, list(leg_ids)
			self._publish(order)
		return order

	def _adopt(self, ao: AlpacaOrder, *, role: str = "simple", parent_id: str = "", leg_ids: list[str] | None = None,
			count_fills: bool = False, origin: str = "external") -> Order:
		"""An order not submitted through this gateway run. count_fills: one appearing while
		running, whose fills are reported by _apply from here on."""
		order = Order(
			id=ao.client_order_id or ao.id, symbol=ao.symbol, side=ao.side, type=ao.type, qty=float(ao.qty or 0),
			status="open" if count_fills else STATUS.get(ao.status, "open"), role=role, parent_id=parent_id,
			leg_ids=list(leg_ids or ()), limit_price=_f(ao.limit_price), stop_price=_f(ao.stop_price),
			tif=ao.time_in_force, filled_qty=0.0 if count_fills else float(ao.filled_qty or 0),
			avg_fill_price=None if count_fills else _f(ao.filled_avg_price), origin=origin, broker_id=ao.id,
			created_at=ao.created_at, updated_at=ao.updated_at,
		)
		self._register(order)
		self.log.info("adopted %s %s %s %s qty %s limit %s stop %s as %s %s (alpaca %s)", ao.symbol, ao.side, ao.type,
			ao.status, ao.qty, ao.limit_price, ao.stop_price, role, order.id, ao.id[:8])
		self.on_order(order)
		return order

	# -- polling / reconcile ----------------------------------------------------------

	async def poll(self, fetch_missing: bool = True) -> None:
		"""Alpaca's open orders handled as updates. Our working orders missing from that list
		(held bracket legs) are fetched one by one: the one checked longest ago on every poll,
		with fetch_missing also all not checked for 4.5 s."""
		if self._inflight or self._polling:
			return
		self._polling = True
		try:
			found = await self.trading.get_orders(GetOrdersParams(status=QueryOrderStatus.OPEN, nested=True, limit=500))
			if self._inflight:
				return                   # a submit started meanwhile: its orders may be in the list
			seen: set[str] = set()
			for top in found:
				for ao in (top, *(top.legs or ())):
					seen.add(ao.id)
					order = self._by_broker.get(ao.id)
					if order is None or not _is_final(order):
						self.on_update(_Polled(ao))
			missing = [(bid, o) for bid, o in self._by_broker.items()
				if not _is_final(o) and bid not in seen and o.broker_id == bid]
			for bid, o in missing:
				if bid not in self._unlisted:
					self._unlisted.add(bid)
					self.log.info("poll: %s %s (%s) is not in alpaca's open orders: fetched one by one", o.id,
						o.symbol, o.status)
			now = time.monotonic()
			missing.sort(key=lambda m: self._checked.get(m[0], 0.0))
			missing = missing[:1] + [m for m in missing[1:] if fetch_missing and now - self._checked.get(m[0], 0.0) >= 4.5]
			for broker_id, order in missing:
				self._checked[broker_id] = time.monotonic()
				try:
					ao = await self.trading.get_order(broker_id)
				except Exception as exc:
					self.log.warning("poll %s failed: %s", order.id, exc)
					continue
				if self._by_broker.get(broker_id) is order and not _is_final(order):
					self.on_update(_Polled(ao))
		finally:
			self._polling = False

	async def reconcile(self) -> None:
		"""After the trade-update stream (re)connected: every working order fetched again."""
		for broker_id, order in list(self._by_broker.items()):
			if _is_final(order) or order.broker_id != broker_id:
				continue
			try:
				ao = await self.trading.get_order(broker_id)
			except Exception as exc:
				self.log.warning("reconcile %s failed: %s", order.id, exc)
				continue
			if ao.status == "replaced" and ao.replaced_by:
				await self._follow(order, broker_id, ao.replaced_by)
			else:
				self._apply(order, ao, None)
