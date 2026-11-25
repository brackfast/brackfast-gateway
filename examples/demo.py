"""Read-only tour of the client: account, clock, positions, open orders, bars, snapshot, streams.

	uv run python examples/demo.py paper.cfg [SYMBOL]

Places no orders.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from brackfastgw.alpaca import (
	Alpaca, Bar, BarsParams, GetOrdersParams, QueryOrderStatus, Quote, StreamReady, TradeUpdate, Trade,
)


async def main(config: str, symbol: str) -> None:
	async with Alpaca(config) as alpaca:
		print(alpaca)
		account = await alpaca.trading.get_account()
		print(f"account {account.account_number}: {account.status}, equity {account.equity}, buying power {account.buying_power}")
		clock = await alpaca.trading.get_clock()
		print(f"market {'open' if clock.is_open else 'closed'}; next open {clock.next_open}, next close {clock.next_close}")
		for p in await alpaca.trading.get_positions():
			print(f"position {p.symbol}: {p.qty} @ {p.avg_entry_price}, unrealized {p.unrealized_pl}")
		orders = await alpaca.trading.get_orders(GetOrdersParams(status=QueryOrderStatus.OPEN, nested=True))
		print(f"{len(orders)} open orders")

		start = datetime.now(timezone.utc) - timedelta(days=5)
		bars = await alpaca.data.get_bars(BarsParams(symbols=[symbol], timeframe="1Hour", start=start))
		print(f"{len(bars.get(symbol, []))} hourly bars of {symbol} since {start:%Y-%m-%d}; last: {bars[symbol][-1] if bars.get(symbol) else None}")
		snapshot = (await alpaca.data.get_snapshots(symbol))[symbol]
		print(f"snapshot {symbol}: quote {snapshot.latest_quote}")

		counts = {"trades": 0, "quotes": 0, "bars": 0}
		alpaca.stocks.on(Trade, lambda t: counts.__setitem__("trades", counts["trades"] + 1), symbols=[symbol])
		alpaca.stocks.on(Quote, lambda q: counts.__setitem__("quotes", counts["quotes"] + 1), symbols=[symbol])
		alpaca.stocks.on(Bar, lambda b: print("bar", b), symbols=[symbol])
		alpaca.orders.on_trade_update(lambda u: print("trade update", u.event, u.order.symbol, u.order.status))
		alpaca.orders.on_ready(lambda r: print("trade updates: listening"))
		await alpaca.stocks.start()
		await alpaca.orders.start()
		await asyncio.sleep(15)
		print(f"15 s of {symbol} on {alpaca.stock_feed}: {counts['trades']} trades, {counts['quotes']} quotes")


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s")
	if len(sys.argv) < 2:
		sys.exit(__doc__)
	asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "AAPL"))
