"""The gateway's window (imgui_bundle). It runs on the main thread; the gateway runs on its own
event loop in another thread. The window reads ``GatewayCore.snapshot`` (rebuilt every 0.25 s)
and sends actions through ``GatewayCore.call``.
"""
from __future__ import annotations

import ctypes
import logging
import math
import os
import sys
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import imgui_bundle
from imgui_bundle import hello_imgui, imgui, immapp

from ..client.config import ENVIRONMENTS, FEEDS, load_config, save_config
from .core import GatewayCore, Snapshot

NY = ZoneInfo("America/New_York")
RED = imgui.ImVec4(0.95, 0.30, 0.30, 1.0)
YELLOW = imgui.ImVec4(0.95, 0.80, 0.30, 1.0)
GREY = imgui.ImVec4(0.6, 0.6, 0.6, 1.0)
LEVEL_COLORS = {"warning": YELLOW, "error": RED}
FONT_SIZE = 14.0                                         # pixels at 100 % display scaling
ON = imgui.ImVec4(0.13, 0.55, 0.13, 1.0)               # Connection Status rows, as IB Gateway
OFF = imgui.ImVec4(0.75, 0.12, 0.12, 1.0)
IDLE = imgui.ImVec4(0.30, 0.32, 0.35, 1.0)
TITLE_BG = imgui.ImVec4(0.11, 0.12, 0.14, 1.0)
TITLE_TEXT = imgui.ImVec4(0.80, 0.82, 0.85, 1.0)
BUTTON_HOVER = imgui.ImVec4(1.0, 1.0, 1.0, 0.10)
CLOSE_HOVER = imgui.ImVec4(0.77, 0.17, 0.11, 1.0)     # Windows' close button red

log = logging.getLogger("alpacagw.window")
_glfw_lib: Any = None


def _ny(ts: datetime | None, fmt: str = "%H:%M:%S") -> str:
	return "" if ts is None else ts.astimezone(NY).strftime(fmt)


def _glfw() -> Any:
	"""The GLFW library the window runs on: imgui_bundle ships it next to its module (the same library,
	so the same GLFW state). None when it is not there."""
	global _glfw_lib
	if _glfw_lib is None:
		_glfw_lib = False
		names = {"win32": "glfw3.dll", "darwin": "libglfw.3.dylib"}
		folder = os.path.dirname(imgui_bundle.__file__)
		for name in (names.get(sys.platform, "libglfw.so.3"), "libglfw.so", "libglfw.dylib"):
			path = os.path.join(folder, name)
			if os.path.exists(path):
				try:
					lib = ctypes.CDLL(path)
					lib.glfwIconifyWindow.argtypes = [ctypes.c_void_p]
					lib.glfwIconifyWindow.restype = None
					_glfw_lib = lib
					break
				except (OSError, AttributeError) as exc:
					log.debug("GLFW library %s: %s", path, exc)
	return _glfw_lib or None


def _pointer(handle: Any) -> int:
	"""A viewport's platform handle (the GLFW backend's GLFWwindow*) as an address; imgui_bundle hands
	it over as a PyCapsule."""
	if handle is None or isinstance(handle, int):
		return handle or 0
	get = ctypes.pythonapi.PyCapsule_GetPointer
	get.restype, get.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
	name = ctypes.pythonapi.PyCapsule_GetName
	name.restype, name.argtypes = ctypes.c_char_p, [ctypes.py_object]
	return get(handle, name(handle)) or 0


def _row_height() -> float:
	"""Height of a Connection Status row (and of the account bar above the table)."""
	return imgui.get_frame_height() + 2 * imgui.get_style().cell_padding.y


def _gear(dl: imgui.ImDrawList, cx: float, cy: float, r: float, col: int) -> None:
	"""A settings gear: 8 teeth around a ring."""
	for k in range(8):
		a = k * math.pi / 4
		c, s = math.cos(a), math.sin(a)
		dl.add_line(imgui.ImVec2(cx + c * r * 0.7, cy + s * r * 0.7), imgui.ImVec2(cx + c * r * 1.15, cy + s * r * 1.15),
			col, max(1.5, r * 0.32))
	dl.add_circle(imgui.ImVec2(cx, cy), r * 0.68, col, 0, max(1.2, r * 0.24))


def minimize_os_window(viewport: imgui.ImGuiViewport) -> None:
	"""Minimize the OS window an imgui window lives in (restored from the taskbar)."""
	lib = _glfw()
	handle = _pointer(viewport.platform_handle) if lib is not None else 0
	if handle:
		lib.glfwIconifyWindow(handle)
	else:
		log.warning("minimize: no GLFW window handle")


class GatewayWindow:
	def __init__(self, core: GatewayCore, thread: threading.Thread) -> None:
		self.core = core
		self.thread = thread
		self.confirm: tuple[str, str, object] | None = None    # (title, text, action)
		self.form: dict[str, str] = {}                          # the settings dialog's fields
		self.form_message = ""
		self.show_secret = False
		self.settings_requested = False
		self.title = "Alpaca Gateway"

	def run(self) -> None:
		"""The OS main window stays hidden: the gateway's window is a Dear ImGui window in an OS window
		of its own (multi-viewports), with imgui's dark title bar instead of the system's. No ini file:
		size and place are the defaults at every start."""
		live = self.core.alpaca.environment == "live"
		self.title = f"Alpaca Gateway - {'LIVE' if live else 'paper'}"
		params = hello_imgui.RunnerParams()
		params.app_window_params.window_title = self.title
		params.app_window_params.hidden = True
		params.imgui_window_params.enable_viewports = True
		params.imgui_window_params.default_imgui_window_type = hello_imgui.DefaultImGuiWindowType.no_default_window
		params.imgui_window_params.show_menu_bar = False
		params.imgui_window_params.show_status_bar = False
		params.imgui_window_params.tweaked_theme.theme = hello_imgui.ImGuiTheme_.so_dark_accent_blue
		params.imgui_window_params.remember_theme = False
		params.ini_disable = True
		params.fps_idling.fps_idle = 4.0
		params.callbacks.show_gui = self.gui
		params.callbacks.post_init = self._post_init
		params.callbacks.setup_imgui_style = self._style
		immapp.run(params)

	@staticmethod
	def _style() -> None:
		"""After the theme: the font size (ImGui >= 1.92 scales its fonts to it)."""
		imgui.get_style().font_size_base = FONT_SIZE

	@staticmethod
	def _post_init() -> None:
		io = imgui.get_io()
		io.config_viewports_no_auto_merge = True    # never into the hidden main window
		io.config_windows_move_from_title_bar_only = True    # no imgui title bar: only our bar moves it

	# -- frame --------------------------------------------------------------------------

	def gui(self) -> None:
		if not self.thread.is_alive():
			hello_imgui.get_runner_params().app_shall_exit = True
			return
		imgui.set_next_window_pos(imgui.ImVec2(200, 120), imgui.Cond_.appearing)
		imgui.set_next_window_size(imgui.ImVec2(490, 440), imgui.Cond_.appearing)
		imgui.push_style_var(imgui.StyleVar_.window_padding, imgui.ImVec2(0, 0))
		imgui.begin(f"{self.title}###gateway", None, imgui.WindowFlags_.no_title_bar | imgui.WindowFlags_.no_collapse
			| imgui.WindowFlags_.no_scrollbar | imgui.WindowFlags_.no_scroll_with_mouse)
		imgui.pop_style_var()
		self._title_bar()
		imgui.begin_child("body", imgui.ImVec2(0, 0), imgui.ChildFlags_.always_use_window_padding)
		snap = self.core.snapshot
		if snap is None:
			imgui.text(f"starting ... ({self.core.status.state} {self.core.status.text})")
		else:
			self._header(snap)
			imgui.separator()
			if imgui.begin_tab_bar("tabs"):
				for name, draw in (("Clients", self._clients), ("Log", self._log)):
					if imgui.begin_tab_item(name)[0]:
						draw(snap)
						imgui.end_tab_item()
				imgui.end_tab_bar()
		self._confirm_popup()
		self._settings_popup()
		imgui.end_child()
		imgui.end()

	def _title_bar(self) -> None:
		"""Our own title bar: the name on the left, minimize and close on the right; dragging it moves the
		window (and with it its OS window)."""
		height = round(imgui.get_frame_height() * 1.45)
		button_w = round(height * 1.4)
		pos = imgui.get_cursor_screen_pos()
		width = imgui.get_window_width()
		dl = imgui.get_window_draw_list()
		dl.add_rect_filled(pos, imgui.ImVec2(pos.x + width, pos.y + height), imgui.get_color_u32(TITLE_BG))
		text_h = imgui.get_text_line_height()
		dl.add_text(imgui.ImVec2(pos.x + 12, pos.y + (height - text_h) / 2), imgui.get_color_u32(TITLE_TEXT),
			"Alpaca Gateway")

		buttons = ("settings", "minimize", "close")
		imgui.set_cursor_screen_pos(pos)
		imgui.invisible_button("##drag", imgui.ImVec2(max(1.0, width - len(buttons) * button_w), height))
		if imgui.is_item_active() and imgui.is_mouse_dragging(imgui.MouseButton_.left, 0.0):
			delta = imgui.get_io().mouse_delta
			wp = imgui.get_window_pos()
			imgui.set_window_pos(imgui.ImVec2(wp.x + delta.x, wp.y + delta.y))

		for i, name in enumerate(buttons):
			x = pos.x + width - (len(buttons) - i) * button_w
			imgui.set_cursor_screen_pos(imgui.ImVec2(x, pos.y))
			clicked = imgui.invisible_button(f"##{name}", imgui.ImVec2(button_w, height))
			hovered = imgui.is_item_hovered()
			if hovered:
				dl.add_rect_filled(imgui.ImVec2(x, pos.y), imgui.ImVec2(x + button_w, pos.y + height),
					imgui.get_color_u32(CLOSE_HOVER if name == "close" else BUTTON_HOVER))
			cx, cy, s = x + button_w / 2, pos.y + height / 2, round(height * 0.16)
			col = imgui.get_color_u32(imgui.ImVec4(1, 1, 1, 1) if hovered else TITLE_TEXT)
			if name == "settings":
				_gear(dl, cx, cy, s * 1.25, col)
			elif name == "minimize":
				dl.add_line(imgui.ImVec2(cx - s, cy), imgui.ImVec2(cx + s, cy), col, 1.0)
			else:
				dl.add_line(imgui.ImVec2(cx - s, cy - s), imgui.ImVec2(cx + s, cy + s), col, 1.0)
				dl.add_line(imgui.ImVec2(cx - s, cy + s), imgui.ImVec2(cx + s, cy - s), col, 1.0)
			if hovered and name == "settings":
				imgui.set_tooltip("Settings")
			if clicked:
				if name == "settings":
					self._open_settings()
				elif name == "minimize":
					minimize_os_window(imgui.get_window_viewport())
				else:
					self._ask("Quit the gateway?", "Connected clients lose their connection.", self.core.request_stop)
		imgui.set_cursor_screen_pos(imgui.ImVec2(pos.x, pos.y + height))

	# -- settings --------------------------------------------------------------------------

	def _open_settings(self) -> None:
		"""The account's config file into the dialog's fields (_settings_popup opens it)."""
		path = self.core.alpaca.config.path
		try:
			config = load_config(path)
			self.form = {"environment": config.environment, "key_id": config.credentials.key_id,
				"secret_key": config.credentials.secret_key, "stock_feed": config.stock_feed}
		except (OSError, ValueError):
			self.form = {"environment": "paper", "key_id": "", "secret_key": "", "stock_feed": "iex"}
		self.form_message = ""
		self.show_secret = False
		self.settings_requested = True

	def _settings_popup(self) -> None:
		if self.settings_requested:
			self.settings_requested = False
			imgui.open_popup("Settings")
		if not imgui.begin_popup_modal("Settings", None, imgui.WindowFlags_.always_auto_resize)[0]:
			return
		f = self.form
		path = self.core.alpaca.config.path
		imgui.text_colored(GREY, f"Account configuration: {path.name}")
		if imgui.is_item_hovered():
			imgui.set_tooltip(str(path.resolve()))
		imgui.spacing()
		label_w = imgui.calc_text_size("Environment").x + 2 * imgui.get_style().item_spacing.x
		field_w = imgui.get_font_size() * 22

		def label(text: str) -> None:
			imgui.align_text_to_frame_padding()
			imgui.text(text)
			imgui.same_line(label_w)
			imgui.set_next_item_width(field_w)

		label("Environment")
		envs = list(ENVIRONMENTS)
		changed, i = imgui.combo("##environment", envs.index(f["environment"]), envs)
		if changed:
			f["environment"] = envs[i]
		label("Key ID")
		_, f["key_id"] = imgui.input_text("##key_id", f["key_id"])
		label("Secret key")
		imgui.set_next_item_width(field_w - imgui.get_font_size() * 4.5)
		flags = 0 if self.show_secret else imgui.InputTextFlags_.password
		_, f["secret_key"] = imgui.input_text("##secret_key", f["secret_key"], flags)
		imgui.same_line()
		_, self.show_secret = imgui.checkbox("show", self.show_secret)
		label("Data feed")
		feeds = list(FEEDS)
		changed, i = imgui.combo("##stock_feed", feeds.index(f["stock_feed"]) if f["stock_feed"] in feeds else 0, feeds)
		if changed:
			f["stock_feed"] = feeds[i]
		imgui.text_colored(GREY, "iex: free  |  sip: all exchanges (paid)  |  delayed_sip: 15 min")
		if f["environment"] == "live":
			imgui.text_colored(RED, "A live account: the gateway starts with it only with --live.")
		if self.form_message:
			imgui.spacing()
			imgui.text_colored(YELLOW, self.form_message)
		imgui.spacing()
		imgui.separator()
		if imgui.button("Save", imgui.ImVec2(90, 0)):
			if not f["key_id"].strip() or not f["secret_key"].strip():
				self.form_message = "Key ID and secret key are required."
			else:
				try:
					save_config(path, {k: v.strip() for k, v in f.items()})
					self.core.call(self._saved_event)
					self.form_message = "Saved. Restart the gateway to use the new settings."
				except OSError as exc:
					self.form_message = f"Not saved: {exc}"
		imgui.same_line()
		if imgui.button("Close", imgui.ImVec2(90, 0)) or imgui.is_key_pressed(imgui.Key.escape):
			imgui.close_current_popup()
		imgui.end_popup()

	async def _saved_event(self) -> None:
		self.core.event("info", f"settings saved to {self.core.alpaca.config.path}; they apply after a restart")

	def _ask(self, title: str, text: str, action) -> None:
		self.confirm = (title, text, action)          # _confirm_popup opens it (in the body's id scope)

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
		# paper / live as a bar across the window, the account in it
		live = snap.environment == "live"
		text = f"{'LIVE' if live else 'PAPER'} ACCOUNT"
		if snap.account is not None:
			text += f"   {snap.account.account}"
		pos = imgui.get_cursor_screen_pos()
		width, height = imgui.get_content_region_avail().x, _row_height()
		imgui.get_window_draw_list().add_rect_filled(pos, imgui.ImVec2(pos.x + width, pos.y + height),
			imgui.get_color_u32(OFF if live else ON), 3.0)
		size = imgui.calc_text_size(text)
		imgui.get_window_draw_list().add_text(imgui.ImVec2(pos.x + (width - size.x) / 2, pos.y + (height - size.y) / 2),
			imgui.get_color_u32(imgui.ImVec4(1, 1, 1, 1)), text)
		imgui.dummy(imgui.ImVec2(width, height))
		if snap.status.text:
			imgui.text_colored(RED, snap.status.text)
		imgui.spacing()
		self._connection_status(snap)

	def _connection_status(self, snap: Snapshot) -> None:
		"""Connection Status as IB Gateway shows it: one row per connection, green when up, red when down."""
		s = snap.status
		n = len(snap.clients)
		rows = [
			("Alpaca Trading API", ON if snap.rest_ok else OFF, "connected" if snap.rest_ok else "disconnected"),
			("Order Updates", ON if s.trading_connected else OFF, "ON: trade_updates" if s.trading_connected
				else "disconnected"),
			("Market Data Stream", ON if s.market_data_connected else OFF if snap.market_symbols else IDLE,
				f"ON: {snap.feed}, {snap.market_symbols} symbol(s)" if s.market_data_connected
				else "disconnected" if snap.market_symbols else "idle (no subscriptions)"),
			("API Server", ON if s.state == "ready" else OFF, f"listening on {snap.address}" if s.state == "ready"
				else s.state),
			("API Client", ON if n else OFF, f"{n} connected" if n else "disconnected"),
		]
		imgui.text("Connection Status")
		flags = imgui.TableFlags_.borders | imgui.TableFlags_.sizing_stretch_same
		if not imgui.begin_table("connections", 2, flags):
			return
		imgui.table_setup_column("Purpose")
		imgui.table_setup_column("Status")
		imgui.table_next_row(imgui.TableRowFlags_.headers)
		for name in ("Purpose", "Status"):
			imgui.table_next_column()
			self._centered(name)
		for purpose, color, status in rows:
			imgui.table_next_row(0, _row_height())
			imgui.table_set_bg_color(imgui.TableBgTarget_.row_bg0, imgui.get_color_u32(color))
			for text in (purpose, status):
				imgui.table_next_column()
				imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + imgui.get_style().frame_padding.y)
				self._centered(text)
		imgui.end_table()

	@staticmethod
	def _centered(text: str) -> None:
		width = imgui.get_content_region_avail().x
		imgui.set_cursor_pos_x(imgui.get_cursor_pos_x() + max(0.0, (width - imgui.calc_text_size(text).x) / 2))
		imgui.text(text)

	# -- tabs ---------------------------------------------------------------------------

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
