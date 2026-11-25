"""The simulated account: fills from the quotes, brackets and OCOs, the account, the file; and through the gateway."""
import asyncio
from datetime import datetime, timezone

import aiohttp
import pytest

from alpacagw.client import Quote as AlpacaQuote
from alpacagw.client.models.market import QuoteData, TradeData
from alpacagw.gateway.core import GatewayCore
from alpacagw.gateway.sim import SimBroker, Simulation, day_end
from alpacagw.protocol import (
	PROTOCOL_VERSION, Account, CancelOrder, ClosePosition, Fill, FlattenAll, Hello, Order, Position, Quote, Reply,
	SubmitBracket, SubmitOco, Welcome,
)

from fake_broker import FakeAlpaca
from test_gateway import Conn, free_port

OPEN = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)        # Tuesday 10:00 New York
EVENING = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)      # Tuesday 20:00 New York
CLOSE = datetime(2026, 11, 24, 21, 0, tzinfo=timezone.utc)       # Tuesday 16:00 New York


class Feed:
	def __init__(self):
		self.subscribed: list[str] = []
		self.latest: Quote | None = None

	def add(self, symbols, *, quotes, bars):
		self.subscribed += symbols

	def remove(self, symbols, *, quotes, bars):
		for s in symbols:
			self.subscribed.remove(s)

	async def latest_quote(self, symbol):
		return self.latest


class Sim:
	"""A SimBroker with a clock to set, a feed to look at, and what it reported."""

	def __init__(self, **kw):
		self.now = OPEN
		self.feed = Feed()
		self.changes: list[tuple[str, str]] = []
		self.fills: list[Fill] = []
		self.broker = SimBroker(self.feed, on_order=lambda o: self.changes.append((o.id, o.status)),
			on_fill=lambda o, f: self.fills.append(f), clock=lambda: self.now, **kw)

	def quote(self, bid, ask, symbol="SPY"):
		self.broker.on_quote(Quote(symbol=symbol, ts=self.now, bid=bid, bid_size=1, ask=ask, ask_size=1))

	def status(self, *ids):
		return [self.broker.orders[i].status for i in ids]

	async def bracket(self, id="b", side="buy", qty=10, limit=None, stop=None, tp=110.0, sl=95.0, tif="gtc"):
		exit_side = "sell" if side == "buy" else "buy"
		kind = ("stop_limit" if limit else "stop") if stop else ("limit" if limit else "market")
		entry = Order(id=id, symbol="SPY", side=side, type=kind, qty=qty, status="pending", role="entry",
			leg_ids=[f"{id}.tp", f"{id}.sl"], limit_price=limit, stop_price=stop, tif=tif, created_at=self.now)
		take = Order(id=f"{id}.tp", symbol="SPY", side=exit_side, type="limit", qty=qty, status="pending",
			role="take_profit", parent_id=id, limit_price=tp, tif=tif, created_at=self.now)
		stop_loss = Order(id=f"{id}.sl", symbol="SPY", side=exit_side, type="stop", qty=qty, status="pending",
			role="stop_loss", parent_id=id, stop_price=sl, tif=tif, created_at=self.now)
		return await self.broker.submit_bracket(entry, take, stop_loss, None)

	async def oco(self, id="x", side="sell", qty=10, tp=110.0, sl=95.0):
		take = Order(id=id, symbol="SPY", side=side, type="limit", qty=qty, status="pending", role="take_profit",
			leg_ids=[f"{id}.sl"], limit_price=tp, tif="gtc", created_at=self.now)
		stop_loss = Order(id=f"{id}.sl", symbol="SPY", side=side, type="stop", qty=qty, status="pending",
			role="stop_loss", parent_id=id, stop_price=sl, tif="gtc", created_at=self.now)
		return await self.broker.submit_oco(take, stop_loss, None)

	async def order(self, id, side, qty, type="market", limit=None, stop=None, tif="day", extended=False):
		o = Order(id=id, symbol="SPY", side=side, type=type, qty=qty, status="pending", limit_price=limit,
			stop_price=stop, tif=tif, created_at=self.now)
		return await self.broker.submit_order(o, extended)


@pytest.fixture
async def sim():
	s = Sim()
	await s.broker.start()
	yield s
	await s.broker.close()


async def test_market_bracket_fills_at_the_ask_then_the_take_profit(sim):
	assert await sim.bracket() is None
	assert sim.status("b", "b.tp", "b.sl") == ["open", "held", "held"] and sim.feed.subscribed == ["SPY"]
	sim.quote(99.9, 100.1)
	assert sim.status("b", "b.tp", "b.sl") == ["filled", "open", "open"]
	assert (sim.fills[0].price, sim.fills[0].qty, sim.fills[0].role) == (100.1, 10, "entry")
	positions = await sim.broker.positions()
	assert (positions["SPY"].qty, positions["SPY"].avg_price, positions["SPY"].unrealized_pl) == (10, 100.1, -1.0)
	sim.quote(109.0, 109.2)                              # below the take profit: nothing
	assert len(sim.fills) == 1
	sim.quote(110.3, 110.5)                              # through it: at the limit, not at the better bid
	assert sim.status("b.tp", "b.sl") == ["filled", "canceled"] and sim.fills[1].price == 110.0
	assert await sim.broker.positions() == {} and sim.feed.subscribed == []
	account = await sim.broker.account()
	assert (account.account, account.cash, account.equity) == ("SIMULATED", 100_099.0, 100_099.0)


async def test_limit_and_stop_entries(sim):
	await sim.bracket("l", limit=100.0)
	sim.quote(100.4, 100.6)
	assert sim.status("l") == ["open"]                  # the ask is above the limit
	sim.quote(99.7, 99.9)
	assert sim.status("l") == ["filled"] and sim.fills[-1].price == 100.0     # resting: at its limit
	await sim.bracket("m", limit=101.0, tp=120.0)       # marketable when it arrives: at the ask
	assert sim.fills[-1].price == 99.9
	await sim.bracket("s", stop=102.0, tp=120.0)
	assert sim.status("s") == ["open"]
	sim.quote(102.3, 102.5)                              # a gap over the stop: at the ask
	assert sim.status("s") == ["filled"] and sim.fills[-1].price == 102.5
	await sim.bracket("sl", stop=103.0, limit=103.2, tp=120.0)
	sim.quote(103.4, 103.6)                              # triggered, but the ask is above its limit
	assert sim.status("sl") == ["open"]
	sim.quote(102.9, 103.1)
	assert sim.status("sl") == ["filled"] and sim.fills[-1].price == 103.2    # a limit order by then: at its limit


async def test_short_bracket_stopped_out(sim):
	await sim.bracket(side="sell", tp=90.0, sl=105.0)
	sim.quote(99.9, 100.1)
	assert sim.fills[0].price == 99.9 and (await sim.broker.positions())["SPY"].qty == -10
	sim.quote(105.2, 105.4)                              # the stop loss buys at the ask
	assert sim.status("b.tp", "b.sl") == ["canceled", "filled"] and sim.fills[1].price == 105.4
	assert (await sim.broker.account()).cash == 100_000 - 55.0


async def test_cancel_replace(sim):
	await sim.bracket(limit=100.0)
	sim.quote(100.4, 100.6)
	assert await sim.broker.cancel(sim.broker.orders["b.tp"]) == "a leg of a working entry: cancel the entry"
	assert await sim.broker.replace(sim.broker.orders["b.tp"], qty=None, limit_price=112.0, stop_price=None) is None
	assert await sim.broker.replace(sim.broker.orders["b"], qty=None, limit_price=100.7, stop_price=None) is None
	assert sim.status("b") == ["filled"] and sim.fills[0].price == 100.6      # moved through the ask: at the ask
	sim.quote(111.0, 111.2)
	assert sim.status("b.tp") == ["open"]                # the replaced take profit, 112
	assert await sim.broker.cancel(sim.broker.orders["b.tp"]) is None
	assert sim.status("b.tp", "b.sl") == ["canceled", "canceled"]
	assert await sim.broker.cancel(sim.broker.orders["b.sl"]) is None         # went with its partner: no problem
	await sim.bracket("c", limit=90.0)
	assert await sim.broker.cancel(sim.broker.orders["c"]) is None
	assert sim.status("c", "c.tp", "c.sl") == ["canceled"] * 3


async def test_oco_needs_the_shares(sim):
	assert "available: 0" in await sim.oco("none")
	assert sim.status("none", "none.sl") == ["rejected", "rejected"]
	await sim.order("buy", "buy", 10)
	sim.quote(99.9, 100.1)
	assert await sim.oco("x", qty=6) is None
	assert "requested: 6, available: 4" in await sim.oco("y", qty=6)
	assert await sim.oco("z", qty=4) is None
	sim.quote(94.8, 95.0)                                # both stop losses, at the bid
	assert sim.status("x", "x.sl", "z", "z.sl") == ["canceled", "filled", "canceled", "filled"]
	assert await sim.broker.positions() == {}


async def test_regular_hours_and_expiry(sim):
	sim.now = EVENING
	await sim.order("m", "buy", 5)
	await sim.order("e", "buy", 5, type="limit", limit=101.0, extended=True)
	sim.quote(99.9, 100.1)
	assert sim.status("m", "e") == ["open", "open"]      # 20:00: the market is closed, extended hours over
	sim.now = datetime(2026, 11, 25, 13, 0, tzinfo=timezone.utc)       # Wednesday 08:00 New York
	await sim.broker.poll()
	assert sim.status("m", "e") == ["open", "filled"]    # extended hours: from the quote there is
	sim.now = datetime(2026, 11, 25, 14, 30, tzinfo=timezone.utc)      # 09:30
	await sim.broker.poll()
	assert sim.status("m") == ["filled"]
	await sim.order("d", "buy", 5, type="limit", limit=50.0)
	await sim.order("i", "buy", 5, type="limit", limit=50.0, tif="ioc")
	assert sim.status("d", "i") == ["open", "canceled"]
	sim.now = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)       # 16:00
	await sim.broker.poll()
	assert sim.status("d") == ["expired"]
	assert day_end(EVENING) == datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)
	assert day_end(datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 30, 21, 0, tzinfo=timezone.utc)
	assert "not simulated" in await sim.order("o", "buy", 5, tif="opg")


async def test_all_hours_and_the_latest_quote():
	s = Sim(all_hours=True)
	s.now = EVENING
	s.feed.latest = Quote(symbol="SPY", ts=CLOSE, bid=99.9, bid_size=1, ask=100.1, ask_size=1)
	await s.broker.start()
	await s.order("m", "buy", 5)
	await asyncio.sleep(0.01)                            # the latest quote was asked for: no stream quote needed
	assert s.status("m") == ["filled"] and s.fills[0].price == 100.1


async def test_average_price_through_zero_and_the_day(sim):
	await sim.order("a", "buy", 10)
	sim.quote(100.0, 100.0)
	sim.quote(110.0, 110.0)
	await sim.order("b", "buy", 10)                      # at once: the quote is known
	assert (await sim.broker.positions())["SPY"].avg_price == 105.0
	await sim.order("c", "sell", 30)                     # through zero: 10 short at 110
	p = (await sim.broker.positions())["SPY"]
	assert (p.qty, p.avg_price) == (-10, 110.0)
	account = await sim.broker.account()
	assert (account.equity, account.last_equity) == (100_100.0, 100_000.0)
	sim.now = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)       # the next day: its base is that equity
	assert (await sim.broker.account()).last_equity == 100_100.0


async def test_kept_in_the_file_and_reset(tmp_path):
	file = tmp_path / "sim.json"
	s = Sim(file=file)
	await s.broker.start()
	await s.bracket()
	s.quote(99.9, 100.1)
	await s.bracket("w", limit=90.0)
	await s.broker.close()

	s = Sim(file=file)
	await s.broker.start()
	assert s.feed.subscribed == ["SPY"]
	assert s.status("b", "b.tp", "b.sl", "w", "w.sl") == ["filled", "open", "open", "open", "held"]
	assert (await s.broker.positions())["SPY"].qty == 10 and (await s.broker.account()).cash == 100_000 - 1001.0
	s.quote(110.0, 110.2)                                # the take profit of before the restart
	assert s.status("b.tp", "b.sl") == ["filled", "canceled"]
	await s.broker.reset()
	assert s.status("w", "w.tp") == ["canceled", "canceled"] and s.feed.subscribed == []
	assert (await s.broker.account()).cash == 100_000.0
	await s.broker.close()

	file.write_text("not json")
	s = Sim(file=file)
	await s.broker.start()                               # a new account, the file kept aside
	assert s.broker.orders == {} and (tmp_path / "sim.json.unreadable").read_text() == "not json"


@pytest.fixture
async def gateway(tmp_path):
	alpaca = FakeAlpaca()
	core = GatewayCore(alpaca, port=free_port(), quote_interval=0.01, refresh=0.2,
		simulation=Simulation(file=tmp_path / "sim.json", all_hours=True))     # the test runs at any time of day
	await core.start()
	session = aiohttp.ClientSession()
	conn = Conn(await session.ws_connect(f"ws://127.0.0.1:{core.port}/"))
	await conn.send(Hello(protocol=PROTOCOL_VERSION, client="test"))
	yield core, alpaca, conn
	await session.close()
	await core.stop()


async def test_latest_quote_of_a_closed_market(gateway):
	core, alpaca, conn = gateway
	alpaca.data.latest = {"SPY": QuoteData(timestamp=CLOSE, bid_price=693.63, bid_size=40, ask_price=736.61, ask_size=40)}
	alpaca.data.latest_trades = {"SPY": TradeData(timestamp=CLOSE, price=714.2, size=100)}
	quote = await core.market.latest_quote("SPY")       # a spread of 43: the last trade instead
	assert (quote.bid, quote.ask) == (714.2, 714.2)
	alpaca.data.latest = {"SPY": QuoteData(timestamp=CLOSE, bid_price=714.1, bid_size=40, ask_price=714.3, ask_size=40)}
	quote = await core.market.latest_quote("SPY")
	assert (quote.bid, quote.ask) == (714.1, 714.3)
	assert await core.market.latest_quote("NONE") is None


def _quote(alpaca, bid, ask):
	alpaca.stocks.emit(AlpacaQuote(symbol="SPY", timestamp=OPEN, bid_price=bid, bid_size=1, ask_price=ask, ask_size=1))


async def test_through_the_gateway(gateway):
	core, alpaca, conn = gateway
	welcome = await conn.wait(lambda m: isinstance(m, Welcome))
	assert (welcome.broker, welcome.environment, welcome.account) == ("simulated", "paper", "SIMULATED")
	account = await conn.wait(lambda m: isinstance(m, Account))
	assert account.equity == 100_000.0

	await conn.send(SubmitBracket(id="x1", symbol="spy", side="buy", qty=10, take_profit=110, stop_loss=95))
	assert (await conn.wait(lambda m: isinstance(m, Reply) and m.id == "x1")).ok
	assert alpaca.stocks.subs["quotes"] == {"SPY"}       # the simulator's own subscription: the client has none
	_quote(alpaca, 99.9, 100.1)
	fill = await conn.wait(lambda m: isinstance(m, Fill))
	assert (fill.order_id, fill.price) == ("x1", 100.1)
	await conn.wait(lambda m: isinstance(m, Order) and m.id == "x1.sl" and m.status == "open")
	position = await conn.wait(lambda m: isinstance(m, Position))
	assert (position.symbol, position.qty, position.avg_price) == ("SPY", 10, 100.1)
	assert not any(c.startswith("submit") for c in alpaca.trading.calls)      # nothing went to Alpaca

	await conn.send(CancelOrder(id="x1.tp"))             # one exit: both
	await conn.wait(lambda m: isinstance(m, Order) and m.id == "x1.sl" and m.status == "canceled")
	await conn.send(SubmitOco(id="o1", symbol="SPY", side="sell", qty=10, take_profit=111, stop_loss=96))
	assert (await conn.wait(lambda m: isinstance(m, Reply) and m.id == "o1")).ok
	await conn.send(ClosePosition(symbol="SPY"))         # cancels the exits, sells at the bid
	await conn.wait(lambda m: isinstance(m, Order) and m.id == "o1.sl" and m.status == "canceled")
	closed = await conn.wait(lambda m: isinstance(m, Fill) and m.side == "sell")
	assert closed.price == 99.9
	await conn.wait(lambda m: isinstance(m, Position) and m.qty == 0)
	account = await conn.wait(lambda m: isinstance(m, Account) and m.cash == 99_998.0)
	assert account.equity == 99_998.0

	await conn.send(SubmitBracket(id="x2", symbol="SPY", side="sell", qty=5, take_profit=90, stop_loss=105))
	assert (await conn.wait(lambda m: isinstance(m, Reply) and m.id == "x2")).ok
	_quote(alpaca, 99.8, 100.0)
	await conn.wait(lambda m: isinstance(m, Fill) and m.order_id == "x2")
	await conn.send(FlattenAll())
	assert (await conn.wait(lambda m: isinstance(m, Reply) and m.command == "flatten_all")).ok
	await conn.wait(lambda m: isinstance(m, Order) and m.id == "x2.tp" and m.status == "canceled")
	await conn.wait(lambda m: isinstance(m, Fill) and m.side == "buy" and m.role == "simple")
	await asyncio.sleep(0.5)                             # the positions' refresh after the fill
	assert core.broker.pos == {} and core.positions == {} and alpaca.stocks.subs["quotes"] == set()
