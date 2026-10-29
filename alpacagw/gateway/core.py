"""GatewayCore: the websocket server, the clients' commands and the account state.

Clients connect to ws://host:port/, send Hello, get Welcome and the current state (Status,
Account, Positions, Orders), then every change. Commands are checked here before they reach
Alpaca; each one is answered with a Reply. See PROTOCOL.md.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any

import aiohttp
import msgspec
from aiohttp import web

from .. import __version__
from ..client import Alpaca
from ..protocol import (
	FINAL, PROTOCOL_VERSION, Account, CancelAll, CancelOrder, ClosePosition, Event, FlattenAll, GetBars, Hello,
	Message, Order, Position, ReplaceOrder, Reply, Status, SubmitBracket, SubmitOrder, Subscribe, Unsubscribe,
	Welcome, decode_client, encode,
)
from .market import MarketRelay
from .orders import OrderTracker

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,48}$")
HELLO_TIMEOUT = 5.0
MAX_OUTBOX = 20_000                     # queued messages; a client this far behind is disconnected
UI_CLIENT = "gateway-ui"


def _now() -> datetime:
	return datetime.now(timezone.utc)


def round_price(price: float | None) -> float | None:
	"""A valid US stock tick: $0.01 from $1, $0.0001 below."""
	if price is None:
		return None
	return round(price, 2) if price >= 1.0 else round(price, 4)


class Client:
	"""One websocket connection. Messages go out in order through the outbox."""

	def __init__(self, ws: web.WebSocketResponse, name: str, peer: str) -> None:
		self.ws = ws
		self.name = name
		self.peer = peer
		self.quotes: set[str] = set()
		self.bars: set[str] = set()
		self.outbox: asyncio.Queue[bytes] = asyncio.Queue()
		self.writer: asyncio.Task | None = None
		self.connected_at = _now()

	def send_raw(self, data: bytes) -> None:
		if self.outbox.qsize() >= MAX_OUTBOX:
			if self.writer is not None and not self.writer.done():
				self.writer.cancel()     # too slow: dropped, the reader loop ends with the socket
				asyncio.get_running_loop().create_task(self.ws.close(code=aiohttp.WSCloseCode.TRY_AGAIN_LATER,
					message=b"too slow"))
			return
		self.outbox.put_nowait(data)

	def send(self, msg: Message) -> None:
		self.send_raw(encode(msg))

	async def write(self) -> None:
		while True:
			data = await self.outbox.get()
			await self.ws.send_bytes(data)


class Snapshot:
	"""What the window shows, rebuilt on the event loop (``GatewayCore.snapshot``)."""

	def __init__(self, core: GatewayCore) -> None:
		self.environment = core.alpaca.environment
		self.address = f"ws://{core.host}:{core.port}/"
		self.status = core.status
		self.account = core.account
		self.rest_ok = core.rest_ok
		self.feed = core.alpaca.stock_feed
		self.market_symbols = core.market.symbols
		self.clients = [(c.name, c.peer, c.connected_at, len(c.quotes | c.bars)) for c in core.clients]
		self.positions = sorted(core.positions.values(), key=lambda p: p.symbol)
		self.orders = sorted(core.tracker.orders.values(), key=lambda o: o.created_at or _now(), reverse=True)
		self.events = list(core.events)


class GatewayCore:
	def __init__(self, alpaca: Alpaca, *, host: str = "127.0.0.1", port: int = 4102,
			refresh: float = 5.0, order_poll: float = 1.0, quote_interval: float = 0.1, bar_interval: float = 0.25,
			log: logging.Logger | None = None) -> None:
		self.alpaca = alpaca
		self.host = host
		self.port = port
		self.refresh = refresh
		self.order_poll = order_poll
		self.log = log or logging.getLogger("alpacagw.gateway")
		self.status = Status(state="starting")
		self.account: Account | None = None
		self.rest_ok = False                     # the last account / positions request worked
		self.positions: dict[str, Position] = {}
		self.events: deque[Event] = deque(maxlen=2000)
		self.clients: list[Client] = []
		self.tracker = OrderTracker(alpaca.trading, on_order=self._on_order, on_fill=self._on_fill)
		self.market = MarketRelay(alpaca, publish=self._publish_market, quote_interval=quote_interval,
			bar_interval=bar_interval)
		self.snapshot: Snapshot | None = None      # for the window; replaced, never changed
		self.loop: asyncio.AbstractEventLoop | None = None
		self._runner: web.AppRunner | None = None
		self._tasks: set[asyncio.Task] = set()
		self._loops: list[asyncio.Task] = []
		self._positions_soon: asyncio.TimerHandle | None = None
		self._stopped = asyncio.Event()
		self._stop_requested = False

	# -- lifecycle ------------------------------------------------------------------

	async def start(self) -> None:
		self.loop = asyncio.get_running_loop()
		app = web.Application()
		app.router.add_get("/", self._handle_ws)
		self._runner = web.AppRunner(app, access_log=None)
		await self._runner.setup()
		await web.TCPSite(self._runner, self.host, self.port).start()
		self.event("info", f"gateway {__version__} listening on ws://{self.host}:{self.port}/ ({self.alpaca.environment} account)")
		orders = self.alpaca.orders
		orders.on_trade_update(self.tracker.on_update)
		orders.on_ready(lambda _m: (self._stream_state(), self._spawn(self.tracker.reconcile())))
		orders.on_disconnected(lambda _m: self._stream_state())
		self.alpaca.stocks.on_ready(lambda _m: self._stream_state())
		self.alpaca.stocks.on_disconnected(lambda _m: self._stream_state())
		try:
			acc = await self.alpaca.trading.get_account()
			if acc.trading_blocked or acc.account_blocked:
				raise RuntimeError(f"Alpaca account {acc.account_number} is blocked for trading")
			await self._refresh()
			self.rest_ok = True
			await self.tracker.load()
			await orders.start()
			await self.market.start()
		except Exception as exc:
			self._set_status("error", str(exc))
			self.event("error", f"start failed: {exc}")
			raise
		self._set_status("ready", "")
		self._loops = [self._spawn(self._refresh_loop()), self._spawn(self._poll_loop()), self._spawn(self._snapshot_loop())]

	async def stop(self) -> None:
		self._set_status("stopping")
		for task in self._loops:
			task.cancel()
		for client in list(self.clients):
			await client.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"gateway stopping")
		if self._runner is not None:
			await self._runner.cleanup()
		await self.tracker.close()
		await self.market.stop()
		await self.alpaca.close()
		self._stopped.set()

	async def run(self) -> None:
		"""Start and serve until ``request_stop``."""
		try:
			await self.start()
		except BaseException:
			await self.stop()                    # free the port: the window may start another try
			raise
		try:
			await self._stopped.wait()
		finally:
			if self.status.state != "stopping":
				await self.stop()

	def request_stop(self) -> None:
		"""Thread-safe: stop the gateway (the window's Quit). Once only: asked again (the window closing
		after the gateway stopped), the loop may be closed already."""
		if self.loop is None or self._stop_requested or self.loop.is_closed():
			return
		self._stop_requested = True
		try:
			self.loop.call_soon_threadsafe(lambda: self._spawn(self.stop()))
		except RuntimeError:                     # closed between the check and the call
			pass

	def _spawn(self, coro) -> asyncio.Task:
		task = asyncio.get_running_loop().create_task(coro)
		self._tasks.add(task)
		task.add_done_callback(self._task_done)
		return task

	def _task_done(self, task: asyncio.Task) -> None:
		self._tasks.discard(task)
		if not task.cancelled() and task.exception() is not None:
			self.log.error("gateway task failed", exc_info=task.exception())

	# -- state ----------------------------------------------------------------------

	def broadcast(self, msg: Message) -> None:
		data = encode(msg)
		for client in self.clients:
			client.send_raw(data)

	def _set_status(self, state: str | None = None, text: str | None = None) -> None:
		s = self.status
		if state is not None:
			s.state = state
		if text is not None:
			s.text = text
		s.clients = [c.name for c in self.clients]
		s.trading_connected = self.alpaca.orders.is_ready
		s.market_data_connected = self.market.connected
		self.broadcast(s)

	def _stream_state(self) -> None:
		trading, market = self.alpaca.orders.is_ready, self.market.connected
		if trading != self.status.trading_connected and self.status.state != "stopping":    # not our own stop
			self.event("info" if trading else "warning", f"alpaca trade updates {'connected' if trading else 'disconnected'}")
		self._set_status()

	def event(self, level: str, text: str, client: str = "") -> None:
		ev = Event(ts=_now(), level=level, text=text, client=client)
		self.events.append(ev)
		self.log.log(logging.getLevelName(level.upper()), "%s%s", f"[{client}] " if client else "", text)
		self.broadcast(ev)

	def _on_order(self, order: Order) -> None:
		self.broadcast(order)

	def _on_fill(self, order: Order, fill) -> None:
		self.broadcast(fill)
		self.event("info", f"fill {order.id}: {fill.side} {fill.qty:g} {fill.symbol} @ {fill.price:g} ({order.role})")
		self._refresh_positions_soon()

	def _refresh_positions_soon(self) -> None:
		if self._positions_soon is None:
			def run() -> None:
				self._positions_soon = None
				self._spawn(self._refresh_positions())
			self._positions_soon = asyncio.get_running_loop().call_later(0.3, run)

	async def _refresh(self) -> None:
		acc = await self.alpaca.trading.get_account()
		account = Account(account=acc.account_number, currency=acc.currency, equity=float(acc.equity or 0),
			last_equity=float(acc.last_equity or 0), cash=float(acc.cash or 0), buying_power=float(acc.buying_power or 0),
			daytrade_count=int(acc.daytrade_count or 0), pattern_day_trader=acc.pattern_day_trader)
		if self.account is None or msgspec.structs.replace(self.account, ts=None) != account:
			account.ts = _now()
			self.account = account
			self.broadcast(account)
		await self._refresh_positions()

	async def _refresh_positions(self) -> None:
		fresh: dict[str, Position] = {}
		for p in await self.alpaca.trading.get_positions():
			if p.asset_class not in (None, "us_equity"):
				continue
			qty = float(p.qty)
			fresh[p.symbol] = Position(symbol=p.symbol, qty=-abs(qty) if p.side == "short" else qty,
				avg_price=float(p.avg_entry_price or 0), current_price=float(p.current_price or 0),
				market_value=float(p.market_value or 0), unrealized_pl=float(p.unrealized_pl or 0))
		for symbol in self.positions.keys() - fresh.keys():
			self.broadcast(Position(symbol=symbol, qty=0.0))
		for symbol, p in fresh.items():
			if self.positions.get(symbol) != p:
				self.broadcast(p)
		self.positions = fresh

	async def _refresh_loop(self) -> None:
		failing = False
		while True:
			await asyncio.sleep(self.refresh)
			try:
				await self._refresh()
				failing = False
			except asyncio.CancelledError:
				raise
			except Exception as exc:
				if not failing:
					self.event("warning", f"account / positions refresh failed: {exc}")
				failing = True
			self.rest_ok = not failing

	async def _poll_loop(self) -> None:
		n, failing = 0, False
		every = max(1, round(self.refresh / self.order_poll))
		while True:
			await asyncio.sleep(self.order_poll)
			n += 1
			try:
				await self.tracker.poll(fetch_missing=n % every == 0)
				failing = False
			except asyncio.CancelledError:
				raise
			except Exception as exc:
				if not failing:
					self.event("warning", f"orders poll failed: {exc}")
				failing = True

	async def _snapshot_loop(self) -> None:
		while True:
			self.snapshot = Snapshot(self)
			await asyncio.sleep(0.25)

	# -- clients --------------------------------------------------------------------

	async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
		ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=1024 * 1024)
		await ws.prepare(request)
		peer = request.remote or "?"
		try:
			first = await ws.receive(timeout=HELLO_TIMEOUT)
			hello = decode_client(first.data) if first.type == aiohttp.WSMsgType.BINARY else None
		except Exception:
			hello = None
		problem = None
		if not isinstance(hello, Hello):
			problem = "the first message must be hello"
		elif hello.protocol != PROTOCOL_VERSION:
			problem = f"protocol {hello.protocol} not supported (gateway: {PROTOCOL_VERSION})"
		if problem:
			self.log.warning("connection from %s refused: %s", peer, problem)
			await ws.close(code=aiohttp.WSCloseCode.POLICY_VIOLATION, message=problem.encode())
			return ws
		names = {c.name for c in self.clients}
		name, n = hello.client or "client", 2
		while name in names:
			name, n = f"{hello.client or 'client'}-{n}", n + 1
		client = Client(ws, name, peer)
		client.writer = asyncio.get_running_loop().create_task(client.write())
		self._welcome(client)
		self.clients.append(client)
		self.event("info", f"client {name} connected from {peer}")
		self._set_status()
		try:
			async for msg in ws:
				if msg.type != aiohttp.WSMsgType.BINARY:
					continue
				try:
					cmd = decode_client(msg.data)
				except Exception as exc:
					client.send(Event(ts=_now(), level="error", text=f"undecodable message: {exc}"))
					continue
				self._command(client, cmd)
		finally:
			client.writer.cancel()
			self.clients.remove(client)
			self._unsubscribe(client, list(client.quotes | client.bars))
			self.event("info", f"client {name} disconnected")
			self._set_status()
		return ws

	def _welcome(self, client: Client) -> None:
		client.send(Welcome(protocol=PROTOCOL_VERSION, gateway=f"alpacagw {__version__}", broker="alpaca",
			environment=self.alpaca.environment, account=self.account.account if self.account else ""))
		client.send(self.status)
		if self.account is not None:
			client.send(self.account)
		for p in self.positions.values():
			client.send(p)
		today = _now().date()
		for o in self.tracker.orders.values():
			if o.status not in FINAL or (o.updated_at is not None and o.updated_at.date() == today):
				client.send(o)

	def _publish_market(self, kind: str, symbol: str, msg: Message) -> None:
		data = None
		for client in self.clients:
			if symbol in (client.quotes if kind == "quote" else client.bars):
				data = data or encode(msg)
				client.send_raw(data)

	def _unsubscribe(self, client: Client, symbols: list[str]) -> None:
		quotes = [s for s in symbols if s in client.quotes]
		bars = [s for s in symbols if s in client.bars]
		client.quotes.difference_update(quotes)
		client.bars.difference_update(bars)
		self.market.remove(quotes, quotes=True, bars=False)
		self.market.remove(bars, quotes=False, bars=True)

	# -- commands ------------------------------------------------------------------

	def _command(self, client: Client, cmd: Any) -> None:
		match cmd:
			case SubmitBracket():
				self._spawn(self.submit_bracket(cmd, client))
			case SubmitOrder():
				self._spawn(self.submit_order(cmd, client))
			case ReplaceOrder():
				self._spawn(self.replace(cmd, client))
			case CancelOrder():
				self._spawn(self.cancel(cmd.id, client))
			case CancelAll():
				self._spawn(self.cancel_all(cmd.symbol.upper(), client))
			case ClosePosition():
				self._spawn(self.close_position(cmd.symbol.upper(), client))
			case FlattenAll():
				self._spawn(self.flatten(client))
			case GetBars():
				self._spawn(self._get_bars(cmd, client))
			case Subscribe():
				symbols = [s.upper() for s in cmd.symbols if SYMBOL_RE.match(s.upper())]
				quotes = [s for s in symbols if cmd.quotes and s not in client.quotes]
				bars = [s for s in symbols if cmd.bars and s not in client.bars]
				client.quotes.update(quotes)
				client.bars.update(bars)
				self.market.add(quotes, quotes=True, bars=False)
				self.market.add(bars, quotes=False, bars=True)
			case Unsubscribe():
				self._unsubscribe(client, [s.upper() for s in cmd.symbols])
			case Hello():
				pass
			case _:
				client.send(Event(ts=_now(), level="error", text=f"unexpected message {type(cmd).__name__}"))

	@staticmethod
	def _name(client: Client | None) -> str:
		return client.name if client is not None else UI_CLIENT

	def _reply(self, client: Client | None, command: str, id: str, problem: str | None, done: str = "") -> None:
		ok = problem is None
		self.event("info" if ok else "warning", f"{command} {id}: {done if ok else problem}".rstrip(": "),
			self._name(client))
		if client is not None:
			client.send(Reply(command=command, id=id, ok=ok, text="" if ok else problem))

	def _check_new(self, id: str, symbol: str, side: str, qty: float) -> str | None:
		if self.status.state != "ready":
			return f"gateway not ready ({self.status.state})"
		if not ID_RE.match(id):
			return "id: 1 to 48 of A-Z a-z 0-9 _ - : ."
		if id in self.tracker.orders:
			return "id already used"
		if not SYMBOL_RE.match(symbol):
			return f"invalid symbol {symbol!r}"
		if side not in ("buy", "sell"):
			return f"side must be buy or sell, not {side!r}"
		if not (qty > 0 and float(qty).is_integer()):
			return f"qty must be a whole number of shares > 0, not {qty:g}"
		return None

	async def submit_bracket(self, cmd: SubmitBracket, client: Client | None = None) -> str | None:
		symbol = cmd.symbol.upper()
		limit, stop_entry, tp, sl, sl_limit = (round_price(p) for p in (cmd.limit_price, cmd.stop_price,
			cmd.take_profit, cmd.stop_loss, cmd.stop_loss_limit))
		entry_price = stop_entry if stop_entry is not None else limit      # where the entry triggers / fills
		problem = self._check_new(cmd.id, symbol, cmd.side, cmd.qty)
		if problem is None:
			long = cmd.side == "buy"
			if any(p is not None and not p > 0 for p in (limit, stop_entry, tp, sl, sl_limit)) or tp is None or sl is None:
				problem = "prices must be > 0"
			elif (tp <= sl) if long else (tp >= sl):
				problem = f"take profit must be {'above' if long else 'below'} the stop loss"
			elif entry_price is not None and not ((sl < entry_price < tp) if long else (tp < entry_price < sl)):
				problem = "the entry price must lie between stop loss and take profit"
			elif stop_entry is not None and limit is not None and ((limit < stop_entry) if long else (limit > stop_entry)):
				problem = f"the limit price of a stop limit entry must be {'at or above' if long else 'at or below'} its stop"
			elif cmd.tif not in ("day", "gtc"):
				problem = "tif must be day or gtc"
		if problem is not None:
			self._reply(client, "submit_bracket", cmd.id, problem)
			return problem
		origin, exit_side = self._name(client), "sell" if cmd.side == "buy" else "buy"
		if stop_entry is None:
			kind = "market" if limit is None else "limit"
		else:
			kind = "stop" if limit is None else "stop_limit"
		entry = Order(id=cmd.id, symbol=symbol, side=cmd.side, type=kind, qty=cmd.qty, status="pending", role="entry",
			leg_ids=[f"{cmd.id}.tp", f"{cmd.id}.sl"], limit_price=limit, stop_price=stop_entry, tif=cmd.tif,
			origin=origin, created_at=_now())
		take = Order(id=f"{cmd.id}.tp", symbol=symbol, side=exit_side, type="limit", qty=cmd.qty, status="pending",
			role="take_profit", parent_id=cmd.id, limit_price=tp, tif=cmd.tif, origin=origin, created_at=_now())
		stop = Order(id=f"{cmd.id}.sl", symbol=symbol, side=exit_side, type="stop" if sl_limit is None else "stop_limit",
			qty=cmd.qty, status="pending", role="stop_loss", parent_id=cmd.id, stop_price=sl, limit_price=sl_limit,
			tif=cmd.tif, origin=origin, created_at=_now())
		problem = await self.tracker.submit_bracket(entry, take, stop, sl_limit)
		at = {"market": "market", "limit": f"limit {limit}", "stop": f"stop {stop_entry}",
			"stop_limit": f"stop {stop_entry} limit {limit}"}[kind]
		self._reply(client, "submit_bracket", cmd.id, problem, f"{cmd.side} {cmd.qty:g} {symbol} {at}, "
			f"take profit {tp}, stop loss {sl}")
		return problem

	async def submit_order(self, cmd: SubmitOrder, client: Client | None = None) -> str | None:
		symbol = cmd.symbol.upper()
		limit, stop = round_price(cmd.limit_price), round_price(cmd.stop_price)
		problem = self._check_new(cmd.id, symbol, cmd.side, cmd.qty)
		if problem is None:
			if cmd.type not in ("market", "limit", "stop", "stop_limit"):
				problem = f"unknown type {cmd.type!r}"
			elif (limit is None) == (cmd.type in ("limit", "stop_limit")):
				problem = f"a {cmd.type} order {'needs' if limit is None else 'has no'} limit price"
			elif (stop is None) == (cmd.type in ("stop", "stop_limit")):
				problem = f"a {cmd.type} order {'needs' if stop is None else 'has no'} stop price"
			elif any(p is not None and not p > 0 for p in (limit, stop)):
				problem = "prices must be > 0"
			elif cmd.tif not in ("day", "gtc", "ioc", "fok", "opg", "cls"):
				problem = f"unknown tif {cmd.tif!r}"
			elif cmd.extended_hours and (cmd.type != "limit" or cmd.tif != "day"):
				problem = "extended hours: limit orders with tif day only"
		if problem is not None:
			self._reply(client, "submit_order", cmd.id, problem)
			return problem
		order = Order(id=cmd.id, symbol=symbol, side=cmd.side, type=cmd.type, qty=cmd.qty, status="pending",
			limit_price=limit, stop_price=stop, tif=cmd.tif, origin=self._name(client), created_at=_now())
		problem = await self.tracker.submit_order(order, cmd.extended_hours)
		self._reply(client, "submit_order", cmd.id, problem, f"{cmd.side} {cmd.qty:g} {symbol} {cmd.type}")
		return problem

	async def replace(self, cmd: ReplaceOrder, client: Client | None = None) -> str | None:
		o = self.tracker.orders.get(cmd.id)
		limit, stop = round_price(cmd.limit_price), round_price(cmd.stop_price)
		problem = None
		if o is None:
			problem = "unknown order"
		elif o.status in FINAL:
			problem = f"already {o.status}"
		elif o.type == "market":
			problem = "a market order has no price"
		elif limit is not None and o.type not in ("limit", "stop_limit"):
			problem = f"a {o.type} order has no limit price"
		elif stop is not None and o.type not in ("stop", "stop_limit"):
			problem = f"a {o.type} order has no stop price"
		elif any(p is not None and not p > 0 for p in (limit, stop, cmd.qty)):
			problem = "prices and qty must be > 0"
		elif limit is None and stop is None and cmd.qty is None:
			problem = "nothing to change"
		if problem is None:
			problem = await self.tracker.replace(o, qty=cmd.qty, limit_price=limit, stop_price=stop)
		what = ", ".join(f"{n} {v:g}" for n, v in (("limit", limit), ("stop", stop), ("qty", cmd.qty)) if v is not None)
		self._reply(client, "replace_order", cmd.id, problem, what)
		return problem

	async def cancel(self, id: str, client: Client | None = None) -> str | None:
		o = self.tracker.orders.get(id)
		problem = "unknown order" if o is None else await self.tracker.cancel(o)
		self._reply(client, "cancel_order", id, problem, "cancel sent")
		return problem

	def _cancelable(self, symbol: str) -> list[Order]:
		"""Working orders of a symbol ("" all), without the legs of a working entry (they go with it)."""
		working = self.tracker.working(symbol or None)
		ids = {o.id for o in working}
		return [o for o in working if not (o.parent_id and o.parent_id in ids)]

	async def cancel_all(self, symbol: str, client: Client | None = None) -> None:
		orders = self._cancelable(symbol)
		problems = [p for p in await asyncio.gather(*(self.tracker.cancel(o) for o in orders)) if p]
		self._reply(client, "cancel_all", symbol or "*", "; ".join(problems) or None, f"{len(orders)} order(s)")

	async def close_position(self, symbol: str, client: Client | None = None) -> None:
		orders = self._cancelable(symbol)
		for o in orders:
			await self.tracker.cancel(o)
		if orders and not await self.tracker.wait_canceled(orders):
			self.event("warning", f"close {symbol}: cancels still pending, closing anyway", self._name(client))
		if symbol not in self.positions:
			await self._refresh_positions()
		if symbol not in self.positions:
			self._reply(client, "close_position", symbol, None, f"{len(orders)} order(s) canceled, no position")
			return
		try:
			order = await self.tracker.close_position(symbol, self._name(client))
		except Exception as exc:
			self._reply(client, "close_position", symbol, str(exc))
			return
		self._reply(client, "close_position", symbol, None, f"close order {order.id}")

	async def flatten(self, client: Client | None = None) -> None:
		try:
			results = await self.alpaca.trading.close_all_positions(cancel_orders=True)
		except Exception as exc:
			self._reply(client, "flatten_all", "*", str(exc))
			return
		self._reply(client, "flatten_all", "*", None, f"orders canceled, {len(results)} position(s) closing")

	async def _get_bars(self, cmd: GetBars, client: Client) -> None:
		client.send(await self.market.get_bars(GetBars(request_id=cmd.request_id, symbol=cmd.symbol.upper(),
			timeframe=cmd.timeframe, start=cmd.start, end=cmd.end, limit=cmd.limit)))

	# -- from the window (other thread) ------------------------------------------------

	def call(self, coro_fn, *args) -> None:
		"""Run ``coro_fn(*args)`` on the gateway's loop; thread-safe."""
		if self.loop is not None:
			self.loop.call_soon_threadsafe(lambda: self._spawn(coro_fn(*args)))
