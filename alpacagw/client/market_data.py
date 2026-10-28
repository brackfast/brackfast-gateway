"""Market Data API v2, stocks: historical bars / trades / quotes, latest data, snapshots."""
from __future__ import annotations

from typing import AsyncIterator, Iterable

import msgspec

from .config import PAPER, Credentials, Endpoints
from .http import RestClient
from .models.market import (
	BarData, BarsPage, BarsParams, LatestBars, LatestQuotes, LatestTrades, QuoteData, QuotesPage,
	QuotesParams, Snapshot, TradeData, TradesPage, TradesParams,
)

_STOCKS = "/v2/stocks"


def _symbols(symbols: Iterable[str] | str) -> list[str]:
	return [symbols] if isinstance(symbols, str) else list(symbols)


class MarketDataClient(RestClient):
	"""``feed`` is used wherever a request does not name its own."""

	log_name = "data"

	def __init__(self, credentials: Credentials, endpoints: Endpoints = PAPER, *, feed: str = "iex", **kw):
		super().__init__(endpoints.data, credentials, **kw)
		self.feed = feed

	def _with_feed(self, params: msgspec.Struct) -> msgspec.Struct:
		if getattr(params, "feed", None) is None:
			return msgspec.structs.replace(params, feed=self.feed)
		return params

	async def _merged(self, path: str, params: msgspec.Struct, page_type: type,
			max_pages: int | None) -> dict[str, list]:
		"""All pages merged into {symbol: [items]}."""
		out: dict[str, list] = {}
		async for page in self.pages(path, self._with_feed(params), page_type, max_pages=max_pages):
			for symbol, items in (page.data or {}).items():
				out.setdefault(symbol, []).extend(items)
		return out

	# -- historical ---------------------------------------------------------------

	async def get_bars(self, params: BarsParams, *, max_pages: int | None = None) -> dict[str, list[BarData]]:
		return await self._merged(f"{_STOCKS}/bars", params, BarsPage, max_pages)

	def iter_bars(self, params: BarsParams, *, max_pages: int | None = None) -> AsyncIterator[BarsPage]:
		return self.pages(f"{_STOCKS}/bars", self._with_feed(params), BarsPage, max_pages=max_pages)

	async def get_trades(self, params: TradesParams, *, max_pages: int | None = None) -> dict[str, list[TradeData]]:
		return await self._merged(f"{_STOCKS}/trades", params, TradesPage, max_pages)

	async def get_quotes(self, params: QuotesParams, *, max_pages: int | None = None) -> dict[str, list[QuoteData]]:
		return await self._merged(f"{_STOCKS}/quotes", params, QuotesPage, max_pages)

	# -- latest -------------------------------------------------------------------

	def _latest_params(self, symbols: Iterable[str] | str, feed: str | None) -> dict:
		return {"symbols": _symbols(symbols), "feed": feed or self.feed}

	async def get_latest_trades(self, symbols: Iterable[str] | str, *, feed: str | None = None) -> dict[str, TradeData]:
		reply = await self.get(f"{_STOCKS}/trades/latest", self._latest_params(symbols, feed), LatestTrades)
		return reply.trades

	async def get_latest_quotes(self, symbols: Iterable[str] | str, *, feed: str | None = None) -> dict[str, QuoteData]:
		reply = await self.get(f"{_STOCKS}/quotes/latest", self._latest_params(symbols, feed), LatestQuotes)
		return reply.quotes

	async def get_latest_bars(self, symbols: Iterable[str] | str, *, feed: str | None = None) -> dict[str, BarData]:
		reply = await self.get(f"{_STOCKS}/bars/latest", self._latest_params(symbols, feed), LatestBars)
		return reply.bars

	async def get_snapshots(self, symbols: Iterable[str] | str, *, feed: str | None = None) -> dict[str, Snapshot]:
		return await self.get(f"{_STOCKS}/snapshots", self._latest_params(symbols, feed), dict[str, Snapshot])
