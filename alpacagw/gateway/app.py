"""Start the gateway:

	alpacagw                    # window, ws://127.0.0.1:4102/ (paper)
	alpacagw --live             # config.cfg is a live account: needs --live (the same port, 4102)
	alpacagw --headless         # no window (server, service)
	alpacagw --simulate         # a simulated account: orders stay in the gateway, Alpaca gives the market data
	alpacagw --config other.cfg # another account file than config.cfg

The account's credentials come from config.cfg in the gateway's folder (the project folder, or the
folder of the executable), or from the file given with --config; the gateway only reads it. Port, local-only and data feed come from the
settings (the window's gear), the command line overrides them.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import msgspec

from .. import __version__
from ..client import Alpaca, AlpacaConfig, Credentials, load_config
from ..protocol import PORT
from . import redact, settings as settings_mod, updates as updates_mod
from .barcache import BarCache
from .core import GatewayCore
from .sim import Simulation

CONFIG_FILE = "config.cfg"
TEMPLATE = """\
; Alpaca Gateway: the account. The gateway only reads this file, it never writes credentials.
; Left empty, key_id / secret_key are asked for at every start (and kept in memory only).
; Data feed, socket port and local-only connections are in the gateway's settings (the gear).
[alpaca]
environment = paper        ; paper | live (live also needs: alpacagw --live)
key_id =
secret_key =
"""
LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(name)-18s | %(levelname)-7s | %(message)s"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(prog="alpacagw", description=f"Alpaca Gateway {__version__}")
	p.add_argument("--config", metavar="FILE", help=f"the account file (default: {CONFIG_FILE} in the gateway's folder; "
		"a file given here must exist)")
	p.add_argument("--live", action="store_true", help="required when the account file is a live account, refused for "
		"paper")
	p.add_argument("--host", help="interface to listen on (default from the settings: 127.0.0.1, this machine only)")
	p.add_argument("--port", type=int, help=f"default from the settings, else {PORT} (paper and live)")
	p.add_argument("--headless", action="store_true", help="no window")
	p.add_argument("--simulate", action="store_true", help="a simulated account: orders stay in the gateway and fill "
		"from Alpaca's quotes (also a setting)")
	p.add_argument("--log", default="", help="log file (default: alpacagw-<environment>.log)")
	return p.parse_args(argv)


def setup_logging(path: str) -> None:
	root = logging.getLogger()
	root.setLevel(logging.INFO)
	console = logging.StreamHandler()
	console.setFormatter(logging.Formatter("%(asctime)s %(name)-18s %(levelname)-7s %(message)s", "%H:%M:%S"))
	root.addHandler(console)
	file = RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
	file.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S"))
	root.addHandler(file)
	redact.install(console, file)                # no key, secret or full account number in the log
	logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def program_folder() -> Path:
	"""The gateway's folder: the executable's when built as one (Nuitka / PyInstaller), else the project's."""
	if getattr(sys, "frozen", False) or "__compiled__" in globals():
		return Path(sys.argv[0]).resolve().parent
	return Path(__file__).resolve().parents[2]


def config_path() -> Path:
	return program_folder() / CONFIG_FILE


class Launcher:
	"""Starts the gateway (its own event loop in a thread) once the credentials are there: from
	config.cfg at once, else from the window's login form (kept in memory only), which also picks
	paper or live. Port, local-only and data feed come from that environment's settings; --host /
	--port override them."""

	def __init__(self, config: AlpacaConfig, *, host: str | None, port: int | None, simulate: bool = False) -> None:
		self.config = config
		self.host_arg, self.port_arg, self.simulate_arg = host, port, simulate
		self.environment = config.environment       # the one running (or to start)
		self.core: GatewayCore | None = None
		self.thread: threading.Thread | None = None
		self.error: BaseException | None = None       # why the last start failed
		self.was_ready = False

	def start(self, credentials: Credentials | None = None, environment: str | None = None) -> None:
		self.environment = environment or self.config.environment
		config = msgspec.structs.replace(self.config, environment=self.environment,
			credentials=credentials or self.config.credentials)
		self.core = core = make_core(config, self.host_arg, self.port_arg, self.simulate_arg)
		self.error, self.was_ready = None, False

		def serve() -> None:
			try:
				asyncio.run(core.run())
			except BaseException as exc:
				self.error = exc
				logging.getLogger("alpacagw").error("gateway stopped: %s", exc)

		self.thread = threading.Thread(target=serve, name="gateway", daemon=True)
		self.thread.start()

	def failed_start(self) -> bool:
		"""The gateway thread ended before it was ready: back to the login form (wrong keys, ...)."""
		if self.thread is None or self.thread.is_alive():
			return False
		if self.core is not None and self.core.status.state == "ready":
			self.was_ready = True
		return self.error is not None and not self.was_ready

	def reset(self) -> None:
		self.core = self.thread = None

	def stop(self) -> None:
		if self.core is not None:
			self.core.request_stop()
		if self.thread is not None:
			self.thread.join(timeout=10)


def make_core(config: AlpacaConfig, host: str | None, port: int | None, simulate: bool = False) -> GatewayCore:
	"""The gateway for an account: host, port and data feed from its environment's settings, unless
	given on the command line. ``simulate`` (or the setting): a simulated account, kept next to the settings."""
	log = logging.getLogger("alpacagw")
	if config.credentials is not None:
		redact.credentials(config.credentials.key_id, config.credentials.secret_key)
	settings = settings_mod.load(config.environment)
	host = host or settings.host
	port = port or settings.port_for(config.environment)
	feed = settings.stock_feed or config.stock_feed
	cache = None
	if settings.bar_cache:
		cache = BarCache(settings_mod.bar_cache_folder(), max_mb=max(10, settings.bar_cache_mb))
		log.info("bar cache: %s", cache.file(f"alpaca-{feed}"))
	simulation = None
	if simulate or settings.simulate:
		simulation = Simulation(file=settings_mod.simulation_file(config.environment), cash=settings.sim_cash,
			all_hours=settings.sim_all_hours)
		log.info("simulated account (%s): no order reaches Alpaca", simulation.file)
	log.info("alpacagw %s, %s keys, %s data, ws://%s:%d/, quotes every %d ms, forming bars every %d ms",
		__version__, config.environment, feed, host, port, settings.quote_interval_ms, settings.bar_interval_ms)
	if host not in ("127.0.0.1", "localhost", "::1"):
		log.warning("listening on %s: every computer that reaches port %d can trade this account", host, port)
	return GatewayCore(Alpaca(config, stock_feed=feed), host=host, port=port, simulation=simulation, bar_cache=cache,
		quote_interval=max(10, settings.quote_interval_ms) / 1000, bar_interval=max(10, settings.bar_interval_ms) / 1000)


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv)
	if args.config:                              # another account file: it must be there (a typo starts nothing)
		given = Path(args.config).expanduser()
		cfg = given.resolve()
		if not cfg.is_file() and not given.is_absolute() and (program_folder() / given).is_file():
			cfg = (program_folder() / given).resolve()   # a name next to config.cfg (a shortcut's own start folder)
		if not cfg.is_file():
			print(f"alpacagw: account file {cfg} not found", file=sys.stderr)
			return 2
	else:
		cfg = config_path()
	if not cfg.is_file():
		cfg.write_text(TEMPLATE, encoding="utf-8")
		print(f"alpacagw: {cfg} did not exist: a template was written. Credentials left empty there are asked "
			"for at start (and not stored).", file=sys.stderr)
	try:
		config = load_config(cfg, require_credentials=False)
	except ValueError as exc:
		print(f"alpacagw: {exc}", file=sys.stderr)
		return 2
	if config.credentials is not None:           # the account is fixed by the file: --live must match it
		if config.is_live and not args.live:
			print(f"alpacagw: {cfg} is a live account: start with --live to trade it", file=sys.stderr)
			return 2
		if args.live and not config.is_live:
			print(f"alpacagw: --live given, but {cfg} is a {config.environment} account", file=sys.stderr)
			return 2
	elif args.live:
		config = msgspec.structs.replace(config, environment="live")    # the login form's default
	setup_logging(args.log or "alpacagw.log")
	logging.getLogger("alpacagw").info("account file %s%s", cfg, "" if config.credentials else " (no credentials)")

	update_check = updates_mod.UpdateCheck()
	if settings_mod.load(config.environment).check_updates:
		update_check.start()                     # in the background; the window shows a newer one, headless logs it
	if args.headless:
		if config.credentials is None:
			print(f"alpacagw: no credentials in {cfg}: fill in key_id and secret_key (headless, nobody to ask)",
				file=sys.stderr)
			return 2
		return _run_headless(make_core(config, args.host, args.port, args.simulate))

	try:
		from .window import GatewayWindow
	except ImportError:
		print("alpacagw: the window needs imgui-bundle (pip install alpacagw[ui]); or use --headless", file=sys.stderr)
		return 2
	launcher = Launcher(config, host=args.host, port=args.port, simulate=args.simulate)
	if config.credentials is not None:
		launcher.start()                         # else the window asks for them first
	GatewayWindow(launcher, update_check).run()
	launcher.stop()
	return 1 if launcher.error is not None and not launcher.was_ready else 0


def _run_headless(core: GatewayCore) -> int:
	async def run() -> None:
		loop = asyncio.get_running_loop()
		for sig in (signal.SIGINT, signal.SIGTERM):
			try:
				loop.add_signal_handler(sig, core.request_stop)
			except (NotImplementedError, RuntimeError):
				pass                     # Windows: Ctrl+C raises KeyboardInterrupt instead
		await core.run()

	try:
		asyncio.run(run())
	except KeyboardInterrupt:
		pass
	except Exception as exc:
		logging.getLogger("alpacagw").error("gateway stopped: %s", exc)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
