"""What the window's info icon shows: version, copyright and the libraries the gateway is built on,
each with its author and license (the notices their licenses ask for)."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata

from .. import __version__

PRODUCT = "Alpaca Gateway"
COPYRIGHT = "Copyright (c) 2026 The Brackfast Gateway authors"
LICENSE = "Released under the MIT License."
DISCLAIMER = ("Not affiliated with or endorsed by Alpaca. Trading involves risk; the software comes without "
	"warranty of any kind.")


@dataclass(frozen=True)
class Library:
	name: str
	author: str                 # copyright holder(s)
	license: str
	url: str
	package: str = ""           # the Python distribution, for its installed version

	@property
	def version(self) -> str:
		if not self.package:
			return ""
		try:
			return metadata.version(self.package)
		except metadata.PackageNotFoundError:
			return ""


LIBRARIES = [
	Library("Python", "the Python Software Foundation", "PSF License Agreement", "https://www.python.org"),
	Library("Dear ImGui", "Omar Cornut", "MIT License", "https://github.com/ocornut/imgui"),
	Library("Dear ImGui Bundle and Hello ImGui", "Pascal Thomet", "MIT License",
		"https://github.com/pthom/imgui_bundle", "imgui-bundle"),
	Library("GLFW", "Marcus Geelnard and Camilla Löwy", "zlib/libpng License", "https://www.glfw.org"),
	Library("Droid Sans font", "Google", "Apache License 2.0", "https://fonts.google.com/specimen/Droid+Sans"),
	Library("Font Awesome 4 icon font", "Dave Gandy", "SIL Open Font License 1.1", "https://fontawesome.com"),
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
