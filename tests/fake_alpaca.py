"""A local stand-in for Alpaca's REST and websocket endpoints, for the tests."""
from __future__ import annotations

import msgspec
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from brackfastgw.alpaca import Credentials, Endpoints

KEY, SECRET = "PKTEST", "secret"
CREDENTIALS = Credentials(KEY, SECRET)


class FakeAlpaca:
	"""Routes are added by the tests; the websockets follow Alpaca's handshakes."""

	def __init__(self):
		self.app = web.Application()
		self.server = TestServer(self.app)
		self.stock_frames: list[list[dict]] = []      # sent after the subscription confirmation
		self.trading_frames: list[dict] = []          # sent after listening
		self.received: list[dict] = []                # control messages from the client
		self.stock_connections = 0
		self.reject_auth = False
		self.app.router.add_get("/v2/iex", self._stocks)
		self.app.router.add_get("/stream", self._trading)

	async def start(self) -> Endpoints:
		await self.server.start_server()
		http = str(self.server.make_url("")).rstrip("/")
		ws = http.replace("http://", "ws://")
		return Endpoints(trading=http, data=http, trading_stream=f"{ws}/stream", data_stream=ws)

	async def close(self) -> None:
		await self.server.close()

	async def _stocks(self, request: web.Request) -> web.WebSocketResponse:
		ws = web.WebSocketResponse()
		await ws.prepare(request)
		self.stock_connections += 1
		packb = msgspec.msgpack.encode
		await ws.send_bytes(packb([{"T": "success", "msg": "connected"}]))
		async for msg in ws:
			if msg.type != WSMsgType.BINARY:
				continue
			data = msgspec.msgpack.decode(msg.data)
			self.received.append(data)
			if data["action"] == "auth":
				if self.reject_auth or data["key"] != KEY:
					await ws.send_bytes(packb([{"T": "error", "code": 402, "msg": "auth failed"}]))
					await ws.close()
				else:
					await ws.send_bytes(packb([{"T": "success", "msg": "authenticated"}]))
			elif data["action"] == "subscribe":
				confirmed = {k: v for k, v in data.items() if k != "action"}
				await ws.send_bytes(packb([{"T": "subscription", **confirmed}]))
				for frame in self.stock_frames:
					await ws.send_bytes(packb(frame))
		return ws

	async def _trading(self, request: web.Request) -> web.WebSocketResponse:
		ws = web.WebSocketResponse()
		await ws.prepare(request)
		dumps = msgspec.json.encode
		async for msg in ws:
			data = msgspec.json.decode(msg.data)
			self.received.append(data)
			if data["action"] == "auth":
				status = "authorized" if data["key"] == KEY else "unauthorized"
				await ws.send_bytes(dumps({"stream": "authorization", "data": {"status": status, "action": "authenticate"}}))
			elif data["action"] == "listen":
				await ws.send_bytes(dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}))
				for frame in self.trading_frames:
					await ws.send_bytes(dumps(frame))
		return ws
