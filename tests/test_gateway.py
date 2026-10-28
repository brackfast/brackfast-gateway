"""The gateway end to end over its websocket, with a fake Alpaca behind it."""
import asyncio
import socket

import aiohttp
import pytest

from alpacagw.client import Quote as AlpacaQuote, Trade
from alpacagw.gateway.core import GatewayCore
from alpacagw.protocol import (
	PROTOCOL_VERSION, Bar, Bars, GetBars, Hello, Order, Quote, Reply, Status, SubmitBracket, Subscribe, Welcome,
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
	core = GatewayCore(alpaca, port=free_port(), token="t0k")
	await core.start()
	session = aiohttp.ClientSession()

	async def connect(token="t0k", protocol=PROTOCOL_VERSION) -> Conn:
		ws = await session.ws_connect(f"ws://127.0.0.1:{core.port}/")
		conn = Conn(ws)
		await conn.send(Hello(protocol=protocol, client="test", token=token))
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


@pytest.mark.parametrize("token,protocol", [("wrong", PROTOCOL_VERSION), ("t0k", 99)])
async def test_refused(gateway, token, protocol):
	core, alpaca, connect = gateway
	conn = await connect(token, protocol)
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
