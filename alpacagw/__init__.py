"""Brackfast Gateway (formerly the Alpaca Gateway). ``alpacagw.client``: the async Alpaca client the gateway is built on."""
try:
	from ._version import __version__   # from the git tag, written when uv installs / builds the gateway
except ImportError:                     # never installed (uv sync writes it)
	__version__ = "0.0.0+unknown"
