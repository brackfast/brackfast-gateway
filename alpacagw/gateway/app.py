"""Start the gateway:

	alpacagw --config paper.cfg                 # window, ws://127.0.0.1:4102/
	alpacagw --config live.cfg --live           # a live account needs --live (port 4101)
	alpacagw --config paper.cfg --headless      # no window (server, service)
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

from .. import __version__
from ..client import Alpaca, load_config
from ..protocol import LIVE_PORT, PAPER_PORT
from .core import GatewayCore

LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(name)-18s | %(levelname)-7s | %(message)s"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(prog="alpacagw", description=f"Alpaca Gateway {__version__}")
	p.add_argument("--config", default="paper.cfg", help="account config file (default: paper.cfg)")
	p.add_argument("--live", action="store_true", help="required for a live account, refused for paper")
	p.add_argument("--host", default="127.0.0.1", help="interface to listen on (default 127.0.0.1: this machine only)")
	p.add_argument("--port", type=int, help=f"default {PAPER_PORT} paper, {LIVE_PORT} live")
	p.add_argument("--token", default="", help="clients must send this token in hello (use it with --host 0.0.0.0)")
	p.add_argument("--headless", action="store_true", help="no window")
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
	logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv)
	try:
		config = load_config(args.config)
	except (FileNotFoundError, ValueError) as exc:
		print(f"alpacagw: {exc}", file=sys.stderr)
		return 2
	if config.is_live and not args.live:
		print(f"alpacagw: {args.config} is a live account: start with --live to trade it", file=sys.stderr)
		return 2
	if args.live and not config.is_live:
		print(f"alpacagw: --live given, but {args.config} is a {config.environment} account", file=sys.stderr)
		return 2
	if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
		print("alpacagw: listening beyond this machine needs --token", file=sys.stderr)
		return 2
	setup_logging(args.log or f"alpacagw-{config.environment}.log")
	port = args.port or (LIVE_PORT if config.is_live else PAPER_PORT)
	core = GatewayCore(Alpaca(config), host=args.host, port=port, token=args.token)
	log = logging.getLogger("alpacagw")
	log.info("alpacagw %s, %s account from %s", __version__, config.environment, Path(args.config).resolve())

	if args.headless:
		return _run_headless(core)

	try:
		from .window import GatewayWindow
	except ImportError:
		print("alpacagw: the window needs imgui-bundle (pip install alpacagw[ui]); or use --headless", file=sys.stderr)
		return 2
	failed: list[BaseException] = []

	def serve() -> None:
		try:
			asyncio.run(core.run())
		except BaseException as exc:
			failed.append(exc)
			log.error("gateway stopped: %s", exc)

	thread = threading.Thread(target=serve, name="gateway", daemon=True)
	thread.start()
	GatewayWindow(core, thread).run()
	core.request_stop()
	thread.join(timeout=10)
	return 1 if failed else 0


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
