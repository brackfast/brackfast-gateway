import asyncio

import pytest

from brackfastgw.alpaca import Bar, Credentials, Quote, StockDataStream, StreamError, TradeUpdate, TradingStream

from fake_alpaca import CREDENTIALS, FakeAlpaca
from test_models import ORDER

BAR = {"T": "b", "S": "AAPL", "t": "2026-10-28T13:30:00Z", "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100}


@pytest.fixture
async def fake():
	fake = FakeAlpaca()
	yield fake
	await fake.close()


async def test_stock_stream_subscribes_and_dispatches(fake):
	endpoints = await fake.start()
	fake.stock_frames = [[
		BAR,
		{"T": "b", **{k: v for k, v in BAR.items() if k not in ("T", "S")}, "S": "MSFT"},
		{"T": "new_thing", "S": "AAPL"},                 # unknown type: skipped, the rest still arrives
		{"T": "q", "S": "AAPL", "t": "2026-10-28T13:30:00Z", "bp": 1.0, "bs": 1, "ap": 1.1, "as": 2},
	]]
	stream = StockDataStream(CREDENTIALS, endpoints=endpoints)
	bars, quotes = [], []
	stream.on(Bar, bars.append, symbols=["AAPL"])       # MSFT is not delivered to this handler

	@stream.on(Quote, symbols="AAPL")
	async def on_quote(q):
		quotes.append(q)

	await stream.start()
	await asyncio.sleep(0.2)
	await stream.drain()
	assert [b.symbol for b in bars] == ["AAPL"]
	assert quotes[0].spread == pytest.approx(0.1)
	assert fake.received[1] == {"action": "subscribe", "bars": ["AAPL"], "quotes": ["AAPL"]}
	assert stream.confirmed.bars == ["AAPL"]

	stream.subscribe(Bar, "MSFT")
	await asyncio.sleep(0.1)
	assert fake.received[-1] == {"action": "subscribe", "bars": ["MSFT"]}
	await stream.stop()


async def test_stock_stream_reconnects_and_resubscribes(fake):
	endpoints = await fake.start()
	stream = StockDataStream(CREDENTIALS, endpoints=endpoints, min_backoff=0.05)
	stream.on(Bar, lambda b: None, symbols=["AAPL"])
	await stream.start()
	await asyncio.sleep(0.1)
	await stream._ws.close()                             # the connection drops
	for _ in range(50):
		await asyncio.sleep(0.05)
		if fake.stock_connections == 2 and stream.is_ready:
			break
	await asyncio.sleep(0.1)
	assert fake.stock_connections == 2
	assert [m for m in fake.received if m["action"] == "subscribe"] == [{"action": "subscribe", "bars": ["AAPL"]}] * 2
	await stream.stop()


async def test_stock_stream_auth_failure_is_fatal(fake):
	endpoints = await fake.start()
	stream = StockDataStream(Credentials("wrong", "key"), endpoints=endpoints, min_backoff=0.05)
	with pytest.raises(StreamError) as err:
		await stream.start(timeout=5)
	assert err.value.code == 402
	assert fake.stock_connections == 1
	await stream.stop()


async def test_trading_stream(fake):
	endpoints = await fake.start()
	fake.trading_frames = [
		{"stream": "trade_updates", "data": {"event": "new", "order": ORDER}},
		{"stream": "trade_updates", "data": {"event": "fill", "order": ORDER, "price": "198.19", "qty": "10"}},
	]
	stream = TradingStream(CREDENTIALS, endpoints=endpoints)
	events, fills = [], []

	async def slow(u: TradeUpdate):
		await asyncio.sleep(0.05 if u.event == "new" else 0)    # order is kept anyway
		events.append(u.event)

	stream.on_trade_update(slow)
	stream.on_trade_update(fills.append, events="fill")
	await stream.start()
	await asyncio.sleep(0.2)
	await stream.drain()
	assert events == ["new", "fill"]
	assert [str(u.price) for u in fills] == ["198.19"]
	assert fake.received[1] == {"action": "listen", "data": {"streams": ["trade_updates"]}}
	await stream.stop()


async def test_trading_stream_unauthorized(fake):
	endpoints = await fake.start()
	stream = TradingStream(Credentials("wrong", "key"), endpoints=endpoints)
	with pytest.raises(StreamError):
		await stream.start(timeout=5)
	await stream.stop()
