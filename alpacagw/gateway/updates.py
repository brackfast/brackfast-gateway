"""Check for updates: is there a newer Alpaca Gateway on the website than the one running?

The website's Download page lists its files in ``download/index.json`` (name, size, SHA-256). The check reads the
same file and looks for the gateway's archives for this system: ``alpaca-gateway-<version>-<date>-<system>.<ext>``
(``windows-x86_64``, ``linux-x86_64``, ...). Nothing is sent but the request itself (the version in the
User-Agent): no account, no key. A failed check says so in Settings -> About and in the log, and is otherwise quiet.
As Brackfast's ``updates.py``.
"""
from __future__ import annotations

import json
import logging
import platform
import re
import sys
import threading
import urllib.request
from datetime import datetime, timezone

from .. import __version__

INDEX_URL = "https://brackfast.io/download/index.json"           # the website's download index
DOWNLOAD_PAGE = "https://brackfast.io/download.html"             # where the user downloads it
PRODUCT = "alpaca-gateway"
TIMEOUT = 10.0
_MACHINES = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
# as tools/build_dist.py names the archives: <system>-<machine>
SYSTEM = (f"{'windows' if sys.platform == 'win32' else sys.platform}-"
	f"{_MACHINES.get(platform.machine().lower(), platform.machine().lower())}")

log = logging.getLogger("alpacagw.updates")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?")
_STAGES = {"a": 0, "b": 1, "rc": 2}

IDLE, CHECKING, NEWEST, AVAILABLE, FAILED = "idle", "checking", "newest", "available", "failed"


def version_key(version: str) -> tuple | None:
	"""Orders versions: 0.2.0.dev3 < 0.2.0a1 < 0.2.0b1 < 0.2.0rc1 < 0.2.0 (None: not a version)."""
	m = _VERSION.match(version)
	if m is None:
		return None
	major, minor, patch, stage, number, dev = m.groups()
	if stage:
		rank, n = _STAGES[stage], int(number)
	elif dev is not None:
		rank, n = -1, int(dev)                   # a development version comes before its release
	else:
		rank, n = 3, 0
	return int(major), int(minor), int(patch), rank, n


def is_prerelease(version: str) -> bool:
	key = version_key(version)
	return key is not None and key[3] < 3


def newest(names: list[str], product: str = PRODUCT, system: str = SYSTEM, prereleases: bool = False) -> str | None:
	"""The newest version among the download names for ``product`` on ``system`` (pre-releases only if asked)."""
	pattern = re.compile(rf"^{re.escape(product)}-(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)-\d{{8}}-{re.escape(system)}\.")
	best: tuple | None = None
	found = None
	for name in names:
		m = pattern.match(name)
		if m is None:
			continue
		key = version_key(m.group(1))
		if key is None or (key[3] < 3 and not prereleases):
			continue
		if best is None or key > best:
			best, found = key, m.group(1)
	return found


class UpdateCheck:
	"""One check at a time, in a thread of its own; the window reads ``state``, ``latest`` and ``text``."""

	def __init__(self, url: str = INDEX_URL, current: str = __version__, fetch=None, system: str = SYSTEM) -> None:
		self.url = url
		self.current = current
		self.system = system
		self.state = IDLE
		self.latest = ""                          # the newer version, when AVAILABLE
		self.text = ""                            # for Settings -> About
		self.checked: datetime | None = None
		self._fetch = fetch or self._get
		self._lock = threading.Lock()

	def start(self) -> None:
		"""Check in the background (a second start while one runs: nothing)."""
		with self._lock:
			if self.state == CHECKING:
				return
			self.state, self.text = CHECKING, "checking ..."
		threading.Thread(target=self.run, name="update check", daemon=True).start()

	def run(self) -> None:
		try:
			index = json.loads(self._fetch(self.url))
			names = [f.get("name", "") for f in index.get("files", []) if isinstance(f, dict)]
		except Exception as exc:                  # unreachable, no such file, not JSON
			self._done(FAILED, "", f"The update server was not reached ({type(exc).__name__}).")
			log.info("update check: %s not reached: %s", self.url, exc)
			return
		latest = newest(names, PRODUCT, self.system, prereleases=is_prerelease(self.current))
		current = version_key(self.current)
		if latest and current is not None and version_key(latest) > current:
			self._done(AVAILABLE, latest, f"Alpaca Gateway {latest} is available (this is {self.current}).")
			log.info("update check: Alpaca Gateway %s is available (running %s): %s", latest, self.current, DOWNLOAD_PAGE)
		else:
			self._done(NEWEST, "", f"Alpaca Gateway {self.current} is the newest version.")

	def _done(self, state: str, latest: str, text: str) -> None:
		with self._lock:
			self.state, self.latest, self.text = state, latest, text
			self.checked = datetime.now(timezone.utc)

	def _get(self, url: str) -> bytes:
		request = urllib.request.Request(url, headers={"User-Agent": f"AlpacaGateway/{self.current}",
			"Accept": "application/json"})
		with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
			return response.read(1_000_000)
