"""Gateway settings from the window's settings dialog, one set per environment (paper / live).

Kept as JSON in the user's config folder (%APPDATA%\\brackfastgw\\settings.json on Windows). No
credentials: those stay in the account's config file, which the gateway only reads. Command line
options (--port, --host) override these settings.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import msgspec

from ..protocol import PORT

log = logging.getLogger("brackfastgw.settings")


SOURCES = {"alpaca": "Alpaca", "yahoo": "Yahoo Finance (unofficial)"}     # data_source -> its name in the window


# settings only some data sources have (the others: every source); the window shows a setting only for its sources
ONLY = {
	"stock_feed": ("alpaca",),               # Alpaca's feeds
	"simulate": ("alpaca",),                 # a choice only where there is a real account; else always simulated
}


def applies(setting: str, source: str) -> bool:
	"""Whether a data source has this setting."""
	return source in ONLY.get(setting, (source,))


class GatewaySettings(msgspec.Struct, kw_only=True):
	data_source: str = "alpaca"              # where the market data comes from: "alpaca" (with its account) | "yahoo"
	                                         # (yahoo.py: no account, no keys; the account is simulated)
	stock_feed: str = ""                     # "" = the account config's stock_feed
	port: int = 0                            # 0 = the default port (4102, paper and live)
	local_only: bool = True                  # listen on 127.0.0.1 only, else on all interfaces
	quote_interval_ms: int = 100             # quotes to the clients at most this often per symbol (the newest)
	bar_interval_ms: int = 250               # the forming 1m bar at most this often per symbol, when it changed
	check_updates: bool = True               # at start: a newer version on the website? (updates.py)
	bar_cache: bool = True                   # historical bars kept on disk (barcache.py), a file per data source
	bar_cache_mb: int = 500                  # a source's file above it loses what was used longest ago (at start)
	simulate: bool = False                   # a simulated account (sim.py): Alpaca gives the market data only
	sim_cash: float = 100_000.0              # of a new simulated account, and after its reset
	sim_all_hours: bool = False              # simulated fills whenever there is a quote, not in regular hours only

	def port_for(self, environment: str) -> int:
		return self.port or PORT

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
	return base / "brackfastgw" / "settings.json"


# Across versions (the file is in the user's folder: a new gateway finds the old one's): a value that does not fit
# (a newer version's, a damaged file) costs only that setting, not every setting of both environments; what this
# version does not know is kept and saved again (back to an older gateway loses nothing of a newer one's); saving one
# environment keeps the other as it is; written through a temporary file (a crash leaves the old file).

OLD_FOLDER = "alpacagw"                       # the folder's name up to the rename to the Brackfast Gateway (2026-11)


def take_over_old_folder(folder: Path | None = None) -> bool:
	"""The Alpaca Gateway's folder (``alpacagw``, next to ours) taken over once, at start: while ours does not exist
	yet, its files (settings, simulated accounts, older settings' copies) are copied, the bar cache (it can be large)
	moved; an Alpaca Gateway started again later finds its settings, rebuilds its cache. True when taken over."""
	folder = folder or path().parent
	old = folder.with_name(OLD_FOLDER)
	if folder.exists() or not old.is_dir():
		return False
	import shutil
	tmp = folder.with_name(folder.name + ".new")              # complete or not at all: a crash midway starts over
	shutil.rmtree(tmp, ignore_errors=True)
	tmp.mkdir(parents=True)
	for item in old.iterdir():
		if item.is_file():
			shutil.copy2(item, tmp / item.name)
	os.replace(tmp, folder)
	cache = old / "bar-cache"
	if cache.is_dir():
		try:
			shutil.move(cache, folder / "bar-cache")
		except OSError as exc:                                 # in use (an Alpaca Gateway running): fetched anew
			log.warning("bar cache not moved from %s: %s", cache, exc)
	log.info("settings taken over from %s (the Alpaca Gateway's) into %s", old, folder)
	return True


def bar_cache_folder() -> Path:
	"""Where the bar cache's files are (one per data source), next to the settings."""
	return path().with_name("bar-cache")


def simulation_file(environment: str) -> Path:
	"""Where the simulated account of an environment's gateway is kept (next to the settings)."""
	return path().with_name(f"simulated-account-{environment}.json")


def _raw(file: Path) -> dict:
	"""The file as it is (both environments, unknown settings included); not JSON: kept aside, nothing."""
	if not file.is_file():
		return {}
	try:
		data = msgspec.json.decode(file.read_bytes())
		if not isinstance(data, dict):
			raise ValueError("not a JSON object")
		return data
	except (OSError, ValueError, msgspec.DecodeError) as exc:
		try:
			copy = file.with_name("settings.unreadable.json")
			if not copy.exists():
				copy.write_bytes(file.read_bytes())
		except OSError:
			pass
		log.warning("%s unreadable (%s): defaults used, the file kept as settings.unreadable.json", file, exc)
		return {}


def _convert(data: dict) -> GatewaySettings:
	"""One environment's settings; a value that does not fit gets its default (the others stay)."""
	try:
		return msgspec.convert(data, GatewaySettings)
	except msgspec.ValidationError:
		pass
	values = {}
	for f in msgspec.structs.fields(GatewaySettings):
		if f.encode_name in data:
			try:
				values[f.name] = msgspec.convert(data[f.encode_name], f.type)
			except msgspec.ValidationError:
				log.warning("setting %s did not fit (%r): the default", f.name, data[f.encode_name])
	return GatewaySettings(**values)


def load(environment: str, file: Path | None = None) -> GatewaySettings:
	entry = _raw(file or path()).get(environment)
	return _convert(entry) if isinstance(entry, dict) else GatewaySettings()


def save(environment: str, settings: GatewaySettings, file: Path | None = None) -> None:
	file = file or path()
	everything = _raw(file)
	old = everything.get(environment)
	everything[environment] = {**(old if isinstance(old, dict) else {}), **msgspec.to_builtins(settings)}
	file.parent.mkdir(parents=True, exist_ok=True)
	tmp = file.with_name(file.name + ".tmp")
	tmp.write_bytes(msgspec.json.format(msgspec.json.encode(everything)))
	os.replace(tmp, file)
