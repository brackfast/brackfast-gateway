"""REST transport: aiohttp, msgspec in and out, retries that never send an order twice."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, TypeVar
from urllib.parse import quote

import aiohttp
import msgspec

from .config import Credentials
from .errors import AlpacaError

T = TypeVar("T")

SessionProvider = Callable[[], Awaitable[aiohttp.ClientSession]]

_encoder = msgspec.json.Encoder()
_decoders: dict[Any, msgspec.json.Decoder] = {}


def _decoder(rtype: Any) -> msgspec.json.Decoder:
	decoder = _decoders.get(rtype)
	if decoder is None:
		decoder = _decoders[rtype] = msgspec.json.Decoder(rtype)
	return decoder


def path_param(value: Any) -> str:
	"""A value escaped for a URL path segment."""
	return quote(str(value), safe="")


def encode_params(params: msgspec.Struct | Mapping[str, Any] | None) -> dict[str, str]:
	"""Query parameters from a request struct or dict.

	None is dropped, lists are comma joined, bools lower case, datetimes RFC 3339 and
	Decimals / enums plain strings (via msgspec.to_builtins).
	"""
	if params is None:
		return {}
	if isinstance(params, msgspec.Struct):
		params = msgspec.to_builtins(params)
	out: dict[str, str] = {}
	for key, value in params.items():
		if value is None:
			continue
		if isinstance(value, bool):
			out[key] = "true" if value else "false"
		elif isinstance(value, (list, tuple, set, frozenset)):
			if value:
				out[key] = ",".join(str(v) for v in value)
		else:
			out[key] = str(msgspec.to_builtins(value))
	return out


class RestClient:
	"""Request machinery shared by the Trading and Market Data clients.

	Retries: 429 for every method (Alpaca rejected the request, nothing was executed), after
	X-RateLimit-Reset / Retry-After; 5xx and connection errors for GET only, so an order is
	never submitted twice.
	"""

	log_name = "rest"

	def __init__(self, base_url: str, credentials: Credentials, *, session: SessionProvider | None = None,
			timeout: float = 10.0, max_retries: int = 3):
		self._base = base_url.rstrip("/")
		self._credentials = credentials
		self._provider = session
		self._own_session: aiohttp.ClientSession | None = None
		self._request_timeout = aiohttp.ClientTimeout(total=timeout)
		self._max_retries = max_retries
		self._log = logging.getLogger(f"alpacagw.{self.log_name}")

	async def _session(self) -> aiohttp.ClientSession:
		if self._provider is not None:
			return await self._provider()
		if self._own_session is None or self._own_session.closed:
			self._own_session = aiohttp.ClientSession()
		return self._own_session

	async def close(self) -> None:
		"""Close the session this client created itself (a provided one belongs to its owner)."""
		if self._own_session is not None and not self._own_session.closed:
			await self._own_session.close()
		self._own_session = None

	@staticmethod
	def _backoff(attempt: int) -> float:
		return min(0.5 * 2 ** attempt, 10.0)

	def _rate_limit_delay(self, resp: aiohttp.ClientResponse, attempt: int) -> float:
		reset = resp.headers.get("X-RateLimit-Reset")
		if reset:
			try:
				return min(max(float(reset) - time.time(), 0.1), 60.0)
			except ValueError:
				pass
		after = resp.headers.get("Retry-After")
		if after:
			try:
				return min(float(after), 60.0)
			except ValueError:
				pass
		return self._backoff(attempt)

	async def request(self, method: str, path: str, *, params: msgspec.Struct | Mapping[str, Any] | None = None,
			body: Any = None, rtype: type[T] | Any = None) -> T:
		session = await self._session()
		url = self._base + path
		query = encode_params(params)
		data = None if body is None else _encoder.encode(body)
		headers = self._credentials.headers()
		if data is not None:
			headers["Content-Type"] = "application/json"
		attempt = 0
		while True:
			try:
				async with session.request(method, url, params=query, data=data, headers=headers,
						timeout=self._request_timeout) as resp:
					raw = await resp.read()
					status = resp.status
					delay = self._rate_limit_delay(resp, attempt) if status == 429 else self._backoff(attempt)
			except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
				if method != "GET" or attempt >= self._max_retries:
					raise
				delay = self._backoff(attempt)
				self._log.warning("%s %s failed (%r), retry in %.1f s", method, path, exc, delay)
			else:
				if status < 400:
					if rtype is None or not raw:
						return None  # type: ignore[return-value]
					return _decoder(rtype).decode(raw)
				retry = status == 429 or (status >= 500 and method == "GET")
				if not retry or attempt >= self._max_retries:
					raise AlpacaError.from_body(status, raw)
				self._log.warning("%s %s -> HTTP %d, retry in %.1f s", method, path, status, delay)
			attempt += 1
			await asyncio.sleep(delay)

	async def get(self, path: str, params=None, rtype: type[T] | Any = None) -> T:
		return await self.request("GET", path, params=params, rtype=rtype)

	async def post(self, path: str, body=None, rtype: type[T] | Any = None) -> T:
		return await self.request("POST", path, body=body, rtype=rtype)

	async def patch(self, path: str, body=None, rtype: type[T] | Any = None) -> T:
		return await self.request("PATCH", path, body=body, rtype=rtype)

	async def delete(self, path: str, params=None, rtype: type[T] | Any = None) -> T:
		return await self.request("DELETE", path, params=params, rtype=rtype)

	async def pages(self, path: str, params: msgspec.Struct | Mapping[str, Any] | None, page_type: type[T], *,
			max_pages: int | None = None) -> AsyncIterator[T]:
		"""Pages following ``next_page_token`` until the last one (or ``max_pages``)."""
		query = encode_params(params)
		count = 0
		while True:
			page = await self.get(path, query, page_type)
			yield page
			count += 1
			token = getattr(page, "next_page_token", None)
			if not token or (max_pages is not None and count >= max_pages):
				return
			query = {**query, "page_token": token}
