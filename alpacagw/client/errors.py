from __future__ import annotations

import msgspec


class _ErrorBody(msgspec.Struct, kw_only=True):
	code: int | None = None
	message: str | None = None


class AlpacaError(Exception):
	"""An error reply from an Alpaca REST endpoint."""

	def __init__(self, status: int, message: str, code: int | None = None, body: bytes = b""):
		super().__init__(message)
		self.status = status
		self.code = code
		self.message = message
		self.body = body

	@classmethod
	def from_body(cls, status: int, raw: bytes) -> AlpacaError:
		text = raw.decode(errors="replace").strip()
		try:
			err = msgspec.json.decode(raw, type=_ErrorBody)
		except msgspec.DecodeError:
			return cls(status, text or f"HTTP {status}", None, raw)
		return cls(status, err.message or text or f"HTTP {status}", err.code, raw)

	def __str__(self) -> str:
		code = f" {self.code}" if self.code is not None else ""
		return f"[HTTP {self.status}{code}] {self.message}"


class StreamError(Exception):
	"""A websocket error that reconnecting cannot fix (authentication failed, no subscription)."""

	def __init__(self, code: int | None, message: str):
		super().__init__(message)
		self.code = code
		self.message = message

	def __str__(self) -> str:
		return f"[stream {self.code}] {self.message}"
