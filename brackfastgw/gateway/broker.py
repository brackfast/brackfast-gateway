"""The broker side of the gateway: where orders go, and where the account and the positions come from.

``GatewayCore`` checks the clients' commands and hands them to its broker; the market data (``market.py``) is the
other side. Two brokers: ``AlpacaBroker`` (the account at Alpaca) and ``sim.SimBroker`` (a simulated account that
fills orders from the quotes, for a data source without an account or for trying things out).
"""
from __future__ import annotations

from typing import Any, Callable, Protocol

from ..protocol import Account, Fill, Order, Position, Quote
from . import redact
from .orders import OrderTracker


class Broker(Protocol):
	"""What ``GatewayCore`` needs of a broker. Orders are the protocol's ``Order``; every change of one is reported
	through ``on_order``, every fill through ``on_fill`` (given at construction). The submit methods, ``replace`` and
	``cancel`` return None or the reason the broker refused."""

	name: str                               # Welcome's broker: "alpaca" | "simulated"
	environment: str                        # Welcome's environment: "paper" | "live"
	orders: dict[str, Order]                # by the gateway's id

	@property
	def connected(self) -> bool: ...        # order updates arrive (Status.trading_connected)

	async def start(self) -> None: ...
	async def close(self) -> None: ...
	async def account(self) -> Account: ...
	async def positions(self) -> dict[str, Position]: ...
	def working(self, symbol: str | None = None) -> list[Order]: ...
	async def submit_bracket(self, entry: Order, tp: Order, sl: Order, stop_loss_limit: float | None) -> str | None: ...
	async def submit_oco(self, tp: Order, sl: Order, stop_loss_limit: float | None) -> str | None: ...
	async def submit_order(self, order: Order, extended_hours: bool = False) -> str | None: ...
	async def replace(self, order: Order, *, qty: float | None, limit_price: float | None,
		stop_price: float | None) -> str | None: ...
	async def cancel(self, order: Order) -> str | None: ...
	async def wait_canceled(self, orders: list[Order], timeout: float = 10.0) -> bool: ...
	async def close_position(self, symbol: str, origin: str) -> Order: ...
	async def flatten(self) -> int: ...     # cancel everything, close everything; how many positions are closing
	async def poll(self, fetch_missing: bool = True) -> None: ...    # every second: what no event reports
	def on_quote(self, quote: Quote) -> None: ...    # every quote the gateway sends to a client


class AlpacaBroker(OrderTracker):
	"""The account at Alpaca: the order tracker plus account, positions and the trade-update stream."""

	name = "alpaca"

	def __init__(self, alpaca: Any, *, on_order: Callable[[Order], None], on_fill: Callable[[Order, Fill], None],
			on_state: Callable[[], None], **kw: Any) -> None:
		super().__init__(alpaca.trading, on_order=on_order, on_fill=on_fill, **kw)
		self.alpaca = alpaca
		self.on_state = on_state                # the trade-update stream connected / disconnected

	@property
	def environment(self) -> str:
		return self.alpaca.environment

	@property
	def connected(self) -> bool:
		return self.alpaca.orders.is_ready

	async def start(self) -> None:
		orders = self.alpaca.orders
		orders.on_trade_update(self.on_update)
		orders.on_ready(lambda _m: (self.on_state(), self._spawn(self.reconcile())))
		orders.on_disconnected(lambda _m: self.on_state())
		acc = await self.trading.get_account()
		redact.account(acc.account_number)       # shortened in every log line from now on
		if acc.trading_blocked or acc.account_blocked:
			raise RuntimeError(f"Alpaca account {redact.mask_account(acc.account_number)} is blocked for trading")
		await self.load()
		await orders.start()

	async def account(self) -> Account:
		acc = await self.trading.get_account()
		return Account(account=acc.account_number, currency=acc.currency, equity=float(acc.equity or 0),
			last_equity=float(acc.last_equity or 0), cash=float(acc.cash or 0), buying_power=float(acc.buying_power or 0),
			daytrade_count=int(acc.daytrade_count or 0), pattern_day_trader=acc.pattern_day_trader)

	async def positions(self) -> dict[str, Position]:
		out: dict[str, Position] = {}
		for p in await self.trading.get_positions():
			if p.asset_class not in (None, "us_equity"):
				continue
			qty = float(p.qty)
			out[p.symbol] = Position(symbol=p.symbol, qty=-abs(qty) if p.side == "short" else qty,
				avg_price=float(p.avg_entry_price or 0), current_price=float(p.current_price or 0),
				market_value=float(p.market_value or 0), unrealized_pl=float(p.unrealized_pl or 0))
		return out

	async def flatten(self) -> int:
		return len(await self.trading.close_all_positions(cancel_orders=True))

	def on_quote(self, quote: Quote) -> None:
		pass
