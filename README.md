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
```

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

## Building the program

```bash
uv run --no-project tools/build_dist.py             # Windows and Linux; --windows / --linux for one
uv run --no-project tools/build_dist.py --dir       # Windows as the folder dist/alpaca-gateway (to try it)
```

The output is `dist/alpaca-gateway-<version>-<date>-windows-x86_64.zip` and
`dist/alpaca-gateway-<version>-<date>-linux-x86_64.tar.xz`, about 29 MB each. Each holds one folder
`alpaca-gateway/` that runs as it is, with nothing to install:

- **The launcher:** `Alpaca Gateway.cmd` on Windows, `alpaca-gateway` on Linux.
- **`alpacagw/`:** the gateway as Python source. It is not compiled.
- **`python/`:** a standalone Python 3.13 from python-build-standalone, the build uv uses. The
  libraries from `uv.lock` are in its site-packages.
- **`README.txt`:** [USER-README.md](USER-README.md), the guide for users (starting, the account,
  settings, options), and `LICENSE`. This README and PROTOCOL.md are for developers and stay out.

Both archives can be built on either system:

- **Python:** comes from its release archive and is checked against a pinned SHA-256.
- **Libraries:** uv installs their wheels for the target platform.

The downloads are kept in `build/cache`.

The version is the git tag `vX.Y.Z`, nothing in the files holds it. Release with
`git tag -a v0.2.0 -m "Alpaca Gateway 0.2.0"` and `git push origin main v0.2.0`, then build. When uv
installs the gateway, hatch-vcs writes `alpacagw/_version.py` (not in git), which `alpacagw.__version__` reads;
the build reinstalls it first, so the archives and the program always show the current version. A commit
after the tag gives `X.Y.(Z+1).devN+g<commit>` (and a warning from the build), uncommitted changes add
`.d<date>`.

**Changes per version:** [CHANGELOG.md](CHANGELOG.md) has a section `## X.Y.Z` for every release, written for
users. Add it before tagging. A release build writes it to `dist/alpaca-gateway-<version>-changes.md` next to
the archives, for the website, and stops if the section is missing. A development build (`0.1.1.dev8+g...`) writes `## Next`, the changes since the last release, if there is such a section.

On Linux the Python needs glibc 2.28 or newer. The Linux build was checked headless in WSL (Ubuntu 22.04).
`config.cfg` and `alpacagw.log` live in the program's folder.

## License

MIT
