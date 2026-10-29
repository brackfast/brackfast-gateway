import asyncio

import pytest

from alpacagw.gateway.orders import OrderTracker
from alpacagw.protocol import Order

from fake_broker import FakeTrading


class Recorder:
	def __init__(self) -> None:
		self.orders: list[tuple[str, str, float | None, float | None]] = []
		self.fills = []

	def on_order(self, o: Order) -> None:
		self.orders.append((o.id, o.status, o.limit_price, o.stop_price))

	def on_fill(self, o: Order, fill) -> None:
		self.fills.append((fill.order_id, fill.qty, fill.price))


@pytest.fixture
def setup():
	broker = FakeTrading()
	rec = Recorder()
	tracker = OrderTracker(broker, on_order=rec.on_order, on_fill=rec.on_fill, adopt_grace=0.05)
	return broker, rec, tracker


def bracket(id="b1", limit=100.0):
	entry = Order(id=id, symbol="AAPL", side="buy", type="limit", qty=10, status="pending", role="entry",
		leg_ids=[f"{id}.tp", f"{id}.sl"], limit_price=limit, tif="gtc")
	tp = Order(id=f"{id}.tp", symbol="AAPL", side="sell", type="limit", qty=10, status="pending", role="take_profit",
		parent_id=id, limit_price=110.0, tif="gtc")
	sl = Order(id=f"{id}.sl", symbol="AAPL", side="sell", type="stop", qty=10, status="pending", role="stop_loss",
		parent_id=id, stop_price=95.0, tif="gtc")
	return entry, tp, sl


async def submitted(broker, tracker):
	entry, tp, sl = bracket()
	assert await tracker.submit_bracket(entry, tp, sl, None) is None
	return entry, tp, sl


async def test_bracket_legs_and_fills(setup):
	broker, rec, tracker = setup
	entry, tp, sl = await submitted(broker, tracker)
	assert (entry.status, tp.status, sl.status) == ("open", "held", "held")
	assert tp.broker_id and sl.broker_id and entry.broker_id
	assert broker.orders[entry.broker_id].client_order_id == "b1"

	broker.fill(entry.broker_id, 4, 99.9)
	tracker.on_update(broker.update(entry.broker_id, "partial_fill"))
	tracker.on_update(broker.update(entry.broker_id, "partial_fill"))       # repeated: no second fill
	broker.fill(entry.broker_id, 6, 100.0)
	tracker.on_update(broker.update(entry.broker_id, "fill"))
	assert rec.fills == [("b1", 4.0, 99.9), ("b1", 6.0, pytest.approx(100.0))]
	assert entry.status == "filled" and entry.filled_qty == 10

	await tracker.poll()                                                    # the legs are working now
	assert (tp.status, sl.status) == ("open", "open")


async def test_replace_keeps_the_id(setup):
	broker, rec, tracker = setup
	entry, tp, sl = await submitted(broker, tracker)
	old = tp.broker_id
	assert await tracker.replace(tp, qty=None, limit_price=111.0, stop_price=None) is None
	assert tp.broker_id != old and tp.limit_price == 111.0 and tp.id == "b1.tp"
	tracker.on_update(broker.update(old, "replaced"))                       # the old order's late event
	await asyncio.sleep(0)
	assert tracker.orders["b1.tp"].broker_id == tp.broker_id
	assert len(tracker.orders) == 3


async def test_held_leg_moved_elsewhere_is_found_by_the_poll(setup):
	broker, rec, tracker = setup
	entry, tp, sl = await submitted(broker, tracker)
	new = broker.replace(sl.broker_id, stop_price=96.5)                     # TradingView: no stream event
	await tracker.poll()
	for _ in range(5):
		await asyncio.sleep(0)
	assert sl.broker_id == new.id and sl.stop_price == 96.5
	assert len(tracker.orders) == 3


async def test_replace_elsewhere_reported_by_the_stream(setup):
	broker, rec, tracker = setup
	entry, tp, sl = await submitted(broker, tracker)
	broker.fill(entry.broker_id, 10, 100.0)
	tracker.on_update(broker.update(entry.broker_id, "fill"))
	new = broker.replace(tp.broker_id, limit_price=112.0)
	tracker.on_update(broker.update(new.id, "new"))                         # the new order first
	assert tp.broker_id == new.id and tp.limit_price == 112.0
	tracker.on_update(broker.update(new.replaces, "replaced"))
	await asyncio.sleep(0.1)
	assert len(tracker.orders) == 3


async def test_external_orders_are_adopted_after_the_grace(setup):
	broker, rec, tracker = setup
	ext = broker._new(client_order_id="web-1", symbol="MSFT", side="buy", type="limit", time_in_force="day",
		status="new", qty=5, limit_price=400)
	tracker.on_update(broker.update(ext.id, "new"))
	assert "web-1" not in tracker.orders
	await asyncio.sleep(0.15)
	o = tracker.orders["web-1"]
	assert o.origin == "external" and o.status == "open" and o.limit_price == 400


async def test_external_bracket_adopted_as_bracket(setup):
	broker, rec, tracker = setup
	from alpacagw.client import OrderRequest
	ext = await broker.submit_order(OrderRequest.bracket("NVDA", "buy", 3, take_profit=150, stop_loss=120,
		limit_price=130, client_order_id="tv-7"))
	for o in (ext, *ext.legs):
		tracker.on_update(broker.update(o.id, "new"))
	await asyncio.sleep(0.15)
	entry = tracker.orders["tv-7"]
	assert entry.role == "entry" and len(entry.leg_ids) == 2
	roles = sorted(tracker.orders[i].role for i in entry.leg_ids)
	assert roles == ["stop_loss", "take_profit"]
	assert all(tracker.orders[i].parent_id == "tv-7" for i in entry.leg_ids)


async def test_rejected_bracket(setup):
	broker, rec, tracker = setup
	broker.reject = "insufficient buying power"
	entry, tp, sl = bracket("b2")
	reason = await tracker.submit_bracket(entry, tp, sl, None)
	assert "insufficient" in reason
	assert (entry.status, tp.status, sl.status) == ("rejected", "rejected", "rejected")


async def test_cancel_entry_cancels_legs(setup):
	broker, rec, tracker = setup
	entry, tp, sl = await submitted(broker, tracker)
	assert await tracker.cancel(entry) is None
	for o in (entry, tp, sl):
		tracker.on_update(broker.update(o.broker_id, "canceled"))
	assert (entry.status, tp.status, sl.status) == ("canceled", "canceled", "canceled")
	assert tracker.working() == []


async def test_load_adopts_working_brackets(setup):
	broker, rec, _ = setup
	from alpacagw.client import OrderRequest
	ext = await broker.submit_order(OrderRequest.bracket("AMD", "sell", 2, take_profit=90, stop_loss=110,
		limit_price=100, client_order_id="old-1"))
	broker.fill(ext.id, 2, 100)
	tracker = OrderTracker(broker, on_order=rec.on_order, on_fill=rec.on_fill)
	await tracker.load()
	assert tracker.orders["old-1"].status == "filled"
	assert sorted(o.role for o in tracker.working()) == ["stop_loss", "take_profit"]
	assert rec.fills == []                                                  # fills before the start are not reported


async def test_oco_exits(setup):
	"""Exits of a position: Alpaca's order is the take profit, the stop loss its leg; both working."""
	broker, rec, tracker = setup
	tp = Order(id="x1", symbol="AAPL", side="sell", type="limit", qty=10, status="pending", role="take_profit",
		leg_ids=["x1.sl"], limit_price=110.0, tif="gtc")
	sl = Order(id="x1.sl", symbol="AAPL", side="sell", type="stop", qty=10, status="pending", role="stop_loss",
		parent_id="x1", stop_price=95.0, tif="gtc")
	assert await tracker.submit_oco(tp, sl, None) is None
	ao = broker.orders[tp.broker_id]
	assert ao.order_class == "oco" and ao.client_order_id == "x1" and float(ao.limit_price) == 110
	assert sl.broker_id == broker.legs[ao.id][0] and float(broker.orders[sl.broker_id].stop_price) == 95
	assert (tp.status, sl.status) == ("open", "open")


async def test_external_oco_adopted_with_its_roles(setup):
	broker, rec, tracker = setup
	from alpacagw.client import OrderRequest
	ext = await broker.submit_order(OrderRequest.oco("NVDA", "sell", 3, take_profit=150, stop_loss=120,
		client_order_id="web-oco"))
	for o in (ext, *ext.legs):
		tracker.on_update(broker.update(o.id, "new"))
	await asyncio.sleep(0.15)
	tp = tracker.orders["web-oco"]
	assert tp.role == "take_profit" and len(tp.leg_ids) == 1
	sl = tracker.orders[tp.leg_ids[0]]
	assert sl.role == "stop_loss" and sl.parent_id == "web-oco"
