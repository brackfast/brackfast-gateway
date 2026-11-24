"""The controls' look, the same as Brackfast's and Chartfast's (Brackfast's brackfast/look.py: a change there belongs here too;
the gateway does not depend on Brackfast). Applied after hello_imgui's theme (so_dark_accent_blue) in ``_style``.

- Buttons flat: ``button()`` draws them without the frame border. The theme's 1 px border plus its border shadow (a
  second line 1 px down and right) made their edges look doubled.
- No border shadow anywhere: input fields and combos keep one clean 1 px outline.
- 2 px corners for controls, tabs, popups and child panes (the theme: 3 / 4); the window's own corners and the
  scrollbars stay.
- Controls 20 px high: 3 px above and below the 14 px text (the theme: 2, 18 px).
- 4 px between controls, side by side and one above the other (the theme: 6).
- Buttons lighter while hovered / pressed (the theme's hover grey is the dialogs' background: they vanished).
"""
from __future__ import annotations

from imgui_bundle import imgui

CORNER_RADIUS = 2.0                 # px: buttons, fields, combos, tabs, popups, child panes
FRAME_PAD_Y = 3.0                   # px above and below a control's text: 20 px high controls at a 14 px font
ITEM_GAP = 4.0                      # px between controls, side by side and one above the other
BUTTON_HOVERED = imgui.ImVec4(0.38, 0.38, 0.38, 0.70)   # lighter than the button (0.30 at 0.62) on any background
BUTTON_ACTIVE = imgui.ImVec4(0.44, 0.44, 0.44, 0.75)    # pressed: lighter still


def apply(style: imgui.Style | None = None) -> None:
	"""The look onto the current style (after the theme)."""
	style = style or imgui.get_style()
	for name in ("frame_rounding", "grab_rounding", "tab_rounding", "popup_rounding", "child_rounding"):
		setattr(style, name, CORNER_RADIUS)
	style.set_color_(imgui.Col_.border_shadow, imgui.ImVec4(0, 0, 0, 0))
	style.frame_padding = imgui.ImVec2(style.frame_padding.x, FRAME_PAD_Y)
	style.item_spacing = imgui.ImVec2(ITEM_GAP, ITEM_GAP)
	# buttons lighter while hovered and pressed: the theme's hovered grey is darker (0.19), the dialogs' own
	# background, so a flat button vanished under the mouse there
	style.set_color_(imgui.Col_.button_hovered, BUTTON_HOVERED)
	style.set_color_(imgui.Col_.button_active, BUTTON_ACTIVE)


def button(label: str, size: imgui.ImVec2 | None = None) -> bool:
	"""imgui.button, flat: no frame border (the fields keep theirs)."""
	imgui.push_style_var(imgui.StyleVar_.frame_border_size, 0.0)
	clicked = imgui.button(label, size if size is not None else imgui.ImVec2(0, 0))
	imgui.pop_style_var()
	return clicked
