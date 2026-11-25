"""Yahoo Finance as the data side: the chart answers' bars, the stream's messages, live bars, and the gateway on it."""
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp

from brackfastgw.gateway.core import GatewayCore
from brackfastgw.gateway.yahoo import Tick, YahooError, YahooRelay, bars_of, hourly, tick_of
from brackfastgw.protocol import (
	PROTOCOL_VERSION, Account, Bar, Bars, Fill, GetBars, Hello, Quote, Reply, SubmitBracket, Subscribe, Welcome,
)

from test_gateway import Conn, free_port

UTC = timezone.utc
NOW = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)          # Tuesday 13:00 New York

# two messages of the price stream as they came (2026-11-25, 08:00 UTC): SPY overnight, BTC-USD
SPY_MESSAGE = '{"type":"pricing","message":"CgNTUFkVuN4xRBjQ/MCbwmgqA1BDWDAUOARFuf+Bv2UAmunA2AEE9QLA9SzB/QKGkb+/"}'
BTC_MESSAGE = ("CgdCVEMtVVNEFTERZUcY0PzAm8JoIgNVU0QqA0NDQzApOAFF3R42wEiAgLaw6gFVp2FpR10YKWVHZeCy1sR9p2FpR7ABgIC2sOoB2AEE4AGA"
	"gLaw6gHoAYCAtrDqAfIBA0JUQ4ECAAAAAGcvc0GJAgAAGH97LHFC9QLgstbE/QLdHjbA")


def ts(*a) -> int:
	return int(datetime(*a, tzinfo=UTC).timestamp())


def result(rows, name="SPDR S&P 500"):
	"""A chart answer's result from (ts, open, high, low, close, volume) rows."""
	cols = list(zip(*rows)) if rows else [[]] * 6
	return {"meta": {"longName": name, "regularMarketPrice": 500.5, "regularMarketTime": ts(2026, 11, 24, 17, 59)},
		"timestamp": list(cols[0]), "indicators": {"quote": [dict(zip(("open", "high", "low", "close", "volume"),
			(list(c) for c in cols[1:])))]}}


def test_bars_of_a_chart_answer():
	rows = [(ts(2026, 11, 24, 14, 30), 10, 11, 9, 10.5, 100), (ts(2026, 11, 24, 14, 31), None, None, None, None, None),
		(ts(2026, 11, 24, 14, 32), 10.5, 12, 10, 11.5, 300), (ts(2026, 11, 24, 14, 32), 10.5, 12, 10, 11.5, 300),
		(ts(2026, 11, 24, 14, 32, 40), 11.7, 11.7, 11.7, 11.7, 0)]     # no price; a row twice; the price now, off the grid
	bars = bars_of(result(rows), "1m")
	assert [(b.ts.minute, b.open, b.high, b.low, b.close, b.volume) for b in bars] == [(30, 10, 11, 9, 10.5, 100),
		(32, 10.5, 12, 10, 11.7, 300)]
	# daily: the row is at the open (14:30 UTC), the bar at New York midnight; the price now belongs to its day
	daily = bars_of(result([(ts(2026, 11, 23, 14, 30), 1, 2, 0.5, 1.5, 10), (ts(2026, 11, 24, 14, 30), 2, 3, 1, 2.5, 20),
		(ts(2026, 11, 24, 17, 59, 30), 2.7, 2.7, 2.7, 2.7, 0)]), "1D")
	assert [(b.ts, b.close, b.volume) for b in daily] == [(datetime(2026, 11, 23, 5, tzinfo=UTC), 1.5, 10),
		(datetime(2026, 11, 24, 5, tzinfo=UTC), 2.7, 20)]
	# weekly: the week's row twice and the day's row: one bar at Monday, its volume once
	weekly = bars_of(result([(ts(2026, 11, 23, 5), 5, 9, 4, 8, 1000), (ts(2026, 11, 23, 5), 5, 9, 4, 8, 1000),
		(ts(2026, 11, 24, 21), 7, 9.5, 7, 8.2, 300)]), "1W")
	assert [(b.ts, b.open, b.high, b.close, b.volume) for b in weekly] == [(datetime(2026, 11, 23, 5, tzinfo=UTC), 5, 9.5, 8.2, 1000)]
	halves = bars_of(result([(ts(2026, 11, 24, 14, 30), 1, 2, 1, 2, 5), (ts(2026, 11, 24, 15, 0), 2, 4, 2, 3, 6),
		(ts(2026, 11, 24, 15, 30), 3, 3, 1, 1, 7)]), "30m")
	assert [(b.ts.hour, b.open, b.high, b.low, b.close, b.volume) for b in hourly(halves)] == [(14, 1, 2, 1, 2, 5), (15, 2, 4, 1, 1, 13)]
	assert bars_of({"meta": {}}, "1m") == []


def test_the_streams_messages():
	spy = tick_of(SPY_MESSAGE)
	assert spy.symbol == "SPY" and 711 < spy.price < 712 and spy.hours == 4 and spy.day_volume is None
	assert spy.ts == datetime(2026, 11, 25, 8, 0, 42, tzinfo=UTC) or abs((spy.ts - datetime(2026, 11, 25, 8, 0, tzinfo=UTC)).total_seconds()) < 3600
	btc = tick_of(BTC_MESSAGE)                            # the base64 alone (the stream's older form)
	assert btc.symbol == "BTC-USD" and 50_000 < btc.price < 70_000 and btc.hours == 1 and btc.day_volume > 0
	assert tick_of('{"type":"heartbeat","message":""}') is None


class Fake(YahooRelay):
	"""The relay with canned chart answers: 1m bars every minute from 14:30 to 21:00 UTC on weekdays."""

	def __init__(self, **kw):
		self.now = NOW
		self.asked: list[tuple] = []
		self.fail = False
		self.sent: list = []
		super().__init__(publish=lambda kind, symbol, msg: self.sent.append(msg), clock=lambda: self.now, **kw)

	async def _chart(self, symbol, interval, start, end):
		self.asked.append((symbol, interval, start, end))
		if self.fail or symbol == "NOSUCH":
			raise YahooError("Yahoo Finance: No data found, symbol may be delisted")
		step = {"1m": 60, "5m": 300, "30m": 1800, "1d": 86400}[interval]
		rows, t = [], int(start.timestamp()) // step * step
		while t <= min(end, self.now).timestamp():
			d = datetime.fromtimestamp(t, UTC)
			if t >= start.timestamp() and d.weekday() < 5 and (interval == "1d" or 14 * 60 + 30 <= d.hour * 60 + d.minute < 21 * 60):
				p = 100 + (t // 60) % 300 / 10
				rows.append((t + (52200 if interval == "1d" else 0), p, p + 1, p - 1, p + 0.5, 1000))
			t += step
		self._names[symbol] = "Test Corp"
		return result(rows, "Test Corp")

	@staticmethod
	def sent_close(at: datetime) -> float:
		return 100 + (int(at.timestamp()) // 60) % 300 / 10 + 0.5


async def test_history_in_as_many_requests_as_yahoo_allows():
	relay = Fake()
	bars = await relay.get_bars(GetBars(request_id=1, symbol="SPY", timeframe="1m", limit=50_000, end=NOW,
		start=NOW - timedelta(days=60)))
	spans = [(end - start).days for _s, _i, start, end in relay.asked]
	assert bars.text == "" and bars.name == "Test Corp" and len(relay.asked) == 5 and max(spans) <= 7
	assert min(start for _s, _i, start, _e in relay.asked) >= NOW - timedelta(days=29)      # not beyond what Yahoo has
	assert bars.bars == sorted(bars.bars, key=lambda b: b.ts) and len({b.ts for b in bars.bars}) == len(bars.bars)
	assert bars.bars[-1].ts == NOW and bars.bars[0].ts >= NOW - timedelta(days=29)

	relay.asked.clear()
	few = await relay.get_bars(GetBars(request_id=2, symbol="SPY", timeframe="1m", limit=100, end=NOW))
	assert len(few.bars) == 100 and len(relay.asked) == 1 and few.bars[-1].ts == NOW

	hours = await relay.get_bars(GetBars(request_id=3, symbol="SPY", timeframe="1h", limit=5, end=NOW))
	assert [b.ts.minute for b in hours.bars] == [0] * 5 and relay.asked[-1][1] == "30m"
	days = await relay.get_bars(GetBars(request_id=4, symbol="SPY", timeframe="1D", limit=3, end=NOW))
	assert [b.ts for b in days.bars] == [datetime(2026, 11, d, 5, tzinfo=UTC) for d in (20, 23, 24)]
	missing = await relay.get_bars(GetBars(request_id=5, symbol="NOSUCH", timeframe="5m", limit=10))
	assert missing.bars == [] and "No data found" in missing.text
	assert "unknown timeframe" in (await relay.get_bars(GetBars(request_id=6, symbol="SPY", timeframe="3m"))).text
	quote = await relay.latest_quote("SPY")                 # the newest 1m bar's close
	assert quote.ts == NOW and quote.bid == quote.ask == relay.sent_close(NOW)
	await relay.stop()


def tick(symbol, price, at, hours=1, volume=None, bid=0.0, ask=0.0):
	return Tick(symbol=symbol, price=price, ts=at, hours=hours, day_volume=volume, bid=bid, ask=ask)


async def test_live_quotes_and_bars():
	relay = Fake(quote_interval=0.01, bar_interval=0.01, complete_delay=0.02)
	relay._send = lambda message: None                    # no stream in the test
	relay._stream = asyncio.get_running_loop().create_future()
	await relay.start()
	relay.add(["SPY", "BRK.B"], quotes=True, bars=True)
	t0 = NOW.replace(second=5)
	relay.on_tick(tick("SPY", 500.0, t0, volume=1000))
	relay.on_tick(tick("SPY", 501.0, t0.replace(second=20), volume=1400))
	relay.on_tick(tick("SPY", 499.5, t0.replace(second=40), volume=1500, bid=499.4, ask=499.6))
	relay.on_tick(tick("BRK-B", 580.0, t0, hours=4))      # Yahoo's symbol; overnight: a quote, no bar
	relay.on_tick(tick("NVDA", 150.0, t0))                # not subscribed
	await asyncio.sleep(0.05)
	assert (await relay.latest_quote("BRK.B")).bid == 580.0     # the stream's newest price, no request
	quotes = {m.symbol: m for m in relay.sent if isinstance(m, Quote)}
	assert (quotes["SPY"].bid, quotes["SPY"].ask) == (499.4, 499.6) and (quotes["BRK.B"].bid, quotes["BRK.B"].ask) == (580.0, 580.0)
	forming = [m for m in relay.sent if isinstance(m, Bar)]
	assert {m.symbol for m in forming} == {"SPY"} and not forming[-1].complete
	assert (forming[-1].ts, forming[-1].open, forming[-1].high, forming[-1].low, forming[-1].close, forming[-1].volume) == (
		NOW, 500.0, 501.0, 499.5, 499.5, 500.0)

	relay.sent.clear()
	relay.now = NOW + timedelta(minutes=1, seconds=6)     # the minute is over: its bar from the chart endpoint
	relay.on_tick(tick("SPY", 502.0, relay.now, volume=1600))
	await asyncio.sleep(0.1)
	complete = [m for m in relay.sent if isinstance(m, Bar) and m.complete]
	assert [m.ts for m in complete] == [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1), NOW]     # and two before
	assert complete[-1].volume == 1000                    # Yahoo's bar, not the one from the prices

	relay.sent.clear()
	relay.fail = True                                     # Yahoo not reached: the bar built from the prices
	relay.now = NOW + timedelta(minutes=2, seconds=3)
	await asyncio.sleep(0.1)                              # no newer price: the flush loop ends the minute
	complete = [m for m in relay.sent if isinstance(m, Bar) and m.complete]
	assert [(m.ts, m.open, m.close, m.volume) for m in complete] == [(NOW + timedelta(minutes=1), 502.0, 502.0, 100.0)]
	relay.remove(["SPY", "BRK.B"], quotes=True, bars=True)
	assert relay.symbols == 0
	relay._stream = None
	await relay.stop()


async def test_the_gateway_on_yahoo(tmp_path):
	from brackfastgw.gateway.sim import Simulation
	relay = Fake(quote_interval=0.01, bar_interval=0.01)
	relay._send = lambda message: None
	relay._stream = asyncio.get_running_loop().create_future()
	core = GatewayCore(market=relay, port=free_port(), simulation=Simulation(file=tmp_path / "sim.json", all_hours=True))
	await core.start()
	async with aiohttp.ClientSession() as session:
		conn = Conn(await session.ws_connect(f"ws://127.0.0.1:{core.port}/"))
		await conn.send(Hello(protocol=PROTOCOL_VERSION, client="test"))
		welcome = await conn.wait(lambda m: isinstance(m, Welcome))
		assert (welcome.broker, welcome.environment, welcome.account) == ("simulated", "paper", "SIMULATED")
		assert (await conn.wait(lambda m: isinstance(m, Account))).equity == 100_000.0
		await conn.send(GetBars(request_id=1, symbol="spy", timeframe="5m", limit=20, end=NOW))
		bars = await conn.wait(lambda m: isinstance(m, Bars))
		assert len(bars.bars) == 20 and bars.name == "Test Corp"
		await conn.send(Subscribe(symbols=["SPY"]))
		await conn.send(SubmitBracket(id="y1", symbol="SPY", side="buy", qty=10, take_profit=150, stop_loss=50))
		assert (await conn.wait(lambda m: isinstance(m, Reply) and m.id == "y1")).ok
		fill = await conn.wait(lambda m: isinstance(m, Fill))             # at once: Yahoo's last price
		assert (fill.order_id, fill.price) == ("y1", relay.sent_close(NOW))
		relay.on_tick(tick("SPY", 151.0, NOW))                            # the stream: through the take profit
		exit_ = await conn.wait(lambda m: isinstance(m, Fill) and m.role == "take_profit")
		assert exit_.price == 150.0
		await conn.wait(lambda m: isinstance(m, Quote) and m.bid == 151.0)
	relay._stream = None
	await core.stop()
	assert core.snapshot is None or core.snapshot.provider == "Yahoo Finance"
