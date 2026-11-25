"""BarCache: historical bars kept on disk, so a history request asks the data source only for what is missing.

One SQLite file per data source in the cache folder (``alpaca-iex.sqlite``, ``alpaca-sip.sqlite``, ...): the sources'
bars differ, and one source's cache can be looked at, cleared or deleted without the others. It knows nothing of
Alpaca: ``get`` is given a ``fetch`` function for the source.

What is kept:

- Bars per symbol and timeframe, and the time ranges that are **covered**: asked from the source completely. A
  covered range without bars is a night, a weekend or a quiet minute, not a gap to ask for again.
- Only settled bars: complete for ``SETTLE`` (15 minutes; the source may still correct a newer one). The newest bars
  of a request are always asked from the source.
- A request walks back from its end: covered ranges come from the file, each gap between them is one request to the
  source (the newest ``limit`` bars up to ``end``, as without the cache).

Adjusted prices (Alpaca's bars are split adjusted) change for all of a symbol's history at a split. A gap's request
therefore also asks for the newest cached bar before it again; when its prices are no longer the cached ones, the
symbol's cache is dropped and the request answered from the source.

A file above ``max_mb`` loses the symbols and timeframes used longest ago when it is opened.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ..protocol import BarData

SETTLE = timedelta(minutes=15)
Fetch = Callable[[datetime, datetime, int], Awaitable[list[BarData]]]
"""(start, end, limit) -> the newest ``limit`` bars with start <= ts <= end, newest first; fewer: there are no more."""

_SCHEMA = """
create table if not exists series (id integer primary key, symbol text, timeframe text, used real,
	unique (symbol, timeframe));
create table if not exists bars (series integer, ts integer, open real, high real, low real, close real, volume real,
	primary key (series, ts)) without rowid;
create table if not exists covered (series integer, lo integer, hi integer);
create index if not exists covered_series on covered (series, hi);
"""


def _ts(t: datetime) -> int:
	return int(t.timestamp())


def _bar(row: tuple) -> BarData:
	return BarData(datetime.fromtimestamp(row[0], timezone.utc), *row[1:])


def _same(a: float, b: float) -> bool:
	return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1.0)


class BarCache:
	def __init__(self, folder: Path, *, max_mb: int = 500, enabled: bool = True, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
			log: logging.Logger | None = None) -> None:
		self.folder = folder
		self.max_mb = max_mb
		self.enabled = enabled                      # off: history is not read from or written to the files (they can
		                                            # still be listed and cleared)
		self.clock = clock
		self.log = log or logging.getLogger("alpacagw.barcache")
		self._files: dict[str, sqlite3.Connection] = {}
		self._lock = threading.Lock()                # one thread in SQLite at a time (the calls run in worker threads)
		self.from_cache = 0                         # bars answered from the files / asked from the source, this run
		self.from_source = 0

	# -- files ----------------------------------------------------------------------

	def file(self, source: str) -> Path:
		return self.folder / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', source)}.sqlite"

	def sources(self) -> list[str]:
		"""The data sources that have a file in the folder."""
		try:
			return sorted(p.stem for p in self.folder.glob("*.sqlite"))
		except OSError:
			return []

	def size(self, source: str) -> int:
		"""The source's file in bytes (0: none), with what SQLite has not moved into it yet (its -wal file)."""
		total = 0
		for file in (self.file(source), self.file(source).with_name(self.file(source).name + "-wal")):
			try:
				total += file.stat().st_size
			except OSError:
				pass
		return total

	def _db(self, source: str) -> sqlite3.Connection:
		db = self._files.get(source)
		if db is None:
			self.folder.mkdir(parents=True, exist_ok=True)
			file = self.file(source)
			try:
				db = self._open(file)
			except sqlite3.DatabaseError as exc:    # not a database (damaged): a new one, it is only a cache
				self.log.warning("%s unreadable (%s): a new bar cache", file, exc)
				file.unlink(missing_ok=True)
				db = self._open(file)
			self._files[source] = db
			self._prune(source, db)
		return db

	@staticmethod
	def _open(file: Path) -> sqlite3.Connection:
		db = sqlite3.connect(file, check_same_thread=False, timeout=10.0)
		try:
			db.execute("pragma journal_mode = wal")  # a paper and a live gateway may share a source's file
			db.execute("pragma synchronous = normal")
			db.executescript(_SCHEMA)
			db.commit()
		except sqlite3.Error:
			db.close()                               # else the file stays open and can not be replaced
			raise
		return db

	def _prune(self, source: str, db: sqlite3.Connection) -> None:
		"""Above ``max_mb``: the series used longest ago go, down to 80 % of it."""
		page = db.execute("pragma page_size").fetchone()[0]
		used = lambda: (db.execute("pragma page_count").fetchone()[0] - db.execute("pragma freelist_count").fetchone()[0]) * page
		limit = self.max_mb * 1024 * 1024
		if used() <= limit:
			return
		dropped = 0
		for (series,) in db.execute("select id from series order by used").fetchall():
			if used() <= 0.8 * limit:
				break
			self._drop(db, series)
			db.commit()
			dropped += 1
		db.execute("vacuum")
		self.log.info("bar cache %s was above %d MB: %d series used longest ago removed", source, self.max_mb, dropped)

	@staticmethod
	def _drop(db: sqlite3.Connection, series: int) -> None:
		for table, column in (("bars", "series"), ("covered", "series"), ("series", "id")):
			db.execute(f"delete from {table} where {column} = ?", (series,))

	def close(self) -> None:
		with self._lock:
			for db in self._files.values():
				db.close()
			self._files.clear()

	def clear(self, source: str, *, remove: bool = False) -> None:
		"""The source's cache emptied: its file stays, small; ``remove``: the file goes too (a source no longer
		used; another gateway that has it open keeps an empty one)."""
		with self._lock:
			db = self._db(source)
			for table in ("bars", "covered", "series"):
				db.execute(f"delete from {table}")
			db.commit()
			db.execute("vacuum")
			if remove:
				db.close()
				del self._files[source]
				file = self.file(source)
				for path in (file, file.with_name(file.name + "-wal"), file.with_name(file.name + "-shm")):
					try:
						path.unlink(missing_ok=True)
					except OSError:
						pass
		self.log.info("bar cache %s %s", source, "removed" if remove else "cleared")

	# -- reading / writing (in a worker thread, under the lock) -----------------------

	def _series(self, db: sqlite3.Connection, symbol: str, timeframe: str) -> int:
		row = db.execute("select id from series where symbol = ? and timeframe = ?", (symbol, timeframe)).fetchone()
		if row is None:
			return db.execute("insert into series (symbol, timeframe, used) values (?, ?, ?)",
				(symbol, timeframe, time.time())).lastrowid
		db.execute("update series set used = ? where id = ?", (time.time(), row[0]))
		return row[0]

	def _look(self, source: str, symbol: str, timeframe: str, start: int, t: int, need: int):
		"""From ``t`` back: ("bars", rows newest first, where they end) when ``t`` lies in a covered range, else
		("gap", its lower end, the newest cached bar just below it or None)."""
		with self._lock:
			db = self._db(source)
			series = self._series(db, symbol, timeframe)
			db.commit()
			row = db.execute("select min(lo) from covered where series = ? and lo < ? and hi >= ?", (series, t, t)).fetchone()
			if row[0] is not None:
				lo = max(row[0], start)
				rows = db.execute("select ts, open, high, low, close, volume from bars where series = ? and ts >= ? "
					"and ts < ? order by ts desc limit ?", (series, lo, t, need)).fetchall()
				return "bars", rows, lo
			below = db.execute("select max(hi) from covered where series = ? and hi < ?", (series, t)).fetchone()[0]
			if below is None or below <= start:
				return "gap", start, None
			# the newest bar of the covered range that ends there (not an older range's: what lies between is unknown)
			range_lo = db.execute("select min(lo) from covered where series = ? and hi = ?", (series, below)).fetchone()[0]
			anchor = db.execute("select ts, open, high, low, close, volume from bars where series = ? and ts >= ? "
				"and ts < ? order by ts desc limit 1", (series, range_lo, below)).fetchone()
			return "gap", below, anchor

	def _store(self, source: str, symbol: str, timeframe: str, bars: list[BarData], lo: int, hi: int) -> None:
		"""Settled bars of a gap's answer, and [lo, hi) as covered (merged with the ranges it touches)."""
		if hi <= lo:
			return
		with self._lock:
			db = self._db(source)
			series = self._series(db, symbol, timeframe)
			db.executemany("insert or replace into bars values (?, ?, ?, ?, ?, ?, ?)",
				[(series, _ts(b.ts), b.open, b.high, b.low, b.close, b.volume) for b in bars if lo <= _ts(b.ts) < hi])
			touching = db.execute("select min(lo), max(hi) from covered where series = ? and lo <= ? and hi >= ?",
				(series, hi, lo)).fetchone()
			if touching[0] is not None:
				db.execute("delete from covered where series = ? and lo <= ? and hi >= ?", (series, hi, lo))
				lo, hi = min(lo, touching[0]), max(hi, touching[1])
			db.execute("insert into covered values (?, ?, ?)", (series, lo, hi))
			db.commit()

	def _forget(self, source: str, symbol: str) -> None:
		with self._lock:
			db = self._db(source)
			for (series,) in db.execute("select id from series where symbol = ?", (symbol,)).fetchall():
				self._drop(db, series)
			db.commit()

	# -- the request ------------------------------------------------------------------

	async def get(self, source: str, symbol: str, timeframe: str, step: timedelta, start: datetime, end: datetime,
			limit: int, fetch: Fetch, *, aligned: bool = False) -> list[BarData]:
		"""The newest ``limit`` bars with start <= ts <= end, newest first: from the source's file where it is
		covered, else from ``fetch`` (whose settled bars are kept). ``aligned``: the source's bars of this timeframe
		start at multiples of ``step`` (UTC), so the time from ``end`` to the next multiple holds no bar and counts
		as covered too; without it, a request ending just before an earlier one's oldest bar (a chart scrolled back)
		leaves a sliver between the two that costs a request of its own."""
		for attempt in (1, 2):
			try:
				return await self._get(source, symbol, timeframe, step, start, end, limit, fetch, aligned)
			except _Adjusted:
				await asyncio.to_thread(self._forget, source, symbol)
				self.log.info("%s %s: the source's prices are not the cached ones (a split?): its cache dropped", source,
					symbol)
			except sqlite3.Error as exc:            # the cache must never cost a chart its bars
				self.log.warning("bar cache %s: %s; asking the source", source, exc)
				break
		return await fetch(start, end, limit)

	async def _get(self, source: str, symbol: str, timeframe: str, step: timedelta, start: datetime, end: datetime,
			limit: int, fetch: Fetch, aligned: bool) -> list[BarData]:
		settled = _ts(self.clock() - step - SETTLE)  # bars before it are complete and no longer corrected
		lo_end, t = _ts(start), _ts(end) + 1         # [lo_end, t): the range still to answer, walking back
		if aligned:
			grid = int(step.total_seconds())
			t = -(-t // grid) * grid                 # up to the next bar's start: none can lie between
		out: list[BarData] = []
		while len(out) < limit and t > lo_end:
			need = limit - len(out)
			# above ``settled`` nothing is covered: the request's newest part is a gap in any case
			kind, a, b = await asyncio.to_thread(self._look, source, symbol, timeframe, lo_end, t, need)
			if kind == "bars":
				out += [_bar(r) for r in a]
				self.from_cache += len(a)
				if len(a) == need:
					break
				t = b
				continue
			gap_lo, anchor = a, b
			ask_lo = anchor[0] if anchor is not None else gap_lo
			got = await fetch(datetime.fromtimestamp(ask_lo, timezone.utc), datetime.fromtimestamp(t - 1, timezone.utc),
				need + (1 if anchor is not None else 0))
			exhausted = len(got) < need + (1 if anchor is not None else 0)
			if anchor is not None:
				if exhausted:                        # everything down to the anchor came: it must be the cached bar
					again = got[-1] if got and _ts(got[-1].ts) == anchor[0] else None
					if again is None or not (_same(again.open, anchor[1]) and _same(again.close, anchor[4])):
						raise _Adjusted
				got = [bar for bar in got[:need] if _ts(bar.ts) >= gap_lo]
			self.from_source += len(got)
			covered_lo = gap_lo if exhausted else _ts(got[-1].ts)
			await asyncio.to_thread(self._store, source, symbol, timeframe, got, covered_lo, min(t, settled))
			out += got
			if not exhausted:
				break
			t = gap_lo
		return out[:limit]


class _Adjusted(Exception):
	"""The source's bar is not the cached one any more."""
