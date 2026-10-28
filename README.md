# Alpaca Gateway

A local gateway between trading applications and [Alpaca](https://alpaca.markets): the one
process that holds your API keys and talks to Alpaca's Trading and Market Data APIs. Apps
connect to the gateway instead of to Alpaca, much as they connect to IB Gateway for Interactive
Brokers.

Status: early. What exists so far is `alpacagw.client`, the async Alpaca client the gateway is
built on. The gateway server and its protocol come next.

## alpacagw.client

An asyncio client for US stocks, built on aiohttp and msgspec:

- Trading API: account, clock / calendar, assets, orders (market, limit, stop, stop limit,
  bracket), replace, cancel, positions, close positions
- Market Data API: historical bars / trades / quotes (pages merged), latest trades / quotes /
  bars, snapshots
- Streams: stock data (trades, quotes, bars, daily / updated bars, statuses, LULD) and the
  account's trade updates; authentication, reconnects and resubscribing are handled for you

```python
import asyncio
from alpacagw.client import Alpaca, Bar, OrderRequest, TradeUpdate

async def main():
	async with Alpaca("paper.cfg") as alpaca:
		print(await alpaca.trading.get_account())

		alpaca.stocks.on(Bar, lambda bar: print(bar), symbols=["AAPL"])
		alpaca.orders.on_trade_update(lambda u: print(u.event, u.order.symbol))
		await alpaca.stocks.start()
		await alpaca.orders.start()

		order = await alpaca.trading.submit_order(OrderRequest.bracket(
			"AAPL", "buy", 1, limit_price=150, take_profit=160, stop_loss=145))
		await asyncio.sleep(60)

asyncio.run(main())
```

Plain functions passed as handlers run inline as messages are decoded. Async handlers get
their messages one at a time, in the order they arrived.

### Configuration

Use one config file per account. Copy `paper.cfg.example` to `paper.cfg` and fill it in.
`*.cfg` files are git-ignored.

```ini
[alpaca]
environment = paper        ; paper | live
key_id = PK...
secret_key = ...
stock_feed = iex           ; iex (free) | sip (paid) | delayed_sip
```

## Development

```bash
uv sync
uv run pytest              # offline: a local fake Alpaca server
uv run python examples/demo.py paper.cfg
```

## License

MIT
