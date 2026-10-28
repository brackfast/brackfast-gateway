# Alpaca Gateway

A local gateway between trading applications and [Alpaca](https://alpaca.markets): the one
process that holds your API keys and talks to Alpaca's Trading and Market Data APIs. Apps
connect to the gateway instead of to Alpaca, much as they connect to IB Gateway for Interactive
Brokers.

Status: early, paper accounts only so far.

## The gateway

```bash
uv sync --extra ui
uv run alpacagw --config paper.cfg              # window; clients connect to ws://127.0.0.1:4102/
uv run alpacagw --config live.cfg --live        # a live account must be confirmed with --live (port 4101)
uv run alpacagw --config paper.cfg --headless   # no window
```

What the gateway does:

- **Orders:** brackets and single orders, replace, cancel, close a position, flatten all. Every
  command is checked before it is sent to Alpaca.
- **Tracking:** it follows the account's orders, including those placed elsewhere (Alpaca's web
  site, TradingView). A replaced order keeps its id for the clients, and held bracket legs that
  were moved are found by polling.
- **Account and positions:** pushed to the clients whenever they change.
- **Market data:** historical bars, live 1-minute bars (forming and complete) and quotes, so the
  clients need no Alpaca keys.

The protocol (msgpack over a websocket) is described in [PROTOCOL.md](PROTOCOL.md). By default
the gateway listens on 127.0.0.1 only. `--host 0.0.0.0` also requires `--token`, which clients
must send in their hello.

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
