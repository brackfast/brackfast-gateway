"""The bar cache: the same answers as the source, asked only for what is missing; a file per source."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from alpacagw.gateway.barcache import SETTLE, BarCache
from alpacagw.gateway.core import GatewayCore
from alpacagw.protocol import BarData, Bars, GetBars, Hello, PROTOCOL_VERSION

from fake_broker import FakeAlpaca

MINUTE = timedelta(minutes=1)
T0 = datetime(2026, 11, 2, tzinfo=timezone.utc)                  # a Monday
NOW = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)         # a Friday, the market open


class Source:
	"""1m bars on weekdays from 14:30 to 21:00 UTC, up to ``now``; ``factor`` scales the prices (a split)."""

	def __init__(self):
		self.now = NOW
		self.factor = 1.0
		self.calls: list[tuple[datetime, datetime, int]] = []

	def bar(self, ts: datetime) -> BarData:
		n = int((ts - T0) / MINUTE)
		price = (100 + n % 500 / 10) * self.factor
		return BarData(ts, price, price + 0.5, price - 0.5, price + 0.1, 1000 + n)

	def answer(self, start: datetime, end: datetime, limit: int) -> list[BarData]:
		out = []
		ts = min(end, self.now).replace(second=0, microsecond=0)
		while ts >= start and ts >= T0 and len(out) < limit:
			if ts.weekday() < 5 and 14 * 60 + 30 <= ts.hour * 60 + ts.minute < 21 * 60:
				out.append(self.bar(ts))
			ts -= MINUTE
		return out

	async def fetch(self, start: datetime, end: datetime, limit: int) -> list[BarData]:
		self.calls.append((start, end, limit))
		return self.answer(start, end, limit)


@pytest.fixture
def setup(tmp_path):
	source = Source()
	cache = BarCache(tmp_path, clock=lambda: source.now)
	yield source, cache
	cache.close()


async def get(cache, source, start, end, limit, name="alpaca-iex", symbol="SPY"):
	return await cache.get(name, symbol, "1m", MINUTE, start, end, limit, source.fetch, aligned=True)


async def test_the_second_request_asks_only_for_the_newest_bars(setup):
	source, cache = setup
	start = NOW - timedelta(days=5)
	first = await get(cache, source, start, NOW, 1000)
	assert first == source.answer(start, NOW, 1000) and len(source.calls) == 1
	source.calls.clear()
	source.now = NOW + timedelta(minutes=3)                  # three minutes later, the chart opened again
	again = await get(cache, source, start, source.now, 1000)
	assert again == source.answer(start, source.now, 1000)
	(lo, hi, limit), = source.calls                          # one request: from the last settled bar on
	assert source.now <= hi < source.now + MINUTE and lo >= NOW - MINUTE - SETTLE - MINUTE and limit == 1001
	assert cache.from_cache > 900


async def test_nothing_is_asked_for_settled_history(setup):
	source, cache = setup
	start, end = NOW - timedelta(days=9), NOW - timedelta(days=2)
	await get(cache, source, start, end, 50_000)             # everything of that week, also its empty weekend
	source.calls.clear()
	inside = await get(cache, source, start + timedelta(days=1), end - timedelta(days=1), 300)
	assert inside == source.answer(start + timedelta(days=1), end - timedelta(days=1), 300) and source.calls == []
	weekend = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
	assert await get(cache, source, weekend, weekend + timedelta(hours=20), 100) == [] and source.calls == []


async def test_older_bars_are_one_more_request(setup):
	source, cache = setup
	end = NOW - timedelta(days=2)
	newest = await get(cache, source, T0, end, 500)          # the newest 500 up to end: covered from the oldest of them
	source.calls.clear()
	older = await get(cache, source, T0, newest[-1].ts - MINUTE, 500)        # the chart scrolled back
	assert older == source.answer(T0, newest[-1].ts - MINUTE, 500) and len(source.calls) == 1
	source.calls.clear()
	both = await get(cache, source, T0, end, 1000)
	assert both == newest + older and source.calls == []


async def test_random_requests_answer_as_the_source(setup):
	source, cache = setup
	rng = random.Random(7)
	for _ in range(150):
		source.now = NOW + timedelta(minutes=rng.randrange(0, 600))
		end = source.now - timedelta(minutes=rng.choice([0, 0, 5, 30, 2000, 9000, 20000]))
		start = end - timedelta(minutes=rng.choice([10, 300, 3000, 30000]))
		limit = rng.choice([1, 7, 100, 1000, 50_000])
		assert await get(cache, source, start, end, limit) == source.answer(start, end, limit), (start, end, limit)


async def test_a_split_drops_the_symbols_cache(setup):
	source, cache = setup
	start = NOW - timedelta(days=3)
	await get(cache, source, start, NOW, 2000)
	source.now = NOW + timedelta(hours=1)
	source.factor = 0.5                                      # 2 for 1: the source's whole history at half the price
	bars = await get(cache, source, start, source.now, 2000)
	assert bars == source.answer(start, source.now, 2000)    # no bar at the old prices among them
	source.calls.clear()
	assert await get(cache, source, start, NOW - timedelta(hours=1), 2000) == source.answer(start, NOW - timedelta(hours=1), 2000)
	assert source.calls == []                                # and kept again, at the new prices


async def test_a_file_per_source_and_clear(setup, tmp_path):
	source, cache = setup
	start, end = NOW - timedelta(days=4), NOW - timedelta(days=1)
	await get(cache, source, start, end, 500, name="alpaca-iex")
	await get(cache, source, start, end, 500, name="alpaca-sip")
	assert sorted(p.name for p in tmp_path.glob("*.sqlite")) == ["alpaca-iex.sqlite", "alpaca-sip.sqlite"]
	assert len(source.calls) == 2 and cache.size("alpaca-iex") > 0 and cache.size("massive") == 0
	cache.clear("alpaca-iex")
	source.calls.clear()
	await get(cache, source, start, end, 500, name="alpaca-sip")
	assert source.calls == []                                # the other source's bars are still there
	await get(cache, source, start, end, 500, name="alpaca-iex")
	assert len(source.calls) == 1


async def test_survives_a_restart_a_damaged_file_and_its_size_limit(tmp_path):
	source = Source()
	start, end = NOW - timedelta(days=4), NOW - timedelta(days=1)
	cache = BarCache(tmp_path, clock=lambda: source.now)
	for symbol in ("AAA", "BBB", "CCC"):
		await get(cache, source, T0, end, 50_000, symbol=symbol)
	cache.close()

	cache = BarCache(tmp_path, clock=lambda: source.now)     # a new run: still there
	source.calls.clear()
	assert await get(cache, source, start, end, 500, symbol="CCC") == source.answer(start, end, 500) and source.calls == []
	cache.close()

	cache = BarCache(tmp_path, max_mb=0, clock=lambda: source.now)     # above its size: everything goes when opened
	assert await get(cache, source, start, end, 500, symbol="CCC") == source.answer(start, end, 500) and len(source.calls) == 1
	cache.close()

	cache.file("alpaca-iex").write_bytes(b"not a database" * 100)
	for extra in ("-wal", "-shm"):
		cache.file("alpaca-iex").with_name("alpaca-iex.sqlite" + extra).unlink(missing_ok=True)
	cache = BarCache(tmp_path, clock=lambda: source.now)
	assert await get(cache, source, start, end, 500) == source.answer(start, end, 500)      # a new file, the bars come
	cache.close()


async def test_through_the_gateway(tmp_path):
	import aiohttp
	from test_gateway import Conn, free_port
	alpaca = FakeAlpaca()
	core = GatewayCore(alpaca, port=free_port(), bar_cache=BarCache(tmp_path))
	await core.start()
	async with aiohttp.ClientSession() as session:
		conn = Conn(await session.ws_connect(f"ws://127.0.0.1:{core.port}/"))
		await conn.send(Hello(protocol=PROTOCOL_VERSION, client="test"))
		end = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
		answers = []
		for request_id in (1, 2):
			await conn.send(GetBars(request_id=request_id, symbol="SPY", timeframe="1m", end=end, limit=3))
			answers.append(await conn.wait(lambda m: isinstance(m, Bars) and m.request_id == request_id))
		assert answers[0].bars == answers[1].bars and len(answers[0].bars) == 3 and answers[0].bars[-1].close == 14.5
		asked = [r for r in alpaca.data.requests if not isinstance(r, tuple)]
		assert len(asked) == 1                               # the second answer came from the file
		assert (tmp_path / "alpaca-iex.sqlite").is_file()
	await core.stop()
