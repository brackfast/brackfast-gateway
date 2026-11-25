"""Trading API v2: account, clock / calendar, assets, orders, positions."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .config import PAPER, Credentials, Endpoints
from .http import RestClient, path_param
from .models.base import dec
from .models.trading import (
	Account, Asset, CalendarDay, Clock, GetAssetsParams, GetOrdersParams, MultiStatusResult, Order,
	OrderRequest, Position, ReplaceOrderRequest,
)


class TradingClient(RestClient):
	log_name = "trading"

	def __init__(self, credentials: Credentials, endpoints: Endpoints = PAPER, **kw):
		super().__init__(endpoints.trading, credentials, **kw)

	# -- account / market ---------------------------------------------------------

	async def get_account(self) -> Account:
		return await self.get("/v2/account", rtype=Account)

	async def get_clock(self) -> Clock:
		return await self.get("/v2/clock", rtype=Clock)

	async def get_calendar(self, start: date | None = None, end: date | None = None) -> list[CalendarDay]:
		params = {"start": start and start.isoformat(), "end": end and end.isoformat()}
		return await self.get("/v2/calendar", params, list[CalendarDay])

	# -- assets -------------------------------------------------------------------

	async def get_assets(self, params: GetAssetsParams | None = None) -> list[Asset]:
		return await self.get("/v2/assets", params, list[Asset])

	async def get_asset(self, symbol_or_id: str) -> Asset:
		return await self.get(f"/v2/assets/{path_param(symbol_or_id)}", rtype=Asset)

	# -- orders -------------------------------------------------------------------

	async def submit_order(self, order: OrderRequest) -> Order:
		return await self.post("/v2/orders", order, Order)

	async def get_orders(self, params: GetOrdersParams | None = None) -> list[Order]:
		return await self.get("/v2/orders", params, list[Order])

	async def get_order(self, order_id: str, *, nested: bool = False) -> Order:
		return await self.get(f"/v2/orders/{path_param(order_id)}", {"nested": nested or None}, Order)

	async def get_order_by_client_id(self, client_order_id: str) -> Order:
		return await self.get("/v2/orders:by_client_order_id", {"client_order_id": client_order_id}, Order)

	async def replace_order(self, order_id: str, changes: ReplaceOrderRequest) -> Order:
		"""Alpaca replaces the order with a new one (new id; ``replaces`` points back)."""
		return await self.patch(f"/v2/orders/{path_param(order_id)}", changes, Order)

	async def cancel_order(self, order_id: str) -> None:
		await self.delete(f"/v2/orders/{path_param(order_id)}")

	async def cancel_all_orders(self) -> list[MultiStatusResult]:
		return await self.delete("/v2/orders", rtype=list[MultiStatusResult]) or []

	# -- positions ----------------------------------------------------------------

	async def get_positions(self) -> list[Position]:
		return await self.get("/v2/positions", rtype=list[Position])

	async def get_position(self, symbol: str) -> Position:
		return await self.get(f"/v2/positions/{path_param(symbol)}", rtype=Position)

	async def close_position(self, symbol: str, *, qty: Decimal | float | str | None = None,
			percentage: Decimal | float | str | None = None) -> Order:
		"""Market order closing the position (all of it, ``qty`` shares or ``percentage``)."""
		params = {"qty": dec(qty), "percentage": dec(percentage)}
		return await self.delete(f"/v2/positions/{path_param(symbol)}", params, Order)

	async def close_all_positions(self, *, cancel_orders: bool = False) -> list[MultiStatusResult]:
		return await self.delete("/v2/positions", {"cancel_orders": cancel_orders}, list[MultiStatusResult]) or []
