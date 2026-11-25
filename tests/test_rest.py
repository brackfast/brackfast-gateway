import msgspec
import pytest
from aiohttp import web

from brackfastgw.alpaca import AlpacaError, BarsParams, MarketDataClient, OrderRequest, TradingClient

from fake_alpaca import CREDENTIALS, KEY, FakeAlpaca

ORDER = {"id": "o1", "client_order_id": "c1", "symbol": "AAPL", "status": "new", "side": "buy", "type": "market",
	"time_in_force": "day", "created_at": "2026-10-28T14:30:00Z", "qty": "1"}


@pytest.fixture
async def fake():
	fake = FakeAlpaca()
	yield fake
	await fake.close()


def _client(cls, endpoints, **kw):
	return cls(CREDENTIALS, endpoints, max_retries=2, **kw)


async def test_post_is_retried_on_429_only(fake, monkeypatch):
	calls = []

	async def orders(request: web.Request):
		calls.append(await request.json())
		assert request.headers["APCA-API-KEY-ID"] == KEY
		if len(calls) == 1:
			return web.json_response({"message": "rate limit"}, status=429, headers={"Retry-After": "0"})
		if len(calls) == 2:
			return web.json_response({"code": 40010001, "message": "server trouble"}, status=500)
		return web.json_response(ORDER)

	fake.app.router.add_post("/v2/orders", orders)
	client = _client(TradingClient, await fake.start())
	with pytest.raises(AlpacaError) as err:
		await client.submit_order(OrderRequest.market("AAPL", "buy", 1))
	assert len(calls) == 2    # 429 retried, the 500 of a POST not (the order may exist)
	assert err.value.status == 500 and err.value.code == 40010001
	assert calls[0]["symbol"] == "AAPL"
	await client.close()


async def test_get_is_retried_on_5xx(fake, monkeypatch):
	monkeypatch.setattr(TradingClient, "_backoff", staticmethod(lambda attempt: 0))
	calls = []

	async def order(request: web.Request):
		calls.append(request.query.get("nested"))
		if len(calls) < 3:
			return web.Response(status=503, text="unavailable")
		return web.json_response(ORDER)

	fake.app.router.add_get("/v2/orders/{id}", order)
	client = _client(TradingClient, await fake.start())
	order = await client.get_order("o1", nested=True)
	assert order.id == "o1" and calls == ["true"] * 3
	await client.close()


async def test_bars_are_merged_over_pages(fake):
	seen = []

	async def bars(request: web.Request):
		seen.append(dict(request.query))
		bar = {"t": "2026-10-28T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}
		if "page_token" not in request.query:
			return web.json_response({"bars": {"AAPL": [bar]}, "next_page_token": "p2"})
		return web.json_response({"bars": {"AAPL": [bar], "MSFT": [bar]}, "next_page_token": None})

	fake.app.router.add_get("/v2/stocks/bars", bars)
	client = _client(MarketDataClient, await fake.start(), feed="iex")
	out = await client.get_bars(BarsParams(symbols=["AAPL", "MSFT"], timeframe="1Min"))
	assert {s: len(b) for s, b in out.items()} == {"AAPL": 2, "MSFT": 1}
	assert seen[0]["feed"] == "iex" and seen[1]["page_token"] == "p2"
	assert out["AAPL"][0].close == 1.5
	await client.close()
