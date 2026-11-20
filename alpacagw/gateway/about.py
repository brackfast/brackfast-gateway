"""Settings -> About: version and license; and THIRD-PARTY-LICENSES.txt, the notices and license texts of the
software the gateway ships with (Python, Dear ImGui, GLFW, the fonts, the packages), as their licenses ask.

Dear ImGui, GLFW and the fonts are no Python distributions of their own: their texts are fixed here or taken from
imgui_bundle's files. The packages' texts come from their metadata. The build writes the file next to the launcher
(``python -m alpacagw.gateway.about > THIRD-PARTY-LICENSES.txt``); the About tab opens it (as Brackfast's).
A new dependency belongs in ``LIBRARIES``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from .. import __version__

PRODUCT = "Alpaca Gateway"
COPYRIGHT = "Copyright (c) 2026 The Alpaca Gateway authors"            # as LICENSE (About shows the license only)
LICENSE = "Released under the MIT License."
DISCLAIMER = ("Not affiliated with or endorsed by Alpaca. Trading involves risk; the software comes without "
	"warranty of any kind.")
LICENSES_FILE = "THIRD-PARTY-LICENSES.txt"

MIT = """\
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions
of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
"""

ZLIB = """\
This software is provided 'as-is', without any express or implied warranty. In no event will the authors be held
liable for any damages arising from the use of this software.

Permission is granted to anyone to use this software for any purpose, including commercial applications, and to
alter it and redistribute it freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not claim that you wrote the original
   software. If you use this software in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.
2. Altered source versions must be plainly marked as such, and must not be misrepresented as being the original
   software.
3. This notice may not be removed or altered from any source distribution.
"""


@dataclass(frozen=True)
class Library:
	name: str
	author: str                 # copyright holder(s)
	license: str
	url: str
	package: str = ""           # the Python distribution: its version and its license files
	text: str = ""              # the license text when no distribution has it: a constant, "file:<path in
	                            # imgui_bundle>", "ofl:<copyright line>" (the SIL OFL after it), or "python"
	                            # (the interpreter's LICENSE.txt)

	@property
	def version(self) -> str:
		if not self.package:
			return ""
		try:
			return metadata.version(self.package)
		except metadata.PackageNotFoundError:
			return ""

	def texts(self) -> list[tuple[str, str]]:
		"""(name, text) of its license files."""
		if self.text == "python":
			base = Path(sys.base_prefix)
			found = [*base.glob("LICENSE*"), *base.glob("lib/python3*/LICENSE*")]
			return [(found[0].name, found[0].read_text(encoding="utf-8", errors="replace"))] if found else []
		if self.text.startswith("ofl:"):          # its copyright line, then the OFL from Inconsolata's file
			ofl = Path(_imgui_bundle_folder(), "assets/fonts/Inconsolata-LICENSE-OFL.txt")
			body = ofl.read_text(encoding="utf-8", errors="replace") if ofl.is_file() else ""
			start = body.find("-" * 20)
			return [("", f"{self.text[4:]}\n\n{body[start:] if start >= 0 else body}")] if body else []
		if self.text.startswith("file:"):
			path = Path(_imgui_bundle_folder(), self.text[5:])
			return [(path.name, path.read_text(encoding="utf-8", errors="replace"))] if path.is_file() else []
		if self.text:
			return [("", self.text)]
		try:
			dist = metadata.distribution(self.package)
		except metadata.PackageNotFoundError:
			return []
		files = [f for f in dist.files or () if "licenses/" in f.as_posix()
			or f.name.upper().startswith(("LICEN", "COPYING", "NOTICE"))]
		paths = [(f.as_posix().split("licenses/", 1)[-1], Path(dist.locate_file(f))) for f in files]
		texts: dict[str, str] = {}           # text -> name: a file in the metadata and in the package once
		for name, path in sorted(paths):
			if path.is_file():
				texts.setdefault(path.read_text(encoding="utf-8", errors="replace"), name)
		return [(name, text) for text, name in texts.items()]


def _imgui_bundle_folder() -> str:
	try:
		import imgui_bundle
	except ImportError:                          # without the window's extra: no imgui_bundle, nothing of it shipped
		return ""
	return str(Path(imgui_bundle.__file__).parent)


LIBRARIES = [
	Library("Python", "the Python Software Foundation", "PSF License Agreement", "https://www.python.org",
		text="python"),
	Library("Dear ImGui", "Omar Cornut", "MIT License", "https://github.com/ocornut/imgui",
		text="Copyright (c) 2014-2026 Omar Cornut\n\n" + MIT),
	Library("Dear ImGui Bundle and Hello ImGui", "Pascal Thomet", "MIT License",
		"https://github.com/pthom/imgui_bundle", "imgui-bundle"),
	Library("GLFW", "Marcus Geelnard and Camilla Löwy", "zlib/libpng License", "https://www.glfw.org",
		text="Copyright (c) 2002-2006 Marcus Geelnard\nCopyright (c) 2006-2019 Camilla Löwy\n\n" + ZLIB),
	Library("Roboto font", "Google", "Apache License 2.0", "https://fonts.google.com/specimen/Roboto",
		text="file:assets/fonts/Roboto/LICENSE.txt"),
	Library("Droid Sans font", "Google", "Apache License 2.0", "https://fonts.google.com/specimen/Droid+Sans",
		text="file:assets/fonts/Roboto/LICENSE.txt"),
	Library("Font Awesome icon fonts", "Fonticons, Inc. (Dave Gandy)", "SIL Open Font License 1.1",
		"https://fontawesome.com", text="ofl:Font Awesome Free fonts: Copyright (c) Fonticons, Inc. (https://fontawesome.com)"),
	Library("aiohttp", "the aio-libs contributors", "Apache License 2.0", "https://github.com/aio-libs/aiohttp",
		"aiohttp"),
	Library("aiohappyeyeballs", "J. Nick Koston", "PSF License 2.0", "https://github.com/aio-libs/aiohappyeyeballs",
		"aiohappyeyeballs"),
	Library("aiosignal", "the aio-libs contributors", "Apache License 2.0", "https://github.com/aio-libs/aiosignal",
		"aiosignal"),
	Library("frozenlist", "the aio-libs contributors", "Apache License 2.0", "https://github.com/aio-libs/frozenlist",
		"frozenlist"),
	Library("multidict", "the aio-libs contributors", "Apache License 2.0", "https://github.com/aio-libs/multidict",
		"multidict"),
	Library("propcache", "the aio-libs contributors", "Apache License 2.0", "https://github.com/aio-libs/propcache",
		"propcache"),
	Library("yarl", "the aio-libs contributors", "Apache License 2.0", "https://github.com/aio-libs/yarl", "yarl"),
	Library("attrs", "Hynek Schlawack and the attrs contributors", "MIT License", "https://www.attrs.org", "attrs"),
	Library("idna", "Kim Davies and contributors", "BSD 3-Clause License", "https://github.com/kjd/idna", "idna"),
	Library("msgspec", "Jim Crist-Harif", "BSD 3-Clause License", "https://github.com/jcrist/msgspec", "msgspec"),
	Library("tzdata", "the Python Software Foundation (data: IANA tz database, public domain)",
		"Apache License 2.0", "https://github.com/python/tzdata", "tzdata"),
]


def title() -> str:
	return f"{PRODUCT} {__version__}"


def third_party_text() -> str:
	"""THIRD-PARTY-LICENSES.txt: every library with its notice and full license texts."""
	rule = "=" * 72
	out = [f"{title()}\n{COPYRIGHT}. {LICENSE}\n\nThis software ships with the following third-party software.\n"
		"Their copyright notices and licenses follow.\n"]
	for lib in LIBRARIES:
		version = f" {lib.version}" if lib.version else ""
		out.append(f"\n{rule}\n{lib.name}{version}\n{lib.author}\n{lib.license}\n{lib.url}\n{rule}\n")
		for name, text in lib.texts():
			out.append(f"\n--- {name} ---\n\n{text.strip()}\n" if name else f"\n{text.strip()}\n")
	return "".join(out)


def licenses_path(folder: Path | None = None) -> Path:
	"""THIRD-PARTY-LICENSES.txt to open: the one next to the launcher (``folder``: the gateway's folder); from the
	sources (none there) written to the temp folder from the installed packages."""
	if folder is not None and (folder / LICENSES_FILE).is_file():
		return folder / LICENSES_FILE
	import tempfile
	path = Path(tempfile.gettempdir()) / f"alpacagw-{LICENSES_FILE}"
	path.write_text(third_party_text(), encoding="utf-8")
	return path


def open_licenses(folder: Path | None = None) -> None:
	"""THIRD-PARTY-LICENSES.txt in the system's text viewer."""
	path = licenses_path(folder)
	if sys.platform == "win32":
		import os
		os.startfile(path)                          # noqa: the default app for .txt
	else:
		import subprocess
		subprocess.Popen(["xdg-open", str(path)])


if __name__ == "__main__":
	sys.stdout.reconfigure(encoding="utf-8")
	sys.stdout.write(third_party_text())
