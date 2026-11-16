"""Build the Alpaca Gateway as a folder that runs with one click, for Windows and Linux:

	uv run --no-project tools/build_dist.py               # both systems
	uv run --no-project tools/build_dist.py --windows     # one of them (--linux)
	uv run --no-project tools/build_dist.py --dir         # Windows as a folder (dist/alpaca-gateway), for testing

-> dist/alpaca-gateway-<version>-windows-x86_64.zip and dist/alpaca-gateway-<version>-linux-x86_64.tar.xz, each with
one folder alpaca-gateway/:

	Alpaca Gateway.cmd / alpaca-gateway    the launcher (double-click)
	alpacagw/                              the gateway, as Python source
	python/                                a standalone Python (python-build-standalone, as uv's) with the
	                                       dependencies in its site-packages (versions from uv.lock)
	LICENSE, README.md, PROTOCOL.md, README.txt

No compiled program: the gateway stays readable Python. Both archives are built on any system: the Python comes
from its release archive (checked against its SHA-256), the wheels through uv for the target platform
(``uv pip install --python-platform``). config.cfg is written next to the launcher at the first start.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "cache"
NAME = "alpaca-gateway"
PYTHON = "3.13"
PBS = "https://github.com/astral-sh/python-build-standalone/releases/download/20260203/"
TARGETS = {
	"windows": {
		"python": PBS + "cpython-3.13.12%2B20260203-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
		"sha256": "95065b15468cc977a9000256b99e7408a6a9cb35ad2711873ba7b621ee37df19",
		"platform": "x86_64-pc-windows-msvc",
		"site_packages": "python/Lib/site-packages",
		"archive": "windows-x86_64.zip",
	},
	"linux": {
		"python": PBS + "cpython-3.13.12%2B20260203-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
		"sha256": "7f1340417839331260dd8ecc309a4f0d2acac1123f3fb7f76600cf52a53a4ef6",
		"platform": "x86_64-manylinux_2_28",           # glibc 2.28 or newer (Debian 10, Ubuntu 20.04, RHEL 8)
		"site_packages": f"python/lib/python{PYTHON}/site-packages",
		"archive": "linux-x86_64.tar.xz",
	},
}
# Parts of the Python not needed by the gateway (the window is imgui, not tkinter)
SKIP_PYTHON = ("idlelib/", "tkinter/", "turtledemo/", "ensurepip/", "test/", "site-packages/pip", "tcl/",
	"lib/tcl", "lib/tk", "lib/itcl", "lib/thread", "Tools/", "bin/idle", "bin/pip", "Scripts/")
DOCS = ("LICENSE", "README.md", "PROTOCOL.md")

WINDOWS_LAUNCHER = """@echo off
rem Starts the Alpaca Gateway with the Python in this folder; pythonw: no console window stays open.
cd /d "%~dp0"
start "" "%~dp0python\\pythonw.exe" -E -s -m alpacagw %*
"""
LINUX_LAUNCHER = """#!/bin/sh
# Starts the Alpaca Gateway with the Python in this folder.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec ./python/bin/python3 -E -s -m alpacagw "$@"
"""
README = """Alpaca Gateway {version}
====================

Start: {start}
The first start writes config.cfg into this folder: fill in key_id and secret_key there, or leave them
empty and enter them in the window (kept in memory only). Clients connect to ws://127.0.0.1:4102/ (paper).

Options (also through the launcher): --live (config.cfg is a live account), --headless (no window),
--port, --host. The gateway is the Python source in alpacagw/; python/ is a standalone Python {python}
(python-build-standalone) with the libraries it needs. Licenses: LICENSE (the gateway), python/LICENSE*
and the packages' license files in {site_packages}.

Logs: alpacagw.log in this folder. More: README.md, PROTOCOL.md (the protocol for clients).
"""


_version: str | None = None


def version() -> str:
	"""The gateway's version from git: the latest tag vX.Y.Z, X.Y.(Z+1).devN+g<commit> for commits after it (and
	.d<date> with uncommitted changes). Reinstalling alpacagw rewrites alpacagw/_version.py from git (hatch-vcs,
	pyproject.toml); the build ships that file with the source."""
	global _version
	if _version is None:
		if subprocess.run(["uv", "sync", "--frozen", "--inexact", "--project", str(ROOT), "--reinstall-package", "alpacagw"],
				stdout=subprocess.DEVNULL).returncode:
			sys.exit("uv sync failed")
		text = (ROOT / "alpacagw" / "_version.py").read_text(encoding="utf-8")
		_version = re.search(r"^__version__ = version = '([^']+)'", text, re.M).group(1)
		if not re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", _version):
			print(f"warning: {_version} is not a release: tag the commit (git tag -a vX.Y.Z) and commit all changes")
	return _version


def download(url: str, sha256: str) -> Path:
	"""The file at ``url``, kept in build/cache; checked against ``sha256`` (printed when the pin is empty)."""
	CACHE.mkdir(parents=True, exist_ok=True)
	path = CACHE / url.rsplit("/", 1)[1].replace("%2B", "+")
	if not path.exists():
		print(f"downloading {url}")
		tmp = path.with_suffix(".part")
		with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
			shutil.copyfileobj(r, f)
		tmp.replace(path)
	digest = hashlib.sha256(path.read_bytes()).hexdigest()
	if not sha256:
		print(f"  sha256 {digest} (not pinned: put it into TARGETS)")
	elif digest != sha256:
		sys.exit(f"{path.name}: sha256 {digest}, expected {sha256}")
	return path


def install_packages(target: dict, out: Path) -> None:
	"""The locked dependencies (uv.lock, extra ui) for the target platform into ``out``: wheels only."""
	req = out.parent / "requirements.txt"
	subprocess.run(["uv", "export", "--frozen", "--extra", "ui", "--no-dev", "--no-hashes", "--no-emit-project",
		"-o", str(req)], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
	subprocess.run(["uv", "pip", "install", "--quiet", "--target", str(out), "--python-platform", target["platform"],
		"--python-version", PYTHON, "--only-binary", ":all:", "--no-deps", "-r", str(req)], cwd=ROOT, check=True)
	shutil.rmtree(out / "bin", ignore_errors=True)               # console scripts with this machine's paths


class Writer:
	"""Files into the archive (or folder), all under alpaca-gateway/."""

	def __init__(self, path: Path) -> None:
		self.path = path

	def file(self, name: str, data: bytes, mode: int = 0o644) -> None: ...
	def link(self, name: str, target: str) -> None: ...
	def close(self) -> None: ...

	def tree(self, src: Path, dest: str) -> None:
		for p in sorted(src.rglob("*")):
			if p.is_file() and "__pycache__" not in p.parts:
				self.file(f"{dest}/{p.relative_to(src).as_posix()}", p.read_bytes(), 0o755 if os.access(p, os.X_OK)
					and p.suffix in ("", ".so") else 0o644)


class ZipWriter(Writer):
	def __init__(self, path: Path) -> None:
		super().__init__(path)
		self.zip = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9)

	def file(self, name: str, data: bytes, mode: int = 0o644) -> None:
		info = zipfile.ZipInfo(f"{NAME}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
		info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, (0o100000 | mode) << 16
		self.zip.writestr(info, data)

	def close(self) -> None:
		self.zip.close()


class TarWriter(Writer):
	"""tar.xz: keeps the executable bits and the Python's symlinks (python3 -> python3.13)."""

	def __init__(self, path: Path) -> None:
		super().__init__(path)
		self.tar = tarfile.open(path, "w:xz", preset=9)

	def file(self, name: str, data: bytes, mode: int = 0o644) -> None:
		info = tarfile.TarInfo(f"{NAME}/{name}")
		info.size, info.mode, info.mtime = len(data), mode, 1767225600
		self.tar.addfile(info, io.BytesIO(data))

	def link(self, name: str, target: str) -> None:
		info = tarfile.TarInfo(f"{NAME}/{name}")
		info.type, info.linkname, info.mode, info.mtime = tarfile.SYMTYPE, target, 0o777, 1767225600
		self.tar.addfile(info)

	def close(self) -> None:
		self.tar.close()


class DirWriter(Writer):
	def file(self, name: str, data: bytes, mode: int = 0o644) -> None:
		p = self.path / NAME / name
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_bytes(data)
		p.chmod(mode | 0o200)

	def link(self, name: str, target: str) -> None:
		print(f"  skipped symlink {name} -> {target}")


def build(system: str, as_dir: bool) -> Path:
	target = TARGETS[system]
	v = version()
	print(f"{system}: Alpaca Gateway {v}")
	python = download(target["python"], target["sha256"])
	dist = ROOT / "dist"
	dist.mkdir(exist_ok=True)
	if as_dir:
		out = dist / NAME
		shutil.rmtree(out, ignore_errors=True)
		writer: Writer = DirWriter(dist)
	else:
		out = dist / f"{NAME}-{v}-{date.today():%Y%m%d}-{target['archive']}"
		writer = ZipWriter(out) if out.suffix == ".zip" else TarWriter(out)
	with tarfile.open(python) as tar:                            # the Python: python/... in the release archive
		for m in tar:
			if any(f"/{s}" in f"/{m.name}" for s in SKIP_PYTHON):
				continue
			if m.issym():
				writer.link(m.name, m.linkname)
			elif m.isfile():
				writer.file(m.name, tar.extractfile(m).read(), 0o755 if m.mode & 0o111 else 0o644)
	with tempfile.TemporaryDirectory() as tmp:
		packages = Path(tmp) / "site-packages"
		install_packages(target, packages)
		writer.tree(packages, target["site_packages"])
	writer.tree(ROOT / "alpacagw", "alpacagw")
	for doc in DOCS:
		writer.file(doc, (ROOT / doc).read_bytes())
	if system == "windows":
		start = 'double-click "Alpaca Gateway.cmd"'
		writer.file("Alpaca Gateway.cmd", WINDOWS_LAUNCHER.replace("\n", "\r\n").encode())
	else:
		start = "double-click alpaca-gateway (or run ./alpaca-gateway in a terminal)"
		writer.file("alpaca-gateway", LINUX_LAUNCHER.encode(), 0o755)
	text = README.format(version=v, start=start, python=PYTHON, site_packages=target["site_packages"])
	writer.file("README.txt", (text.replace("\n", "\r\n") if system == "windows" else text).encode())
	writer.close()
	size = sum(p.stat().st_size for p in out.rglob("*")) if out.is_dir() else out.stat().st_size
	print(f"  -> {out.relative_to(ROOT)} ({size / 1e6:.0f} MB)")
	return out


def main() -> None:
	p = argparse.ArgumentParser(description="Build the Alpaca Gateway for Windows and Linux (Python source).")
	p.add_argument("--windows", action="store_true")
	p.add_argument("--linux", action="store_true")
	p.add_argument("--dir", action="store_true", help="Windows as a folder dist/alpaca-gateway instead of the zip")
	a = p.parse_args()
	systems = ["windows"] if a.dir else [s for s in TARGETS if getattr(a, s)] or list(TARGETS)
	for system in systems:
		build(system, a.dir)


if __name__ == "__main__":
	main()
