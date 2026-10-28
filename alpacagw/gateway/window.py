"""The gateway's window (imgui_bundle). It runs on the main thread; the gateway runs on its own
event loop in another thread. The window reads ``GatewayCore.snapshot`` (rebuilt every 0.25 s)
and sends actions through ``GatewayCore.call``.
"""
from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from imgui_bundle import hello_imgui, imgui, immapp

from ..protocol import FINAL
from .core import GatewayCore, Snapshot

NY = ZoneInfo("America/New_York")
GREEN = imgui.ImVec4(0.30, 0.80, 0.40, 1.0)
RED = imgui.ImVec4(0.95, 0.30, 0.30, 1.0)
YELLOW = imgui.ImVec4(0.95, 0.80, 0.30, 1.0)
GREY = imgui.ImVec4(0.6, 0.6, 0.6, 1.0)
LEVEL_COLORS = {"warning": YELLOW, "error": RED}


def _ny(ts: datetime | None, fmt: str = "%H:%M:%S") -> str:
	return "" if ts is None else ts.astimezone(NY).strftime(fmt)


def _price(p: float | None) -> str:
	return "" if p is None else f"{p:,.2f}" if p >= 1 else f"{p:.4f}"


class GatewayWindow:
	def __init__(self, core: GatewayCore, thread: threading.Thread) -> None:
		self.core = core
		self.thread = thread
		self.working_only = True
		self.show_legs = True
		self.confirm: tuple[str, str, object] | None = None    # (title, text, action)

	def run(self) -> None:
		live = self.core.alpaca.environment == "live"
		title = f"Alpaca Gateway - {'LIVE' if live else 'paper'}"
		immapp.run(self.gui, window_title=title, window_size=(980, 660), fps_idle=4)

	# -- frame --------------------------------------------------------------------------

	def gui(self) -> None:
		if not self.thread.is_alive():
			hello_imgui.get_runner_params().app_shall_exit = True
			return
		snap = self.core.snapshot
		if snap is None:
			imgui.text(f"starting ... ({self.core.status.state} {self.core.status.text})")
			return
		self._header(snap)
		imgui.separator()
		if imgui.begin_tab_bar("tabs"):
			for name, draw in (("Positions", self._positions), ("Orders", self._orders), ("Clients", self._clients),
					("Log", self._log)):
				if imgui.begin_tab_item(name)[0]:
					draw(snap)
					imgui.end_tab_item()
			imgui.end_tab_bar()
		self._confirm_popup()

	def _ask(self, title: str, text: str, action) -> None:
		self.confirm = (title, text, action)
		imgui.open_popup("confirm")

	def _confirm_popup(self) -> None:
		if self.confirm is not None and not imgui.is_popup_open("confirm"):
			imgui.open_popup("confirm")
		if imgui.begin_popup_modal("confirm", None, imgui.WindowFlags_.always_auto_resize)[0]:
			title, text, action = self.confirm or ("", "", None)
			imgui.text(title)
			imgui.text_colored(GREY, text)
			if imgui.button("OK", (90, 0)):
				if action is not None:
					action()
				self.confirm = None
				imgui.close_current_popup()
			imgui.same_line()
			if imgui.button("Cancel", (90, 0)) or imgui.is_key_pressed(imgui.Key.escape):
				self.confirm = None
				imgui.close_current_popup()
			imgui.end_popup()

	def _header(self, snap: Snapshot) -> None:
		live = snap.environment == "live"
		imgui.push_style_color(imgui.Col_.button, RED if live else GREEN)
		imgui.button(" LIVE " if live else " PAPER ")
		imgui.pop_style_color()
		imgui.same_line()
		s = snap.status
		imgui.text_colored(GREEN if s.state == "ready" else YELLOW if s.state == "starting" else RED, s.state)
		imgui.same_line()
		imgui.text_colored(GREY, snap.address)
		imgui.same_line()
		imgui.text_colored(GREEN if s.trading_connected else RED, "orders")
		imgui.same_line()
		imgui.text_colored(GREEN if s.market_data_connected else GREY, "market data")
		imgui.same_line()
		imgui.text(f"{len(snap.clients)} client(s)")
		if s.text:
			imgui.text_colored(RED, s.text)
		imgui.same_line(imgui.get_window_width() - 70)
		if imgui.button("Quit"):
			self._ask("Quit the gateway?", "Connected clients lose their connection.", self.core.request_stop)

		a = snap.account
		if a is not None:
			day = a.equity - a.last_equity if a.last_equity else 0.0
			imgui.text(f"Account {a.account}   equity {a.equity:,.2f}   ")
			imgui.same_line()
			imgui.text_colored(GREEN if day >= 0 else RED, f"day {day:+,.2f}")
			imgui.same_line()
			imgui.text(f"   cash {a.cash:,.2f}   buying power {a.buying_power:,.2f}   day trades {a.daytrade_count}"
				+ ("   PDT" if a.pattern_day_trader else ""))
		working = sum(1 for o in snap.orders if o.status not in FINAL)
		imgui.begin_disabled(not snap.positions and not working)
		if imgui.button("Flatten all"):
			self._ask("Flatten all?", "Cancels every order and closes every position at market.",
				lambda: self.core.call(self.core.flatten))
		imgui.end_disabled()

	# -- tabs ---------------------------------------------------------------------------

	def _positions(self, snap: Snapshot) -> None:
		flags = imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.row_bg | imgui.TableFlags_.resizable
		if not imgui.begin_table("positions", 7, flags):
			return
		for name in ("Symbol", "Qty", "Avg price", "Price", "Value", "Unrealized", ""):
			imgui.table_setup_column(name)
		imgui.table_headers_row()
		for p in snap.positions:
			imgui.table_next_row()
			imgui.table_next_column(); imgui.text(p.symbol)
			imgui.table_next_column(); imgui.text_colored(GREEN if p.qty > 0 else RED, f"{p.qty:g}")
			imgui.table_next_column(); imgui.text(_price(p.avg_price))
			imgui.table_next_column(); imgui.text(_price(p.current_price))
			imgui.table_next_column(); imgui.text(f"{p.market_value:,.2f}")
			imgui.table_next_column(); imgui.text_colored(GREEN if p.unrealized_pl >= 0 else RED, f"{p.unrealized_pl:+,.2f}")
			imgui.table_next_column()
			if imgui.small_button(f"Close##{p.symbol}"):
				self._ask(f"Close {p.symbol}?", f"Cancels its orders and sells / buys {abs(p.qty):g} shares at market.",
					lambda s=p.symbol: self.core.call(self.core.close_position, s))
		imgui.end_table()

	def _orders(self, snap: Snapshot) -> None:
		_, self.working_only = imgui.checkbox("Working only", self.working_only)
		imgui.same_line()
		_, self.show_legs = imgui.checkbox("Show legs", self.show_legs)
		flags = (imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.row_bg | imgui.TableFlags_.resizable
			| imgui.TableFlags_.scroll_y)
		if not imgui.begin_table("orders", 11, flags):
			return
		for name in ("", "Time", "Symbol", "Role", "Side", "Type", "Qty", "Limit", "Stop", "Status", "Origin"):
			imgui.table_setup_column(name)
		imgui.table_setup_scroll_freeze(0, 1)
		imgui.table_headers_row()
		by_id = {o.id: o for o in snap.orders}
		for o in self._grouped(snap.orders):
			final = o.status in FINAL
			if self.working_only and final:
				continue
			if not self.show_legs and o.parent_id:
				continue
			imgui.table_next_row()
			imgui.table_next_column()
			parent_working = o.parent_id in by_id and by_id[o.parent_id].status not in FINAL
			if not final and not parent_working and imgui.small_button(f"x##{o.id}"):
				self.core.call(self.core.cancel, o.id)
			imgui.table_next_column(); imgui.text(_ny(o.created_at))
			imgui.table_next_column(); imgui.text(("   " if o.parent_id else "") + o.symbol)
			imgui.table_next_column(); imgui.text(o.role)
			imgui.table_next_column(); imgui.text_colored(GREEN if o.side == "buy" else RED, o.side)
			imgui.table_next_column(); imgui.text(o.type)
			imgui.table_next_column(); imgui.text(f"{o.filled_qty:g}/{o.qty:g}" if o.filled_qty else f"{o.qty:g}")
			imgui.table_next_column(); imgui.text(_price(o.limit_price))
			imgui.table_next_column(); imgui.text(_price(o.stop_price))
			imgui.table_next_column()
			imgui.text_colored(GREY if final else YELLOW if o.status == "held" else GREEN, o.status)
			if imgui.is_item_hovered():
				imgui.set_tooltip(f"id {o.id}\nalpaca {o.broker_id}\ntif {o.tif}"
					+ (f"\navg fill {_price(o.avg_fill_price)}" if o.avg_fill_price else "")
					+ (f"\n{o.reason}" if o.reason else ""))
			imgui.table_next_column(); imgui.text(o.origin)
		imgui.end_table()

	@staticmethod
	def _grouped(orders: list) -> list:
		"""Newest first, each leg right below its entry."""
		legs: dict[str, list] = {}
		for o in orders:
			if o.parent_id:
				legs.setdefault(o.parent_id, []).append(o)
		ids = {o.id for o in orders}
		out = []
		for o in orders:
			if o.parent_id and o.parent_id in ids:
				continue
			out.append(o)
			out.extend(sorted(legs.get(o.id, ()), key=lambda leg: leg.role))
		return out

	def _clients(self, snap: Snapshot) -> None:
		if not imgui.begin_table("clients", 4, imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.row_bg):
			return
		for name in ("Client", "Address", "Connected", "Symbols"):
			imgui.table_setup_column(name)
		imgui.table_headers_row()
		for name, peer, since, symbols in snap.clients:
			imgui.table_next_row()
			imgui.table_next_column(); imgui.text(name)
			imgui.table_next_column(); imgui.text(peer)
			imgui.table_next_column(); imgui.text(_ny(since, "%m-%d %H:%M:%S"))
			imgui.table_next_column(); imgui.text(str(symbols))
		imgui.end_table()

	def _log(self, snap: Snapshot) -> None:
		imgui.begin_child("log")
		for ev in snap.events:
			imgui.text_colored(GREY, _ny(ev.ts))
			imgui.same_line()
			text = f"[{ev.client}] {ev.text}" if ev.client else ev.text
			color = LEVEL_COLORS.get(ev.level)
			if color is None:
				imgui.text_unformatted(text)
			else:
				imgui.text_colored(color, text)
		if imgui.get_scroll_y() >= imgui.get_scroll_max_y() - 5:
			imgui.set_scroll_here_y(1.0)
		imgui.end_child()
