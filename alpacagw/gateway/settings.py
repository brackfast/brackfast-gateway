"""Gateway settings from the window's settings dialog, one set per environment (paper / live).

Kept as JSON in the user's config folder (%APPDATA%\\alpacagw\\settings.json on Windows). No
credentials: those stay in the account's config file, which the gateway only reads. Command line
options (--port, --host) override these settings.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import msgspec

from ..protocol import LIVE_PORT, PAPER_PORT

log = logging.getLogger("alpacagw.settings")

DEFAULT_PORTS = {"paper": PAPER_PORT, "live": LIVE_PORT}


class GatewaySettings(msgspec.Struct, kw_only=True):
	stock_feed: str = ""                     # "" = the account config's stock_feed
	port: int = 0                            # 0 = the default port of the environment
	local_only: bool = True                  # listen on 127.0.0.1 only, else on all interfaces
	quote_interval_ms: int = 100             # quotes to the clients at most this often per symbol (the newest)
	bar_interval_ms: int = 250               # the forming 1m bar at most this often per symbol, when it changed

	def port_for(self, environment: str) -> int:
		return self.port or DEFAULT_PORTS[environment]

	@property
	def host(self) -> str:
		return "127.0.0.1" if self.local_only else "0.0.0.0"


def path() -> Path:
	if sys.platform == "win32":
		base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
	elif sys.platform == "darwin":
		base = Path.home() / "Library" / "Application Support"
	else:
		base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
	return base / "alpacagw" / "settings.json"


def _all(file: Path) -> dict[str, GatewaySettings]:
	if not file.is_file():
		return {}
	try:
		return msgspec.json.decode(file.read_bytes(), type=dict[str, GatewaySettings])
	except (OSError, msgspec.DecodeError, msgspec.ValidationError) as exc:
		log.warning("%s unreadable (%s): defaults used", file, exc)
		return {}


def load(environment: str, file: Path | None = None) -> GatewaySettings:
	return _all(file or path()).get(environment, GatewaySettings())


def save(environment: str, settings: GatewaySettings, file: Path | None = None) -> None:
	file = file or path()
	everything = _all(file)
	everything[environment] = settings
	file.parent.mkdir(parents=True, exist_ok=True)
	file.write_bytes(msgspec.json.format(msgspec.json.encode(everything)))
