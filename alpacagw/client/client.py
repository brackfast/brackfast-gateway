"""``Alpaca``: one account, one shared aiohttp session, streams created on first use."""
from __future__ import annotations

import os

import aiohttp

from .config import AlpacaConfig, load_config
from .market_data import MarketDataClient
from .streams import StockDataStream, TradingStream
from .trading import TradingClient


class Alpaca:
	"""
	alpaca = Alpaca("paper.cfg")              # one config file per account
	account = await alpaca.trading.get_account()
	bars = await alpaca.data.get_bars(BarsParams(symbols=["AAPL"], timeframe="1Min", start=...))
	alpaca.stocks.on(Bar, on_bar, symbols=["AAPL"])
	alpaca.orders.on_trade_update(on_update)
	await alpaca.stocks.start(); await alpaca.orders.start()
	...
	await alpaca.close()

	``async with Alpaca("paper.cfg") as alpaca:`` closes it at the end.
	"""

	def __init__(self, config: AlpacaConfig | str | os.PathLike, *, stock_feed: str | None = None,
			http_timeout: float = 10.0, **stream_kw):
		self.config = config if isinstance(config, AlpacaConfig) else load_config(config)
		self.stock_feed = stock_feed or self.config.stock_feed
		self._http_timeout = http_timeout
		self._stream_kw = stream_kw
		self._session: aiohttp.ClientSession | None = None
		self._stocks: StockDataStream | None = None
		self._orders: TradingStream | None = None
		credentials, endpoints = self.config.credentials, self.config.endpoints
		self.trading = TradingClient(credentials, endpoints, session=self._shared_session, timeout=http_timeout)
		self.data = MarketDataClient(credentials, endpoints, feed=self.stock_feed, session=self._shared_session,
			timeout=http_timeout)

	@property
	def environment(self) -> str:
		""""paper" or "live"."""
		return self.config.environment

	async def _shared_session(self) -> aiohttp.ClientSession:
		if self._session is None or self._session.closed:
			# no total timeout on the session: it would end the websockets; requests get theirs below
			self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,
				sock_connect=self._http_timeout, sock_read=None))
		return self._session

	@property
	def stocks(self) -> StockDataStream:
		if self._stocks is None:
			self._stocks = StockDataStream(self.config.credentials, feed=self.stock_feed,
				endpoints=self.config.endpoints, session=self._shared_session, **self._stream_kw)
		return self._stocks

	@property
	def orders(self) -> TradingStream:
		if self._orders is None:
			self._orders = TradingStream(self.config.credentials, endpoints=self.config.endpoints,
				session=self._shared_session, **self._stream_kw)
		return self._orders

	async def close(self) -> None:
		"""Stop the streams and close the HTTP session."""
		for stream in (self._stocks, self._orders):
			if stream is not None:
				await stream.stop()
		if self._session is not None and not self._session.closed:
			await self._session.close()
		self._session = None

	async def __aenter__(self) -> Alpaca:
		return self

	async def __aexit__(self, *exc) -> None:
		await self.close()

	def __repr__(self) -> str:
		return f"Alpaca({self.environment}, {self.config.path}, {self.config.credentials!r})"
