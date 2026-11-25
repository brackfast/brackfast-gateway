"""The gateway end to end over its websocket, with a fake Alpaca behind it."""
import asyncio
import socket

import aiohttp
import pytest

from brackfastgw.alpaca import Quote as AlpacaQuote, Trade
from brackfastgw.gateway.core import GatewayCore
from brackfastgw.protocol import (
	PROTOCOL_VERSION, Bar, Bars, CancelOrder, GetBars, Hello, Order, Quote, Reply, Status, SubmitBracket, SubmitOco,
	Subscribe, Welcome,
	decode_gateway, encode,
)

from fake_broker import FakeAlpaca


def free_port() -> int:
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


class Conn:
	def __init__(self, ws):
		self.ws = ws
		self.got: list = []

	def send(self, msg) -> asyncio.Future:
		return self.ws.send_bytes(encode(msg))

	async def wait(self, pred, timeout: float = 2.0):
		async def look():
			i = 0
			while True:
				while i < len(self.got):
					if pred(self.got[i]):
						return self.got[i]
					i += 1
				msg = await self.ws.receive()
				if msg.type != aiohttp.WSMsgType.BINARY:
					raise ConnectionError(f"{msg.type} {msg.data} {msg.extra}")
				self.got.append(decode_gateway(msg.data))
		return await asyncio.wait_for(look(), timeout)


@pytest.fixture
async def gateway():
	alpaca = FakeAlpaca()
	core = GatewayCore(alpaca, port=free_port())
	await core.start()
	session = aiohttp.ClientSession()

	async def connect(protocol=PROTOCOL_VERSION) -> Conn:
		ws = await session.ws_connect(f"ws://127.0.0.1:{core.port}/")
		conn = Conn(ws)
		await conn.send(Hello(protocol=protocol, client="test"))
		return conn

	yield core, alpaca, connect
	await session.close()
	await core.stop()


async def test_hello_welcome_and_state(gateway):
	core, alpaca, connect = gateway
	conn = await connect()
	welcome = await conn.wait(lambda m: isinstance(m, Welcome))
	assert (welcome.environment, welcome.account, welcome.broker) == ("paper", "PA123", "alpaca")
	status = await conn.wait(lambda m: isinstance(m, Status))
	assert status.state == "ready" and status.trading_connected


async def test_refused(gateway):
	core, alpaca, connect = gateway
	conn = await connect(99)
	with pytest.raises(ConnectionError, match="1008"):          # policy violation
		await conn.wait(lambda m: True)


async def test_bracket_through_the_gateway(gateway):
	core, alpaca, connect = gateway
	conn = await connect()
	await conn.send(SubmitBracket(id="x1", symbol="aapl", side="buy", qty=10, limit_price=100.004, take_profit=110,
		stop_loss=95))
	reply = await conn.wait(lambda m: isinstance(m, Reply))
	assert reply.ok and reply.id == "x1"
	entry = await conn.wait(lambda m: isinstance(m, Order) and m.id == "x1" and m.status == "open")
	assert entry.limit_price == 100.0 and entry.symbol == "AAPL" and entry.origin == "test"
	await conn.wait(lambda m: isinstance(m, Order) and m.id == "x1.sl" and m.status == "held")

	await conn.send(SubmitBracket(id="x1", symbol="AAPL", side="buy", qty=10, take_profit=110, stop_loss=95))
	await conn.send(SubmitBracket(id="x2", symbol="AAPL", side="buy", qty=10, take_profit=90, stop_loss=95))
	await conn.send(SubmitBracket(id="x3", symbol="AAPL", side="sell", qty=1.5, take_profit=90, stop_loss=95))
	replies = []
	for id in ("x1", "x2", "x3"):
		replies.append(await conn.wait(lambda m, id=id: isinstance(m, Reply) and m.id == id and not m.ok))
	assert ["used" in replies[0].text, "above" in replies[1].text, "whole" in replies[2].text] == [True] * 3
	assert alpaca.trading.calls.count("submit x1") == 1


async def test_bars_and_live_data(gateway):
	core, alpaca, connect = gateway
	conn = await connect()
	await conn.send(GetBars(request_id=7, symbol="spy", timeframe="1m", limit=3))
	bars = await conn.wait(lambda m: isinstance(m, Bars))
	assert bars.request_id == 7 and len(bars.bars) == 3
	assert bars.bars[0].ts < bars.bars[-1].ts and bars.bars[-1].close == 14.5     # the newest 3, oldest first
	assert bars.name == "SPDR S&P 500 ETF Trust"                                 # the asset's name, for the chart
	await conn.send(GetBars(request_id=8, symbol="spy", timeframe="5m", limit=3))
	await conn.wait(lambda m: isinstance(m, Bars) and m.request_id == 8)
	assert alpaca.trading.calls.count("asset SPY") == 1                         # asked once per symbol

	await conn.send(Subscribe(symbols=["SPY"]))
	await asyncio.sleep(0.05)
	assert alpaca.stocks.subs["quotes"] == {"SPY"} and alpaca.stocks.subs["trades"] == {"SPY"}
	from datetime import datetime, timezone
	t0 = datetime(2026, 10, 28, 14, 31, 5, tzinfo=timezone.utc)
	alpaca.stocks.emit(Trade(symbol="SPY", timestamp=t0, price=500.0, size=10))
	alpaca.stocks.emit(Trade(symbol="SPY", timestamp=t0.replace(second=9), price=501.0, size=5))
	alpaca.stocks.emit(AlpacaQuote(symbol="SPY", timestamp=t0, bid_price=499.9, bid_size=1, ask_price=500.1, ask_size=2))
	quote = await conn.wait(lambda m: isinstance(m, Quote))
	assert quote.ask == 500.1
	bar = await conn.wait(lambda m: isinstance(m, Bar))
	assert (bar.open, bar.high, bar.close, bar.volume, bar.complete) == (500.0, 501.0, 501.0, 15.0, False)
	assert bar.ts == t0.replace(second=0)


async def test_forming_bar_prices_only_from_priced_trades():
	"""Odd lots and derivatively priced trades (conditions I, 4, ...) count in the volume but set no price (as
	Alpaca's bars); after Alpaca's bar for a minute, a late trade of it starts no forming bar again."""
	from datetime import datetime, timezone
	from brackfastgw.alpaca import Bar as AlpacaBar
	from brackfastgw.gateway.market import MarketRelay
	alpaca = FakeAlpaca()
	sent = []
	relay = MarketRelay(alpaca, publish=lambda kind, symbol, msg: sent.append(msg), quote_interval=0.01,
		bar_interval=0.01)
	await relay.start()
	relay.add(["NVDA"], quotes=False, bars=True)
	t0 = datetime(2026, 11, 19, 14, 37, 1, tzinfo=timezone.utc)
	alpaca.stocks.emit(Trade(symbol="NVDA", timestamp=t0, price=153.11, size=1, conditions=["@", "I"]))   # odd lot
	await asyncio.sleep(0.05)
	assert sent == []                                       # volume only: nothing to send yet
	alpaca.stocks.emit(Trade(symbol="NVDA", timestamp=t0.replace(second=2), price=157.10, size=100, conditions=["@"]))
	alpaca.stocks.emit(Trade(symbol="NVDA", timestamp=t0.replace(second=3), price=156.58, size=65,
		conditions=["@", "4", "I"]))
	alpaca.stocks.emit(Trade(symbol="NVDA", timestamp=t0.replace(second=4), price=157.20, size=200, conditions=["@", "F"]))
	await asyncio.sleep(0.05)
	bar = sent[-1]
	assert (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.complete) == (157.10, 157.20, 157.10, 157.20, 366.0,
		False)
	minute = t0.replace(second=0)
	alpaca.stocks.emit(AlpacaBar(symbol="NVDA", timestamp=minute, open=157.1, high=157.2, low=157.1, close=157.2,
		volume=366))
	alpaca.stocks.emit(Trade(symbol="NVDA", timestamp=t0.replace(second=59), price=150.0, size=100, conditions=["@"]))
	await asyncio.sleep(0.05)
	assert sent[-1].complete and sent[-1].low == 157.1     # the late trade sent no forming bar for the minute
	await relay.stop()


async def test_forming_bar_filled_with_the_minutes_earlier_trades():
	"""Subscribed mid-minute: the minute's trades before the subscription come from the trades API, so the forming
	bar has the minute's open, high and low; a trade both fetched and streamed counts once."""
	from datetime import datetime, timezone
	from brackfastgw.alpaca import TradeData
	from brackfastgw.gateway.market import MarketRelay
	alpaca = FakeAlpaca()
	t0 = datetime(2026, 11, 23, 14, 53, 0, tzinfo=timezone.utc)
	at = lambda s: t0.replace(second=s)
	both = TradeData(timestamp=at(31), price=151.0, size=10, id=3, exchange="Q", conditions=["@"])
	alpaca.data.trades["NVDA"] = [
		TradeData(timestamp=at(2), price=150.0, size=100, id=1, exchange="Q", conditions=["@"]),
		TradeData(timestamp=at(9), price=153.0, size=50, id=2, exchange="N", conditions=["@"]),
		TradeData(timestamp=at(15), price=140.0, size=5, id=9, exchange="D", conditions=["@", "I"]),   # odd lot
		both]
	sent = []
	relay = MarketRelay(alpaca, publish=lambda kind, symbol, msg: sent.append(msg), quote_interval=0.01,
		bar_interval=0.01, fill_delay=0.05, clock=lambda: at(32))
	await relay.start()
	relay.add(["NVDA"], quotes=False, bars=True)
	alpaca.stocks.emit(Trade(symbol="NVDA", timestamp=both.timestamp, price=151.0, size=10, id=3, exchange="Q",
		conditions=["@"]))
	await asyncio.sleep(0.03)
	assert sent[-1].open == 151.0                           # before the fill: from the subscription on
	await asyncio.sleep(0.08)
	params = alpaca.data.requests[-1]
	assert (params.symbols, params.start, params.end) == (["NVDA"], t0, at(32))
	bar = sent[-1]
	assert (bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.complete) == (t0, 150.0, 153.0, 150.0,
		151.0, 165.0, False)
	alpaca.stocks.emit(Trade(symbol="NVDA", timestamp=at(40), price=154.0, size=1, id=4, exchange="Q", conditions=["@"]))
	await asyncio.sleep(0.03)
	assert (sent[-1].open, sent[-1].high, sent[-1].volume) == (150.0, 154.0, 166.0)      # streaming goes on from there
	await relay.stop()


async def test_no_fill_once_the_minute_is_over():
	"""Alpaca's bar for the minute came before the trades did: it stays, the fetched trades are not used."""
	from datetime import datetime, timezone
	from brackfastgw.alpaca import Bar as AlpacaBar, TradeData
	from brackfastgw.gateway.market import MarketRelay
	alpaca = FakeAlpaca()
	t0 = datetime(2026, 11, 23, 14, 53, 0, tzinfo=timezone.utc)
	alpaca.data.trades["NVDA"] = [TradeData(timestamp=t0.replace(second=5), price=100.0, size=1, id=1, conditions=["@"])]
	sent = []
	relay = MarketRelay(alpaca, publish=lambda kind, symbol, msg: sent.append(msg), quote_interval=0.01,
		bar_interval=0.01, fill_delay=0.05, clock=lambda: t0.replace(second=59))
	await relay.start()
	relay.add(["NVDA"], quotes=False, bars=True)
	alpaca.stocks.emit(AlpacaBar(symbol="NVDA", timestamp=t0, open=1, high=2, low=1, close=2, volume=9))
	await asyncio.sleep(0.12)
	assert len(sent) == 1 and sent[0].complete and alpaca.data.requests
	await relay.stop()


def test_stop_from_another_thread_twice():
	"""The window's way: the gateway on its own loop in a thread; Quit stops it, and closing the window
	asks again once the loop is closed (that raised "Event loop is closed")."""
	import threading

	core = GatewayCore(FakeAlpaca(), port=free_port())
	failed = []

	def serve():
		try:
			asyncio.run(core.run())
		except BaseException as exc:
			failed.append(exc)

	thread = threading.Thread(target=serve, daemon=True)
	thread.start()
	for _ in range(100):
		if core.status.state == "ready":
			break
		threading.Event().wait(0.05)
	assert core.status.state == "ready"
	core.request_stop()                          # Quit
	thread.join(5)
	assert not thread.is_alive() and not failed
	core.request_stop()                          # the window closing afterwards: no error
	assert not any(e.level == "warning" and "disconnected" in e.text for e in core.events)


async def test_relay_intervals_are_independent():
	"""Quotes every 20 ms, the forming bar every 300 ms: after 100 ms quotes went out, the bar not yet."""
	from datetime import datetime, timezone
	from brackfastgw.gateway.market import MarketRelay
	alpaca = FakeAlpaca()
	sent = []
	relay = MarketRelay(alpaca, publish=lambda kind, symbol, msg: sent.append(kind), quote_interval=0.02,
		bar_interval=0.3)
	await relay.start()
	relay.add(["SPY"], quotes=True, bars=True)
	t0 = datetime(2026, 10, 28, 14, 31, 5, tzinfo=timezone.utc)
	await asyncio.sleep(0.05)                               # first flushes (nothing to send) done
	alpaca.stocks.emit(AlpacaQuote(symbol="SPY", timestamp=t0, bid_price=1, bid_size=1, ask_price=2, ask_size=1))
	alpaca.stocks.emit(Trade(symbol="SPY", timestamp=t0, price=1.5, size=10))
	await asyncio.sleep(0.1)
	assert "quote" in sent and "bar" not in sent
	await asyncio.sleep(0.35)
	assert "bar" in sent
	await relay.stop()


async def test_stop_entries(gateway):
	"""A breakout entry: stop (stop_price), stop limit (both); the limit may not be on the wrong side of the stop."""
	core, alpaca, connect = gateway
	conn = await connect()
	await conn.send(SubmitBracket(id="s1", symbol="AAPL", side="buy", qty=10, stop_price=101, take_profit=110,
		stop_loss=95))
	entry = await conn.wait(lambda m: isinstance(m, Order) and m.id == "s1" and m.status == "open")
	assert (entry.type, entry.stop_price, entry.limit_price) == ("stop", 101.0, None)
	await conn.send(SubmitBracket(id="s2", symbol="AAPL", side="sell", qty=10, stop_price=99, limit_price=98.95,
		take_profit=90, stop_loss=105))
	entry = await conn.wait(lambda m: isinstance(m, Order) and m.id == "s2" and m.status == "open")
	assert (entry.type, entry.stop_price, entry.limit_price) == ("stop_limit", 99.0, 98.95)
	await conn.send(SubmitBracket(id="s3", symbol="AAPL", side="buy", qty=10, stop_price=101, limit_price=100.9,
		take_profit=110, stop_loss=95))
	await conn.send(SubmitBracket(id="s4", symbol="AAPL", side="buy", qty=10, stop_price=111, take_profit=110,
		stop_loss=95))
	bad = [await conn.wait(lambda m, id=id: isinstance(m, Reply) and m.id == id and not m.ok) for id in ("s3", "s4")]
	assert "at or above" in bad[0].text and "between" in bad[1].text


async def test_oco_exits(gateway):
	"""Exits of a position: take profit id, stop loss id.sl; cancelling the stop loss cancels the take profit's OCO."""
	core, alpaca, connect = gateway
	conn = await connect()
	await conn.send(SubmitOco(id="e1", symbol="AAPL", side="sell", qty=10, take_profit=110, stop_loss=95))
	tp = await conn.wait(lambda m: isinstance(m, Order) and m.id == "e1" and m.status == "open")
	sl = await conn.wait(lambda m: isinstance(m, Order) and m.id == "e1.sl" and m.status == "open")
	assert (tp.role, tp.leg_ids, tp.limit_price) == ("take_profit", ["e1.sl"], 110.0)
	assert (sl.role, sl.parent_id, sl.stop_price, sl.side) == ("stop_loss", "e1", 95.0, "sell")
	assert (await conn.wait(lambda m: isinstance(m, Reply) and m.id == "e1")).ok
	await conn.send(CancelOrder(id="e1.sl"))
	await conn.wait(lambda m: isinstance(m, Reply) and m.command == "cancel_order" and m.id == "e1.sl" and m.ok)
	assert f"cancel {tp.broker_id}" in alpaca.trading.calls
	await conn.send(SubmitOco(id="e2", symbol="AAPL", side="sell", qty=10, take_profit=90, stop_loss=95))
	await conn.send(SubmitOco(id="e3", symbol="AAPL", side="buy", qty=10, take_profit=90, stop_loss=95,
		stop_loss_limit=94))
	bad = [await conn.wait(lambda m, id=id: isinstance(m, Reply) and m.id == id and not m.ok) for id in ("e2", "e3")]
	assert "above the stop loss" in bad[0].text and "at or above its stop" in bad[1].text
