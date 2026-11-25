# Alpaca Gateway

> **Read-only mirror.** This GitHub repository mirrors the Alpaca Gateway's own repository. Pull requests and
> issues are not handled here: send bugs, questions and patches through the
> [feedback form](https://brackfast.io/#feedback).

A local gateway between trading applications and [Alpaca](https://alpaca.markets): the one
process that holds your API keys and talks to Alpaca's Trading and Market Data APIs. Apps
connect to the gateway instead of to Alpaca.

Status: early, paper accounts only so far.

## The gateway

```bash
uv sync --extra ui
uv run alpacagw                 # window; clients connect to ws://127.0.0.1:4102/ (paper)
uv run alpacagw --live          # config.cfg is a live account: confirm with --live (also on 4102)
uv run alpacagw --headless      # no window: config.cfg must hold the credentials
uv run alpacagw --config other.cfg   # another account file (it must exist)
uv run alpacagw --simulate      # a simulated account: Alpaca gives the market data only
uv run alpacagw --yahoo         # Yahoo Finance's market data (unofficial), no account, no keys
```

### Simulated account

With `--simulate` (or Settings, Simulation tab, "Simulated account") no order reaches Alpaca: orders stay in the gateway
and fill from Alpaca's quotes (`alpacagw/gateway/sim.py`). Clients see a paper account (`welcome`:
broker `simulated`, account `SIMULATED`) and use the same commands.

- A buy fills at the ask, a sell at the bid; whole orders, no partial fills, no queue.
- Limit orders fill at their limit once the quote reaches it, stops at the quote once it passes them.
- Regular hours only (09:30 to 16:00 New York; limit orders with extended hours 04:00 to 20:00), unless
  "Fill outside regular hours" is set: then whenever there is a quote, also a closed market's last price.
- Brackets, OCOs, replace, cancel, close position and flatten work as at Alpaca; an OCO needs the
  position's shares.
- No margin rules, fees or buying power check. The account (cash, positions, working orders) is kept
  in `simulated-account-<environment>.json` next to the settings; Settings has a reset.

### Yahoo Finance

With `--yahoo` (or Settings, Data source; or "Use Yahoo Finance" in the login dialog) the market data comes
from Yahoo Finance instead of Alpaca (`alpacagw/gateway/yahoo.py`). No account and no keys are needed; the
account is the simulated one, as Yahoo has no orders.

**Unofficial:** Yahoo has no public API. The gateway uses what Yahoo's web site uses (its chart endpoint and
its price stream); Yahoo can change or block that at any time, and its terms of use apply to you. Nothing about
you is sent.

- History: 1m bars for the last 30 days, 2m to 1h for the last 60 days, daily, weekly and monthly as far
  back as there is; extended hours included, prices split adjusted. Through the bar cache (`yahoo.sqlite`).
- Live: each symbol's last price, sent as a quote with that price as bid and ask (Yahoo sends no bid / ask),
  and as the forming 1m bar; after each minute Yahoo's own bar of that minute replaces it.
- Simulated fills are therefore at the last price, without a spread.
- Overnight prices (20:00 to 04:00 New York) are quotes only, there are no bars for them.
- US stocks and ETFs by their ticker (`BRK.B` is asked as Yahoo's `BRK-B`).

### Bar cache

Historical bars are kept on disk (`alpacagw/gateway/barcache.py`; Settings, "Keep historical bars on disk"),
so a history request asks Alpaca only for what is missing:

- One SQLite file per data source in `bar-cache/` next to the settings: `alpaca-iex.sqlite`,
  `alpaca-sip.sqlite`, ... Their bars differ, and one source's file can be cleared or deleted alone.
- Per symbol and timeframe it keeps the bars and the time ranges asked completely, so a night or a
  weekend without bars is not asked for again.
- Only bars complete for 15 minutes are kept; the newest part of a request always comes from Alpaca.
- Alpaca's bars are split adjusted: each request to Alpaca also asks for the newest cached bar again,
  and when its prices changed (a split), the symbol's cache is dropped.
- A file above the size in the settings (500 MB) loses what was used longest ago at the next start.
- The window's Bar Cache tab lists the sources' files with their sizes, each with Clear, and Clear All.

The gateway is built in two halves for this: the market data (`market.py` for Alpaca, `yahoo.py`
for Yahoo Finance) and the broker (`broker.py`: `AlpacaBroker`, or `sim.SimBroker`, which knows nothing
of Alpaca).

The account comes from `config.cfg` in the gateway's folder: the project folder, or the folder of
the executable. If the file is missing, the gateway writes a template there. The gateway only reads
it and never writes credentials. `--config FILE` takes another file instead (a relative
name: in the current folder, else in the gateway's folder); it must exist, no template is written for it.

If `key_id` / `secret_key` are empty, the window asks for them at start. Credentials entered there
are kept in memory for that run only.

What the gateway does:

- **Orders:** brackets and single orders, replace, cancel, close a position, flatten all. Every
  command is checked before it is sent to Alpaca.
- **Tracking:** it follows the account's orders, including those placed elsewhere (Alpaca's web
  site, TradingView). A replaced order keeps its id for the clients, and held bracket legs that
  were moved are found by polling.
- **Account and positions:** pushed to the clients whenever they change.
- **Market data:** historical bars, live 1-minute bars (forming and complete) and quotes, so the
  clients need no Alpaca keys.

The protocol (msgpack over a websocket) is described in [PROTOCOL.md](PROTOCOL.md).

The window's settings (the gear) hold the data feed, the socket port and "Allow local connections
only" (on by default: 127.0.0.1). They are saved per environment in `%APPDATA%\alpacagw\settings.json`
and apply after a restart. `--port` and `--host` override them.

Without local-only, any computer that reaches the port can trade the account: use it only in a
network you trust.

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
	async with Alpaca("config.cfg") as alpaca:
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

`Alpaca("config.cfg")` reads one account's file. `*.cfg` files are git-ignored.

```ini
[alpaca]
environment = paper        ; paper | live
key_id = PK...
secret_key = ...
stock_feed = iex           ; optional: iex (free) | sip (paid) | delayed_sip
```

## Development

```bash
uv sync
uv run pytest              # offline: a local fake Alpaca server
uv run python examples/demo.py config.cfg
```

## License

MIT
