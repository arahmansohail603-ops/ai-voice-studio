"""Every generated stylesheet must be balanced Qt CSS.

Qt silently rejects a sheet it cannot parse: the widget renders unstyled and the
only trace is ``Could not parse stylesheet of object X`` on stderr, which nobody
sees unless they go looking at ``app-stderr.log``. The Toast shipped that
warning on every single toast for a long time.

The cause is a footgun in the way these sheets are written. Rules are assembled
from several implicitly-concatenated fragments, the first ones f-strings holding
``{{``/``}}`` to emit real braces, and the last one usually a plain string. In an
f-string ``}}`` means "one }", but in a plain string it is two. One fragment
with the wrong prefix and the sheet gains a stray brace and stops applying.
"""
from __future__ import annotations

import re
import unittest

from app.gui import theme
from app.gui.widgets import Toast

_STRUCTURAL_BRACES = re.compile(r"\{[^{}]*\}")


def _unbalanced_brace(sheet: str) -> bool:
    """True if *sheet* has a brace Qt would choke on.

    Any brace left over after the well-formed ``{...}`` rules are removed is
    stray. The first stray brace is what makes Qt discard the whole sheet, so
    detecting leftovers is enough -- no need to emulate its parser.
    """
    leftover = _STRUCTURAL_BRACES.sub("", sheet)
    return "{" in leftover or "}" in leftover


def _assert_balanced(test: unittest.TestCase, sheet: str, label: str) -> None:
    test.assertNotEqual(
        _unbalanced_brace(sheet),
        True,
        msg=f"{label} produced unbalanced braces:\n  {sheet!r}",
    )


class ThemeSheetTests(unittest.TestCase):
    """The shared helpers every widget is styled through."""

    def test_frame_style_is_balanced(self) -> None:
        for radius in (0, 12):
            for border, width in ((None, 0), (theme.BORDER, 1), (theme.ACCENT, 2)):
                _assert_balanced(
                    self,
                    theme.frame_style(theme.PANEL_BG, radius, border, width),
                    f"frame_style(radius={radius}, border={border}, width={width})",
                )

    def test_label_style_is_balanced(self) -> None:
        for color in (theme.TEXT, theme.SUBTEXT, theme.DANGER, theme.WARNING):
            for align in (None, "left", "center", "right"):
                _assert_balanced(
                    self,
                    theme.label_style(color, align),
                    f"label_style({color}, {align})",
                )

    def test_button_style_is_balanced(self) -> None:
        _assert_balanced(
            self, theme.button_style(), "button_style() defaults"
        )
        _assert_balanced(
            self,
            theme.button_style(theme.CARD_BG, theme.ACCENT, theme.TEXT, 8, theme.BORDER, 1),
            "button_style() with hover and border",
        )

    def test_progress_style_is_balanced(self) -> None:
        _assert_balanced(self, theme.progress_style(), "progress_style()")

    def test_scroll_style_is_balanced(self) -> None:
        _assert_balanced(self, theme.scroll_style(theme.PANEL_BG), "scroll_style()")


class ToastSheetTests(unittest.TestCase):
    """The widget that actually regressed."""

    @classmethod
    def setUpClass(cls) -> None:
        from PyQt5.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def _toast(self):
        """A Toast on its own throwaway parent.

        ``Toast`` takes a QWidget, not the QApplication, so each test gets a
        real parent to read a stylesheet off of.
        """
        from PyQt5.QtWidgets import QWidget

        master = QWidget()
        self.addCleanup(master.deleteLater)
        return Toast(master)

    def _sheet_after_show(self, kind: str) -> str:
        """Read back the sheet the real code produced.

        Deliberately goes through ``Toast.show`` instead of rebuilding the CSS
        here: a copy would keep passing while ``widgets.py`` stayed broken.
        """
        toast = self._toast()
        toast.show("probe", kind)
        return toast.styleSheet()

    def test_constructor_sheet_is_balanced(self) -> None:
        _assert_balanced(self, self._toast().styleSheet(), "Toast.__init__ sheet")

    def test_every_toast_colour_sheet_is_balanced(self) -> None:
        for kind in ("info", "ok", "warn", "error"):
            _assert_balanced(
                self, self._sheet_after_show(kind), f"Toast {kind} sheet"
            )

    def test_sheets_have_no_double_closing_brace(self) -> None:
        """The exact defect, named so the failure message points at it.

        Only safe for the Toast because its sheet is a single rule. Two adjacent
        rules legitimately end with ``}}`` when written as ``...} }``-style CSS,
        which is why the theme tests check balance rather than this substring.
        """
        for kind in ("info", "ok", "warn", "error"):
            sheet = self._sheet_after_show(kind)
            self.assertNotIn(
                "}}", sheet, msg=f"Toast {kind} sheet has a stray brace: {sheet!r}"
            )


if __name__ == "__main__":
    unittest.main()
