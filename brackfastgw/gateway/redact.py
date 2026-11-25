"""Nothing secret in the log: the API key, its secret and the account number are taken out of every log line.

The log (brackfastgw.log) is meant to be sent along with a question. ``install`` puts the filter on the log handlers;
the key pair is registered when the gateway starts with it (``credentials``), the account number when Alpaca names
it (``account``). Key and secret are replaced completely, the account number shortened (PA3…X1Y).
"""
from __future__ import annotations

import logging
import threading

_lock = threading.Lock()
_values: dict[str, str] = {}                        # what is taken out -> what is written instead


def mask_account(account: str) -> str:
	"""An account number shortened to tell accounts apart without giving it away: PA3QXYZ123 -> PA…123."""
	account = (account or "").strip()
	return f"{account[:2]}…{account[-3:]}" if len(account) > 6 else "…"


def mask_key(key_id: str) -> str:
	"""An API key id for a repr: its first characters only (PKAB…)."""
	return f"{key_id[:4]}…" if len(key_id) > 8 else "…"


def credentials(key_id: str, secret_key: str) -> None:
	_add(key_id, "<key id>")
	_add(secret_key, "<secret>")


def account(value: str) -> None:
	_add(value, mask_account(value))


def _add(value: str, replacement: str) -> None:
	value = (value or "").strip()
	if len(value) >= 6:                              # short strings would take out ordinary words and numbers
		with _lock:
			_values[value] = replacement


def clean(text: str) -> str:
	with _lock:
		values = sorted(_values.items(), key=lambda kv: -len(kv[0]))    # longer first: one may contain another
	for value, replacement in values:
		if value in text:
			text = text.replace(value, replacement)
	return text


class Redactor(logging.Filter):
	"""On a handler: the record's message (its arguments filled in) and exception text cleaned."""

	def filter(self, record: logging.LogRecord) -> bool:
		if _values:
			record.msg, record.args = clean(record.getMessage()), None
			if record.exc_info and not record.exc_text:
				record.exc_text = clean(logging.Formatter().formatException(record.exc_info))
			elif record.exc_text:
				record.exc_text = clean(record.exc_text)
		return True


def install(*handlers: logging.Handler) -> None:
	for handler in handlers:
		handler.addFilter(Redactor())
