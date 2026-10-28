"""Base structs and helpers shared by all models."""
from __future__ import annotations

from decimal import Decimal

import msgspec


class Model(msgspec.Struct, kw_only=True, gc=False):
	"""Data decoded from Alpaca. Unknown fields are ignored, so fields Alpaca adds later never break decoding."""


class Request(msgspec.Struct, kw_only=True, omit_defaults=True):
	"""Request body or query parameters. Fields left at their default are not sent."""


def dec(value: Decimal | int | float | str | None) -> Decimal | None:
	"""Decimal without float artefacts: 0.1 -> Decimal("0.1")."""
	if value is None or isinstance(value, Decimal):
		return value
	return Decimal(str(value))
