"""Tests of the window's building blocks (need a screen: they are skipped where Tk cannot start)."""
from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facesorter import ui_kit  # noqa: E402
from facesorter.ui_kit import ErrorGate, ScrollFrame  # noqa: E402


class Wheel:
    """A mouse-wheel event at a point of the screen."""

    def __init__(self, x: int, y: int, delta: int = -120):
        self.x_root, self.y_root, self.delta = x, y, delta


class WindowTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("no screen for Tk")
        ui_kit.theme(self.root)
        self.root.geometry("400x300+0+0")
        self.page = ScrollFrame(self.root)
        self.page.pack(fill="both", expand=True)
        for k in range(60):  # much taller than the window, so it scrolls
            ttk.Label(self.page.inner, text=f"line {k}").pack()
        for _ in range(5):
            self.root.update()

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            self.root.destroy()

    def middle(self) -> Wheel:
        c = self.page.canvas
        return Wheel(c.winfo_rootx() + c.winfo_width() // 2, c.winfo_rooty() + c.winfo_height() // 2)

    def test_wheel_scrolls_the_page(self) -> None:
        before = self.page.canvas.yview()[0]
        ScrollFrame.dispatch(self.middle(), None)
        self.assertGreater(self.page.canvas.yview()[0], before)

    def test_wheel_over_a_window_without_a_name(self) -> None:
        """v3.3: the open list of a combobox has no name in Python; the wheel over it crashed (KeyError)."""
        def popdown(x, y):
            raise KeyError("popdown")
        self.page.winfo_containing = popdown
        ScrollFrame.dispatch(self.middle(), None)   # must not raise

    def test_wheel_after_a_page_was_closed(self) -> None:
        """Closed pages (e.g. after switching the language) must not break the wheel."""
        old = ScrollFrame(self.root)
        old.pack()
        self.root.update()
        old.destroy()
        ScrollFrame.dispatch(self.middle(), None)   # must not raise
        self.assertGreater(self.page.canvas.yview()[0], 0.0)

    def test_one_wheel_handler_for_the_whole_program(self) -> None:
        for _ in range(5):
            ScrollFrame(self.root)
        handlers = [line for line in self.root.bind_all("<MouseWheel>").splitlines() if line.strip()]
        self.assertEqual(len(handlers), 1, handlers)


class ErrorGateTest(unittest.TestCase):
    def test_same_error_is_shown_once_a_minute(self) -> None:
        g = ErrorGate(quiet=60)
        self.assertTrue(g.allow("KeyError: 'popdown'", now=0))
        self.assertFalse(g.allow("KeyError: 'popdown'", now=1))
        self.assertFalse(g.allow("KeyError: 'popdown'", now=59))
        self.assertTrue(g.allow("KeyError: 'popdown'", now=61))

    def test_another_error_is_shown(self) -> None:
        g = ErrorGate(quiet=60)
        self.assertTrue(g.allow("A", now=0))
        self.assertTrue(g.allow("B", now=1))

    def test_nothing_new_while_a_message_is_open(self) -> None:
        g = ErrorGate()
        g.showing = True
        self.assertFalse(g.allow("C", now=0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
