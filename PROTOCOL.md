# Gateway protocol, version 1

Apps talk to the gateway over a websocket: `ws://127.0.0.1:4102/` for a paper account and
`ws://127.0.0.1:4101/` for a live one by default (IB Gateway uses 4002 / 4001). The protocol does
not depend on the broker, so an app written for it works with any gateway that speaks it.

## Encoding

- Every message is a binary websocket frame holding one [msgpack](https://msgpack.org) map.
- The key `msg` names the kind of message, e.g. `{"msg": "cancel_order", "id": "b1"}`.
- Optional fields at their default value may be left out, in both directions. Unknown keys must
  be ignored.
- Times are msgpack timestamps (extension type -1), always UTC.
- Prices and quantities are floats. Symbols are upper case (the gateway converts them).
- The Python definitions are in [`alpacagw/protocol.py`](alpacagw/protocol.py).

## Connection

1. The client sends `hello` as its first message, within 5 seconds.
2. The gateway answers `welcome`, followed by the current state: `status`, `account`, one
   `position` per open position, and every working `order` (plus the orders that finished
   today).
3. From then on the gateway sends every change. Commands can be sent at any time.

The gateway closes the connection with code 1008 if `hello` is missing, the protocol version is
not supported, or the token is wrong. A gateway that listens beyond the local machine always
requires a token.

## Client to gateway

| msg | Fields | Effect |
|---|---|---|
| `hello` | `protocol` (1), `client` (name), `token` | Opens the session. |
| `submit_bracket` | `id`, `symbol`, `side`, `qty`, `take_profit`, `stop_loss`, `limit_price`?, `stop_loss_limit`?, `tif` (`gtc`) | An entry (market without `limit_price`, else limit) with a take profit leg and a stop loss leg. The legs get the ids `<id>.tp` and `<id>.sl`. |
| `submit_order` | `id`, `symbol`, `side`, `qty`, `type`, `limit_price`?, `stop_price`?, `tif` (`day`), `extended_hours` | A single order of type `market`, `limit`, `stop` or `stop_limit`. |
| `replace_order` | `id`, `limit_price`?, `stop_price`?, `qty`? | Changes a working order. The order keeps its `id`. |
| `cancel_order` | `id` | Cancels an order. Cancelling an entry cancels its legs. |
| `cancel_all` | `symbol` (`""` = all) | Cancels the working orders. |
| `close_position` | `symbol` | Cancels the symbol's orders, then closes the position at market. |
| `flatten_all` | | Cancels all orders and closes all positions. |
| `get_bars` | `request_id`, `symbol`, `timeframe`, `start`?, `end`?, `limit` (1000) | Historical bars, answered with `bars`. |
| `subscribe` | `symbols`, `quotes` (true), `bars` (true) | Live `quote` and 1-minute `bar` messages for the symbols. |
| `unsubscribe` | `symbols` | Stops them. |

Order ids are chosen by the client: 1 to 48 characters from `A-Z a-z 0-9 _ - : .`, unique per
account. Quantities are whole shares. The gateway rounds prices to a valid tick: $0.01 from $1
up, $0.0001 below.

Every command except `get_bars`, `subscribe` and `unsubscribe` is answered with a `reply`.
`ok: false` means it was refused, and `text` says why. An order that was sent shows its progress
in `order` messages.

Timeframes: `1m` `2m` `5m` `15m` `30m` `1h` `1D` `1W` `1M`.

## Gateway to client

| msg | Fields |
|---|---|
| `welcome` | `protocol`, `gateway` (name and version), `broker` (`alpaca`), `environment` (`paper` / `live`), `account` |
| `status` | `state` (`starting` / `ready` / `error` / `stopping`), `trading_connected`, `market_data_connected`, `clients`, `text` |
| `account` | `account`, `currency`, `equity`, `last_equity` (at the previous close), `cash`, `buying_power`, `daytrade_count`, `pattern_day_trader`, `ts` |
| `position` | `symbol`, `qty` (< 0 short; 0 = closed, sent once), `avg_price`, `current_price`, `market_value`, `unrealized_pl` |
| `order` | `id`, `symbol`, `side`, `type`, `qty`, `status`, `role`, `parent_id`, `leg_ids`, `limit_price`, `stop_price`, `tif`, `filled_qty`, `avg_fill_price`, `origin`, `reason`, `broker_id`, `created_at`, `updated_at` |
| `fill` | `order_id`, `symbol`, `side`, `qty` (this fill), `price`, `ts`, `role` |
| `reply` | `command`, `id`, `ok`, `text` |
| `bars` | `request_id`, `symbol`, `timeframe`, `bars` (oldest first; each one an array `[ts, open, high, low, close, volume]`, `ts` = the bar's start), `text` (error) |
| `bar` | `symbol`, `ts`, `open`, `high`, `low`, `close`, `volume`, `complete` |
| `quote` | `symbol`, `ts`, `bid`, `bid_size`, `ask`, `ask_size` |
| `event` | `ts`, `level` (`info` / `warning` / `error`), `text`, `client`: log lines for the user |

### Orders

- `status` is one of `pending` (not acknowledged yet), `open`, `held` (a bracket leg waiting
  for its entry), `partially_filled`, `filled`, `canceled`, `expired` or `rejected`. The last
  four are final, and a final order is never sent again.
- `role` is one of `simple`, `entry`, `take_profit` or `stop_loss`. The legs point to their
  entry with `parent_id`, and the entry lists its legs in `leg_ids`.
- Orders placed elsewhere are reported too, with `origin` set to `external`. That includes the
  broker's web site, TradingView and other apps.
- Changes made elsewhere arrive as `order` messages with the same `id`, for example a leg
  dragged in TradingView.
- `fill` messages are sent once per execution. A client can just as well derive fills from
  `filled_qty`, which only grows.

### Live bars

- While a minute is forming, its bar is sent with `complete: false`, at most 4 times a second
  and only when it changes. It is built from trades.
- Shortly after the minute ends, the broker's own bar for it arrives with `complete: true` and
  replaces the forming one. A correction of a complete bar comes as another `complete: true`
  bar with the same `ts`.
- Quotes are sent at most 10 times a second per symbol, always the newest.
