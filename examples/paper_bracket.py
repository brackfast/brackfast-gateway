"""Smoke test of a running gateway with a paper account: a limit bracket far below the market
(it cannot fill), the take profit, stop loss and entry moved, then canceled.

	uv run brackfast-gateway --headless     # in another terminal (config.cfg with the paper keys)
	uv run python examples/paper_bracket.py [SYMBOL]

Refuses to run against a live account.
"""
from __future__ import annotations

import asyncio
import sys
import time

import aiohttp

from brackfastgw.protocol import (
	PORT, PROTOCOL_VERSION, Bars, CancelOrder, GetBars, Hello, Order, Reply, ReplaceOrder, SubmitBracket,
	Welcome, decode_gateway, encode,
)


async def main(symbol: str) -> int:
	async with aiohttp.ClientSession() as session:
		ws = await session.ws_connect(f"ws://127.0.0.1:{PORT}/")
		orders: dict[str, Order] = {}
		got: list = []

		async def reader() -> None:
			async for msg in ws:
				m = decode_gateway(msg.data)
				got.append(m)
				if isinstance(m, Order):
					orders[m.id] = m
				if isinstance(m, Reply):
					print(f"  reply {m.command} {m.id}: {'ok' if m.ok else m.text}")

		task = asyncio.create_task(reader())
		await ws.send_bytes(encode(Hello(protocol=PROTOCOL_VERSION, client="paper-bracket")))
		await ws.send_bytes(encode(GetBars(request_id=1, symbol=symbol, timeframe="1m", limit=1)))
		await asyncio.sleep(2)
		welcome = next(m for m in got if isinstance(m, Welcome))
		if welcome.environment != "paper":
			print(f"the gateway trades a {welcome.environment} account: not running")
			return 1
		last = next(m for m in got if isinstance(m, Bars)).bars[-1].close
		entry, tp, sl = round(last * 0.80, 2), round(last * 0.85, 2), round(last * 0.75, 2)
		bid = f"smoke{int(time.time())}"
		steps = [
			(f"bracket {bid}: buy 1 {symbol} @ {entry}, take profit {tp}, stop loss {sl} (last {last})",
				SubmitBracket(id=bid, symbol=symbol, side="buy", qty=1, limit_price=entry, take_profit=tp, stop_loss=sl)),
			("move the take profit", ReplaceOrder(id=f"{bid}.tp", limit_price=round(tp + 1, 2))),
			("move the stop loss", ReplaceOrder(id=f"{bid}.sl", stop_price=round(sl - 1, 2))),
			("move the entry", ReplaceOrder(id=bid, limit_price=round(entry - 1, 2))),
			("cancel the entry (and with it the legs)", CancelOrder(id=bid)),
		]
		for text, cmd in steps:
			print(text)
			await ws.send_bytes(encode(cmd))
			await asyncio.sleep(3)
		await asyncio.sleep(1)
		for o in orders.values():
			if o.id.startswith(bid):
				print(f"  {o.id:22} {o.role:12} {o.status:9} limit {o.limit_price} stop {o.stop_price}")
		task.cancel()
		ok = all(o.status == "canceled" for o in orders.values() if o.id.startswith(bid))
		print("ok" if ok else "FAILED")
		return 0 if ok else 1


if __name__ == "__main__":
	sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "AAPL")))
