"""Endpoints, credentials and the config file, one file per account.

Keep paper and live credentials in separate files (``paper.cfg``, ``live.cfg``)::

	[alpaca]
	environment = paper        ; paper | live (required)
	key_id = PK...
	secret_key = ...
	stock_feed = iex           ; optional: iex (default, free) | sip | delayed_sip
"""
from __future__ import annotations

import configparser
import logging
import os
import stat
from pathlib import Path

import msgspec

log = logging.getLogger("alpacagw.config")


class Endpoints(msgspec.Struct, frozen=True, kw_only=True):
	trading: str                          # Trading API REST
	data: str                             # Market Data REST
	trading_stream: str                   # trade_updates websocket
	data_stream: str                      # market data websocket (feed appended)


PAPER = Endpoints(
	trading="https://paper-api.alpaca.markets",
	data="https://data.alpaca.markets",
	trading_stream="wss://paper-api.alpaca.markets/stream",
	data_stream="wss://stream.data.alpaca.markets",
)

LIVE = Endpoints(
	trading="https://api.alpaca.markets",
	data="https://data.alpaca.markets",
	trading_stream="wss://api.alpaca.markets/stream",
	data_stream="wss://stream.data.alpaca.markets",
)

ENVIRONMENTS = {"paper": PAPER, "live": LIVE}

FEEDS = ("iex", "sip", "delayed_sip")


class Credentials:
	"""API key pair. The secret never shows up in repr / logs."""

	__slots__ = ("key_id", "secret_key")

	def __init__(self, key_id: str, secret_key: str):
		self.key_id = key_id
		self.secret_key = secret_key

	@classmethod
	def from_env(cls, key_var: str = "APCA_API_KEY_ID", secret_var: str = "APCA_API_SECRET_KEY") -> Credentials:
		try:
			return cls(os.environ[key_var], os.environ[secret_var])
		except KeyError as exc:
			raise RuntimeError(f"environment variable {exc.args[0]} is not set") from None

	def headers(self) -> dict[str, str]:
		return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret_key}

	def auth_message(self) -> dict[str, str]:
		"""The websocket authentication message (market data and trading streams)."""
		return {"action": "auth", "key": self.key_id, "secret": self.secret_key}

	def __repr__(self) -> str:
		return f"Credentials(key_id={self.key_id!r})"


class AlpacaConfig(msgspec.Struct, kw_only=True):
	environment: str                      # "paper" | "live"
	credentials: Credentials
	stock_feed: str = "iex"
	path: Path | None = None

	@property
	def endpoints(self) -> Endpoints:
		return ENVIRONMENTS[self.environment]

	@property
	def is_live(self) -> bool:
		return self.environment == "live"


def _value(section: configparser.SectionProxy, key: str) -> str | None:
	value = section.get(key, "").strip()
	if not value or value.startswith("<"):    # empty or still the template's placeholder
		return None
	return value


def load_config(path: str | os.PathLike) -> AlpacaConfig:
	"""Read one account's config file."""
	cfg_path = Path(path).expanduser()
	if not cfg_path.is_file():
		raise FileNotFoundError(f"Alpaca config not found: {cfg_path.resolve()}")
	if os.name == "posix" and cfg_path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
		log.warning("%s is readable by other users; consider: chmod 600 %s", cfg_path, cfg_path)

	parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
	parser.read(cfg_path, encoding="utf-8")
	if not parser.has_section("alpaca"):
		raise ValueError(f"{cfg_path}: missing section [alpaca]")
	section = parser["alpaca"]

	environment = section.get("environment", "").strip().lower()
	if environment not in ENVIRONMENTS:
		raise ValueError(f"{cfg_path}: environment must be one of {', '.join(ENVIRONMENTS)} (got {environment!r})")
	key_id, secret_key = _value(section, "key_id"), _value(section, "secret_key")
	if not (key_id and secret_key):
		raise ValueError(f"{cfg_path}: key_id and secret_key are required")
	feed = (_value(section, "stock_feed") or "iex").lower()
	if feed not in FEEDS:
		raise ValueError(f"{cfg_path}: stock_feed must be one of {', '.join(FEEDS)} (got {feed!r})")

	return AlpacaConfig(environment=environment, credentials=Credentials(key_id, secret_key),
		stock_feed=feed, path=cfg_path)
