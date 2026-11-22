"""The window (Tkinter): the library card, the "This computer", "Who is this?" and "Library" tabs,
and the review window.

One language at a time (Arabic by default, English from the language button), laid out from the
right in Arabic. Rows of buttons wrap onto a new line instead of hiding what does not fit.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np

from . import __version__
from .engine import FaceEngine, Filters, decode_jpg
from .i18n import LANGUAGES, language, person_label, rtl, set_language, tr
from .library import _ROOT, LIBRARY_DIR, Library, LockedError, MultipleLibrariesError, device_name
from .perf import LEVELS, Throttle, psutil, speed_label
from .session import (MODES, build_review, discard_session, identify, mode_label, pending_session, save_review,
                      scan, stage_label)

try:  # drag and drop of files onto the window
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # pragma: no cover - the portable build has it
    TkinterDnD = None
    DND_FILES = None

SETTINGS = _ROOT / "settings.json"
ERRORS = _ROOT / "errors.txt"
VERDICT_COLORS = {"sure": "#2c2", "maybe": "#e90", "unknown": "#999"}
LEVEL_COLORS = {"weak": "#c33", "medium": "#c80", "strong": "#2a2"}
CELL = 96


def section_label(section: str) -> str:
    return {"maybe": tr("❓ Is this…?", "❓ محتاج تأكيد"), "sure": tr("✅ Sure", "✅ متأكد"),
            "seen_before": tr("👤 Seen before", "👤 اتشاف قبل كده"), "new": tr("🆕 New people", "🆕 ناس جداد")}[section]


def level_text(level: str) -> str:
    return {"weak": tr("weak", "ضعيف"), "medium": tr("medium", "متوسط"), "strong": tr("strong", "قوي")}[level]


# ---------------------------------------------------------------- direction (Arabic: from the right)
def start() -> str:
    """The side a row starts from: right in Arabic, left in English."""
    return "right" if rtl() else "left"


def end() -> str:
    return "left" if rtl() else "right"


def anchor() -> str:
    return "e" if rtl() else "w"


def justify() -> str:
    return "right" if rtl() else "left"


# ---------------------------------------------------------------- helpers
def _png(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode()


def photo_from_jpg(data: bytes | None, size: int) -> tk.PhotoImage:
    img = decode_jpg(data) if data else None
    if img is None:
        img = np.full((size, size, 3), 70, np.uint8)
    return tk.PhotoImage(data=_png(cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)))


def photo_from_array(img: np.ndarray | None, size: int) -> tk.PhotoImage:
    if img is None:
        img = np.full((size, size, 3), 70, np.uint8)
    return tk.PhotoImage(data=_png(cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)))


def open_path(p: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        os.system(f'open "{p}"')
    else:
        os.system(f'xdg-open "{p}"')


def log_error(where: str, text: str) -> None:
    """Keep the details of a problem in errors.txt next to the program."""
    try:
        with open(ERRORS, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  v{__version__}  {where} =====\n{text}\n")
    except OSError:
        pass


def enable_sharp_text() -> None:
    """Windows: tell it we handle high-resolution screens, so text is not blurry."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def dropped_paths(root: tk.Misc, data: str) -> list[Path]:
    return [Path(p) for p in root.tk.splitlist(data)]


def setting_path(value: str | None) -> Path | None:
    """Library place from settings: relative to the program when on the same drive (USB letters change)."""
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else (_ROOT / p).resolve()


def path_setting(p: Path) -> str:
    try:
        return os.path.relpath(p, _ROOT)
    except ValueError:  # another drive
        return str(p)


def fmt_time(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec // 60) % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def shorten_middle(text: str, font: tkfont.Font, width: int) -> str:
    """Cut the middle of a long path so it fits: E:\\Photos\\…\\Library."""
    if width <= 20 or font.measure(text) <= width:
        return text
    lo, hi = 1, len(text)
    best = "…"
    while lo <= hi:
        keep = (lo + hi) // 2
        cand = text[:keep // 2] + "…" + text[len(text) - (keep - keep // 2):]
        if font.measure(cand) <= width:
            best, lo = cand, keep + 1
        else:
            hi = keep - 1
    return best


# ---------------------------------------------------------------- building blocks
class Flow(ttk.Frame):
    """A row of widgets that wraps onto the next line when the window is too narrow, so nothing is hidden.

    add(widget)            from the start side (right in Arabic)
    add(widget, end=True)  grouped at the other side (in the order given, from the edge inwards)
    """

    def __init__(self, parent, padx: int = 3, pady: int = 3, **kw):
        super().__init__(parent, width=10, height=10, **kw)
        self.padx, self.pady = padx, pady
        self.items: list[tuple[tk.Widget, bool]] = []
        self._pending = False
        self._width = 0
        self.bind("<Configure>", self._on_configure)

    def add(self, widget: tk.Widget, end: bool = False) -> tk.Widget:
        self.items.append((widget, end))
        widget.bind("<Configure>", lambda e: self.relayout(), add="+")
        self.relayout()
        return widget

    def _on_configure(self, e) -> None:
        if e.width != self._width:
            self._width = e.width
            self.relayout()

    def relayout(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self._layout)

    def _layout(self) -> None:
        self._pending = False
        if not self.winfo_exists():
            return
        width = self.winfo_width()
        shown = [(w, e) for w, e in self.items if w.winfo_exists()]
        if width <= 10:  # not on screen yet: one row is enough to say how tall it is
            width = sum(w.winfo_reqwidth() + 2 * self.padx for w, _ in shown) or 10
        rows: list[dict] = []

        def new_row() -> dict:
            rows.append({"start": [], "end": [], "used": 0})
            return rows[-1]
        row = new_row()
        for w, at_end in shown:
            if at_end:
                continue
            need = w.winfo_reqwidth() + 2 * self.padx
            if row["start"] and row["used"] + need > width:
                row = new_row()
            row["start"].append(w)
            row["used"] += need
        for w, at_end in shown:
            if not at_end:
                continue
            need = w.winfo_reqwidth() + 2 * self.padx
            if (row["start"] or row["end"]) and row["used"] + need > width:
                row = new_row()
            row["end"].append(w)
            row["used"] += need
        y = self.pady
        right_to_left = rtl()
        for row in rows:
            ws = row["start"] + row["end"]
            if not ws:
                continue
            h = max(w.winfo_reqheight() for w in ws)
            x = 0
            for w in row["start"]:
                ww = w.winfo_reqwidth()
                left = width - x - self.padx - ww if right_to_left else x + self.padx
                w.place(x=left, y=y + (h - w.winfo_reqheight()) // 2)
                x += ww + 2 * self.padx
            x = 0
            for w in row["end"]:
                ww = w.winfo_reqwidth()
                left = x + self.padx if right_to_left else width - x - self.padx - ww
                w.place(x=left, y=y + (h - w.winfo_reqheight()) // 2)
                x += ww + 2 * self.padx
            y += h + self.pady
        if int(self.cget("height")) != y:
            self.configure(height=y)


class WrapLabel(ttk.Label):
    """A label that breaks its text into lines to fit the width it is given, instead of cutting it."""

    def __init__(self, parent, **kw):
        kw.setdefault("justify", justify())
        kw.setdefault("anchor", anchor())
        super().__init__(parent, width=1, **kw)
        self.bind("<Configure>", lambda e: self.configure(wraplength=max(40, e.width - 4)))


class Tip:
    """A small yellow note when the mouse rests on a widget (e.g. the full library path)."""

    def __init__(self, widget: tk.Widget, text_fn):
        self.widget, self.text_fn, self.win = widget, text_fn, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _=None) -> None:
        text = self.text_fn()
        if not text or self.win:
            return
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        x, y = self.widget.winfo_rootx(), self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.win.wm_geometry(f"+{x}+{y}")
        tk.Label(self.win, text=text, background="#ffffe0", relief="solid", borderwidth=1, padx=6, pady=3,
                 justify=justify(), wraplength=900).pack()

    def hide(self, _=None) -> None:
        if self.win:
            self.win.destroy()
            self.win = None


def choose(parent: tk.Misc, title: str, message: str, options: list[str]) -> int | None:
    """Ask a question with clear buttons (instead of Yes / No). Returns the button's index, None if closed."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    body = ttk.Frame(win, padding=16)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=message, justify=justify(), wraplength=560).pack(anchor=anchor(), pady=(0, 12))
    row = ttk.Frame(body)
    row.pack(fill="x")
    answer: list[int] = []

    def pick(k: int) -> None:
        answer.append(k)
        win.destroy()
    for k, text in enumerate(options):
        ttk.Button(row, text=text, command=lambda k=k: pick(k)).pack(side=start(), padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()
    parent.wait_window(win)
    return answer[0] if answer else None


def ask_yes(parent: tk.Misc, title: str, message: str) -> bool:
    return messagebox.askyesno(title, message, parent=parent)


def layout_problems(root: tk.Misc) -> list[str]:
    """Widgets whose text does not fit, or that stick out of their window (used by the build test)."""
    problems: list[str] = []
    text_kinds = ("TButton", "TLabel", "TCheckbutton", "TRadiobutton", "Button", "Label", "Checkbutton",
                  "Radiobutton")

    def walk(w: tk.Misc, scrolled: bool = False) -> None:
        if isinstance(w, tk.Toplevel) and w is not root:
            return  # other windows are checked on their own
        scrolled = scrolled or isinstance(w, tk.Canvas)  # scrolled content may be taller than the window
        for c in w.winfo_children():
            if not c.winfo_ismapped():
                if c.winfo_manager() in ("pack", "grid", "place") and not isinstance(c, tk.Toplevel):
                    try:  # laid out, but Tk had no room left to show it at all
                        problems.append(f"hidden: {c.winfo_class()} {str(c.cget('text'))!r}")
                    except tk.TclError:
                        problems.append(f"hidden: {c.winfo_class()} {c}")
                continue
            name = c.winfo_class()
            text = ""
            try:
                text = str(c.cget("text"))
            except tk.TclError:
                pass
            if name in text_kinds and text and not isinstance(c, WrapLabel):
                if c.winfo_width() + 1 < c.winfo_reqwidth() or c.winfo_height() + 1 < c.winfo_reqheight():
                    problems.append(f"squeezed: {text!r} ({c.winfo_width()}x{c.winfo_height()} < "
                                    f"{c.winfo_reqwidth()}x{c.winfo_reqheight()})")
            if name in text_kinds and text:
                top = c.winfo_toplevel()
                x, y = c.winfo_rootx() - top.winfo_rootx(), c.winfo_rooty() - top.winfo_rooty()
                if x < -1 or x + c.winfo_width() > top.winfo_width() + 1 or \
                        (not scrolled and (y < -1 or y + c.winfo_height() > top.winfo_height() + 1)):
                    problems.append(f"outside the window: {text!r} at {x},{y} {c.winfo_width()}x{c.winfo_height()}")
                p = c.nametowidget(c.winfo_parent())
                px, py = c.winfo_x(), c.winfo_y()
                if px < -1 or py < -1 or px + c.winfo_width() > p.winfo_width() + 1 or \
                        py + c.winfo_height() > p.winfo_height() + 1:
                    problems.append(f"cut by its box: {text!r}")
            walk(c, scrolled)
    walk(root)
    return problems


class Grid(ttk.Frame):
    """A scrollable grid of small pictures with a state per picture and an optional keyboard cursor.

    items: dicts with  key, jpg (bytes), on (True/False/None), dim (bool), caption (str)
    headers: {index: text} -> a full-width title row before that item (e.g. years)
    """

    def __init__(self, parent, size: int = CELL, on_click=None, on_double=None):
        super().__init__(parent)
        self.size = size
        self.on_click = on_click
        self.on_double = on_double
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#1e1e1e")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=end(), fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        for ev, d in (("<MouseWheel>", None), ("<Button-4>", -1), ("<Button-5>", 1)):
            self.canvas.bind(ev, lambda e, d=d: self.canvas.yview_scroll(d or (-1 if e.delta > 0 else 1), "units"))
        self.items: list[dict] = []
        self.headers: dict[int, str] = {}
        self.cursor = -1
        self.images: dict = {}
        self._pos: list[tuple[int, int]] = []

    def set_items(self, items: list[dict], headers: dict[int, str] | None = None, cursor: int = -1) -> None:
        self.items, self.headers, self.cursor = items, headers or {}, cursor
        for it in items:
            if it["key"] not in self.images:
                self.images[it["key"]] = photo_from_jpg(it.get("jpg"), self.size)
        self.canvas.yview_moveto(0)
        self.draw()

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        width = max(1, c.winfo_width())
        cell = self.size + 12
        cols = max(1, (width - 6) // cell)
        y = 0
        col = 0
        self._pos = []
        for k, it in enumerate(self.items):
            if k in self.headers:
                if col:
                    y += cell
                    col = 0
                c.create_text(width - 8 if rtl() else 8, y + 12, text=self.headers[k], anchor=anchor(), fill="#ddd",
                              font=("TkDefaultFont", 11, "bold"))
                y += 28
            x = width - (col + 1) * cell + 6 if rtl() else col * cell + 6
            self._pos.append((x, y))
            on = it.get("on")
            color = {True: "#3c3", False: "#c33", None: "#333"}[on]
            if k == self.cursor:
                c.create_rectangle(x - 5, y - 5, x + self.size + 5, y + self.size + 5, outline="#fd0", width=3)
            rect = c.create_rectangle(x - 2, y - 2, x + self.size + 2, y + self.size + 2, outline=color, width=3)
            im = c.create_image(x, y, image=self.images[it["key"]], anchor="nw")
            tags = [rect, im]
            if it.get("dim"):
                tags.append(c.create_rectangle(x, y, x + self.size, y + self.size, fill="#000", stipple="gray50",
                                               outline=""))
            if on is False:
                tags.append(c.create_text(x + self.size - 10, y + 10, text="✗", fill="#f55",
                                          font=("TkDefaultFont", 14, "bold")))
            if it.get("caption"):
                tags.append(c.create_text(x + 3, y + self.size - 3, text=it["caption"], anchor="sw", fill="#ff0",
                                          font=("TkDefaultFont", 8, "bold")))
            for t in tags:
                c.tag_bind(t, "<Button-1>", lambda e, k=k: self.on_click and self.on_click(k))
                c.tag_bind(t, "<Double-Button-1>", lambda e, k=k: self.on_double and self.on_double(k))
            col += 1
            if col >= cols:
                col = 0
                y += cell
        c.configure(scrollregion=(0, 0, width, y + cell + 10))

    def show_cursor(self) -> None:
        box = self.canvas.bbox("all")
        if 0 <= self.cursor < len(self._pos) and box:
            self.canvas.yview_moveto(max(0.0, (self._pos[self.cursor][1] - 40) / max(float(box[3]), 1.0)))

    def columns(self) -> int:
        return max(1, (max(1, self.canvas.winfo_width()) - 6) // (self.size + 12))


def fit_window(win: tk.Toplevel | tk.Tk, width: int, height: int) -> None:
    """Open a window at a size that fits the screen (small laptops, big zoom)."""
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = min(width, sw - 40), min(height, sh - 90)
    win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")


# ============================================================== review window
class ReviewWindow(tk.Toplevel):
    def __init__(self, app: "App", review, mode: str, dest: str | None):
        super().__init__(app.root)
        self.app, self.review, self.mode, self.dest = app, review, mode, dest
        self.title(f"{tr('Review', 'المراجعة')}  —  {review.device}  ·  {review.folder}")
        fit_window(self, 1200, 800)
        self.transient(app.root)
        self.names = app.lib.names()

        top = Flow(self, padx=6)
        top.pack(fill="x", padx=10, pady=(8, 0))
        c = review.counts()
        top.add(ttk.Label(top, font=app.f_bold, text=tr(
            f"{len(review.photos)} photos · {len(review.faces)} faces", f"{len(review.photos)} صورة · {len(review.faces)} وش")
            + f"   —   ❓ {c['maybe']}   ✅ {c['sure']}   👤 {c['seen_before']}   🆕 {c['new']}"))
        WrapLabel(self, foreground="#666", text=tr(
            "Keys: ←→↑↓ move · Space ✓/✗ · Del ✗ · Enter = OK, next · N = name · PgDn next",
            "الكيبورد: الأسهم للتنقل · Space ✓/✗ · Del ✗ · Enter تمام واللي بعده · N الاسم · PgDn المجموعة اللي بعدها"
        )).pack(fill="x", padx=12)

        bottom = Flow(self, padx=4)
        bottom.pack(side="bottom", fill="x", padx=10, pady=8)
        where = f"  →  {Path(dest).name}" if dest else ""
        bottom.add(ttk.Label(bottom, text=tr("Mode: ", "الوضع: ") + mode_label(mode) + where))
        bottom.add(ttk.Button(bottom, text=tr("Save", "احفظ"), command=self.save), end=True)
        bottom.add(ttk.Button(bottom, text=tr("Later", "بعدين"), command=self.destroy), end=True)
        bottom.add(ttk.Button(bottom, text=tr("Discard session", "الغي الجلسة"), command=self.discard), end=True)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        self.tree = ttk.Treeview(left, columns=("n",), selectmode="browse", show="tree headings")
        self.tree.heading("#0", text=tr("Groups", "المجموعات"), anchor=anchor())
        self.tree.heading("n", text=tr("Faces", "وشوش"))
        self.tree.column("#0", width=270)
        self.tree.column("n", width=max(60, app.f_bold.measure(tr("Faces", "وشوش")) + 18), anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_group())
        for pane, weight in ((right, 3), (left, 1)) if rtl() else ((left, 1), (right, 3)):
            body.add(pane, weight=weight)

        self.head = WrapLabel(right, font=app.f_head)
        self.head.pack(fill="x")
        self.info = WrapLabel(right, foreground="#555")
        self.info.pack(fill="x")
        namebar = Flow(right, padx=3)
        namebar.pack(fill="x", pady=4)
        namebar.add(ttk.Label(namebar, text=tr("Name:", "الاسم:")))
        self.name_var = tk.StringVar()
        self.name_box = ttk.Combobox(namebar, textvariable=self.name_var, width=24, values=self.names,
                                     justify=justify())
        namebar.add(self.name_box)
        self.name_box.bind("<<ComboboxSelected>>", lambda e: self.name_changed())
        self.name_box.bind("<KeyRelease>", lambda e: self.name_changed())
        self.name_box.bind("<Return>", lambda e: (self.name_changed(), self.focus_set(), "break")[-1])
        self.cands = Flow(right, padx=2)
        self.cands.pack(fill="x")
        self.isolate_btn = ttk.Button(namebar, text=tr("Isolate this person", "اعزل الشخص ده"), command=self.isolate)
        namebar.add(self.isolate_btn, end=True)
        namebar.add(ttk.Button(namebar, text=tr("All ✓", "الكل ✓"), command=lambda: self.set_all(True)), end=True)
        namebar.add(ttk.Button(namebar, text=tr("All ✗", "الكل ✗"), command=lambda: self.set_all(False)), end=True)
        self.grid = Grid(right, on_click=self.click)
        self.grid.pack(fill="both", expand=True)

        self.group_items: dict[str, int] = {}
        self.fill_tree()
        self.bind("<Left>", lambda e: self.move(1 if rtl() else -1))
        self.bind("<Right>", lambda e: self.move(-1 if rtl() else 1))
        self.bind("<Up>", lambda e: self.move(-self.grid.columns()))
        self.bind("<Down>", lambda e: self.move(self.grid.columns()))
        self.bind("<space>", lambda e: self.key_toggle())
        self.bind("<Delete>", lambda e: self.key_set(False))
        self.bind("<Return>", lambda e: self.accept_and_next())
        self.bind("<Next>", lambda e: self.next_group())
        self.bind("<Prior>", lambda e: self.next_group(-1))
        self.bind("<KeyPress-n>", lambda e: self.focus_name())
        self.bind("<KeyPress-N>", lambda e: self.focus_name())
        self.focus_set()

    # ---- tree
    def label(self, g) -> str:
        if g.section == "maybe":
            return f"{g.name}?"
        if g.name:
            return g.name
        if g.section == "seen_before":
            return tr(f"Unknown_{g.person_id:04d} · {g.seen_count}× · {g.devices} PC",
                      f"مجهول_{g.person_id:04d} · {g.seen_count} مرة · {g.devices} جهاز")
        return tr(f"New person {self.review.groups.index(g) + 1}", f"شخص جديد {self.review.groups.index(g) + 1}")

    def fill_tree(self) -> None:
        for sec in ("maybe", "sure", "seen_before", "new"):
            groups = [k for k, g in enumerate(self.review.groups) if g.section == sec]
            if not groups:
                continue
            parent = self.tree.insert("", "end", text=section_label(sec), values=(len(groups),), open=True)
            for k in groups:
                g = self.review.groups[k]
                item = self.tree.insert(parent, "end", text=self.label(g), values=(len(g.faces),))
                self.group_items[item] = k
        first = next(iter(self.group_items), None)
        if first:
            self.tree.selection_set(first)
        else:
            self.head.config(text=tr("No faces found in these photos.", "مفيش وشوش في الصور دي."))

    def current(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.group_items:
            return None
        return self.review.groups[self.group_items[sel[0]]]

    def refresh_row(self) -> None:
        sel = self.tree.selection()
        g = self.current()
        if g:
            ok = sum(1 for i in g.faces if g.ok(i))
            self.tree.item(sel[0], text=self.label(g), values=(f"{ok}/{len(g.faces)}",))

    def show_group(self) -> None:
        g = self.current()
        if g is None:
            return
        rv = self.review
        self.head.config(text=f"{section_label(g.section)}   —   {self.label(g)}")
        bits = [tr(f"{len(g.faces)} faces", f"{len(g.faces)} وش")]
        low = sum(1 for i in g.faces if not rv.faces[i].good)
        if low:
            bits.append(tr(f"{low} low quality (dark: sorted, not learned)",
                           f"{low} مش واضحين (الغامقين: بيترتبوا ومش بيتعلم منهم)"))
        if g.already:
            bits.append(tr(f"{len(g.already)} already learned", f"{len(g.already)} اتعلموا قبل كده"))
        if g.section == "seen_before":
            bits.append(tr(f"seen {g.seen_count} times on {g.devices} computer(s): give a name to remember them",
                           f"اتشاف {g.seen_count} مرة على {g.devices} جهاز: سمّيه عشان البرنامج يفتكره"))
        if g.pool:
            bits.append(tr(f"+{len(g.pool)} faces from earlier visits", f"+{len(g.pool)} وش من زيارات قبل كده"))
        self.info.config(text="   ·   ".join(bits))
        self.name_var.set(g.name)
        known = g.section in ("sure", "maybe") and g.person_id is not None
        self.isolate_btn.config(state="normal" if known else "disabled")
        for w in self.cands.winfo_children():
            w.destroy()
        self.cands.items.clear()
        for cname, score in g.candidates[:3]:
            self.cands.add(ttk.Button(self.cands, text=f"{cname} ({score:.2f})",
                                      command=lambda n=cname: (self.name_var.set(n), self.name_changed())))
        self.cands.relayout()
        self.draw_faces(cursor=0)

    def draw_faces(self, cursor: int | None = None) -> None:
        g = self.current()
        if g is None:
            return
        rv = self.review
        items = [{"key": i, "jpg": rv.faces[i].crop, "on": g.ok(i), "dim": not rv.faces[i].good,
                  "caption": tr("✓ learned", "✓ اتعلم") if i in g.already else ""} for i in g.faces]
        if cursor is None:
            cursor = self.grid.cursor
        self.grid.set_items(items, cursor=cursor)
        self.refresh_row()

    # ---- editing
    def name_changed(self) -> None:
        g = self.current()
        if g is not None and g.name != self.name_var.get():
            g.name = self.name_var.get()
            g.touched = True
            self.refresh_row()
            self.head.config(text=f"{section_label(g.section)}   —   {self.label(g)}")

    def click(self, k: int) -> None:
        g = self.current()
        if g is None:
            return
        i = g.faces[k]
        g.set(i, not g.ok(i))
        self.grid.cursor = k
        self.draw_faces()

    def set_all(self, value: bool) -> None:
        g = self.current()
        if g:
            for i in g.faces:
                g.set(i, value)
            self.draw_faces()

    def _typing(self) -> bool:
        return self.focus_get() is self.name_box

    def move(self, d: int):
        if self._typing():
            return None
        g = self.current()
        if g and g.faces:
            self.grid.cursor = max(0, min(len(g.faces) - 1, self.grid.cursor + d))
            self.grid.draw()
            self.grid.show_cursor()
        return "break"

    def key_toggle(self):
        if self._typing():
            return None
        g = self.current()
        if g and 0 <= self.grid.cursor < len(g.faces):
            self.click(self.grid.cursor)
        return "break"

    def key_set(self, value: bool):
        if self._typing():
            return None
        g = self.current()
        if g and 0 <= self.grid.cursor < len(g.faces):
            g.set(g.faces[self.grid.cursor], value)
            self.grid.cursor = min(len(g.faces) - 1, self.grid.cursor + 1)
            self.draw_faces()
        return "break"

    def accept_and_next(self):
        if self._typing():
            return None
        g = self.current()
        if g and g.section == "maybe" and not any(g.ok(i) for i in g.faces):
            for i in g.faces:  # Enter on "is this X?" means yes
                g.set(i, True)
            self.draw_faces()
        self.next_group()
        return "break"

    def next_group(self, d: int = 1) -> str:
        items = list(self.group_items)
        sel = self.tree.selection()
        if items:
            k = items.index(sel[0]) if sel and sel[0] in items else -1
            k = max(0, min(len(items) - 1, k + d))
            self.tree.selection_set(items[k])
            self.tree.see(items[k])
        return "break"

    def focus_name(self):
        if self._typing():
            return None
        self.name_box.focus_set()
        self.name_box.select_range(0, "end")
        return "break"

    def isolate(self) -> None:
        """This is not who the program thinks: take the person out of the known list (they go back to
        the unknown people, to be named again). Their faces here become 'seen before'."""
        g = self.current()
        if g is None or g.person_id is None or g.section not in ("sure", "maybe"):
            return
        old = g.name
        if not ask_yes(self, tr("Isolate", "عزل"), tr(
                f"Take '{old}' out of the known people? Their faces go back to the unknown people, to be named again.",
                f"تعزل '{old}'؟ وشوشه هترجع للمجهولين عشان تتسمّى تاني.")):
            return
        self.app._append(self.app.lib.isolate([g.person_id]))
        for other in self.review.groups:  # every group of that person in this review
            if other.person_id == g.person_id and other.section in ("sure", "maybe"):
                p = self.app.lib.person(g.person_id)
                other.seen_count, other.devices = (p.seen_count, len(p.devices)) if p else (0, 0)
                other.section, other.name, other.candidates = "seen_before", "", []
                other.accepted = {i: True for i in other.faces}
                other.touched = True
        self.tree.delete(*self.tree.get_children())
        self.group_items.clear()
        self.fill_tree()

    # ---- finish
    def save(self) -> None:
        dest = self.dest
        if self.mode != "learn" and not dest:
            dest = filedialog.askdirectory(parent=self, title=tr("Where to put the sorted photos",
                                                                 "تحط الصور المرتبة فين"))
            if not dest:
                return
        maybe_left = sum(1 for g in self.review.groups if g.section == "maybe" and not any(g.ok(i) for i in g.faces))
        if maybe_left and not ask_yes(self, tr("Save", "احفظ"), tr(
                f"{maybe_left} '❓ Is this…?' group(s) were not confirmed; they stay unknown. Save anyway?",
                f"فيه {maybe_left} مجموعة محتاجة تأكيد متأكدتهاش، وهتفضل مجهولة. تحفظ؟")):
            return
        review, mode, app = self.review, self.mode, self.app
        others = app.others_var.get()
        self.destroy()
        app.run_job(lambda: app.finish_save(save_review(app.lib, review, mode, Path(dest) if dest else None,
                                                        others, app.on_progress, app.log)))

    def discard(self) -> None:
        if ask_yes(self, tr("Discard", "إلغاء"), tr("Forget this session without learning anything?",
                                                    "تلغي الجلسة دي من غير ما تتعلم حاجة؟")):
            discard_session(self.app.lib, self.review.session_id)
            self.app.refresh_all()
            self.destroy()


# ===================================================================== app
class App:
    def __init__(self) -> None:
        enable_sharp_text()
        self.root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        scale = os.environ.get("FACE_SORTER_UI_SCALE")  # the build test tries 100%, 125% and 150%
        if scale:
            self.root.tk.call("tk", "scaling", float(scale) * 96 / 72)
        self.root.report_callback_exception = self.on_error
        try:
            self.settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.settings = {}
        st = self.settings
        set_language(os.environ.get("FACE_SORTER_LANGUAGE") or st.get("language", "ar"))
        self.setup_style()
        fit_window(self.root, 1280, 880)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.minsize(min(820, sw - 40), min(560, sh - 90))
        if (sw < 1300 or sh < 800) and sys.platform.startswith("win"):
            self.root.state("zoomed")  # a small screen: use all of it
        self.lib = self.startup_library()
        if self.lib is None:
            self.root.destroy()
            raise SystemExit(0)
        self.engine: FaceEngine | None = None
        self.msgs: queue.Queue = queue.Queue()
        self.busy = False
        self.throttle = Throttle(st.get("speed", "balanced"), st.get("battery_saver", True))
        self.src_var = tk.StringVar(value=st.get("source", ""))
        self.dest_var = tk.StringVar(value=st.get("dest", ""))
        self.mode_var = tk.StringVar(value=st.get("mode", "learn"))
        self.strict_var = tk.IntVar(value=st.get("strictness", 5))
        self.minp_var = tk.IntVar(value=st.get("min_photos", 2))
        self.others_var = tk.BooleanVar(value=st.get("others", True))
        self.speed_var = tk.StringVar(value=self.throttle.level)
        self.battery_var = tk.BooleanVar(value=self.throttle.battery_saver)
        self.f_shots = tk.BooleanVar(value=st.get("skip_screenshots", True))
        self.f_stickers = tk.BooleanVar(value=st.get("skip_stickers", True))
        self.f_small = tk.BooleanVar(value=st.get("skip_small", True))
        self.f_exclude = tk.StringVar(value=st.get("exclude", ""))
        self.started = 0.0
        self.stage = (0, 0, 0)
        self.stage_start = (0, 0.0, 0)
        self.paused_at = 0.0
        self.ui: ttk.Frame | None = None
        self.build_ui()
        self.root.after(100, self.pump)

    def setup_style(self) -> None:
        """Slightly bigger text, clear tabs, and fonts for titles."""
        base = tkfont.nametofont("TkDefaultFont")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=10)
            except tk.TclError:
                pass
        family = base.actual("family")
        self.f_title = tkfont.Font(self.root, family=family, size=14, weight="bold")
        self.f_head = tkfont.Font(self.root, family=family, size=12, weight="bold")
        self.f_bold = tkfont.Font(self.root, family=family, size=10, weight="bold")
        self.f_tab = tkfont.Font(self.root, family=family, size=11, weight="bold")
        style = ttk.Style(self.root)
        style.configure("TNotebook", tabposition="ne" if rtl() else "nw")
        style.configure("TNotebook.Tab", padding=(16, 6), font=self.f_tab)
        style.configure("Card.TFrame", relief="groove", borderwidth=2)
        style.configure("People.Treeview", rowheight=44)
        style.configure("Treeview.Heading", font=self.f_bold)

    def build_ui(self) -> None:
        """Make (or remake, after changing the language) everything inside the window."""
        self.root.title(f"Face Sorter v{__version__} - " + tr("Face library", "مكتبة الوشوش"))
        self.ui = ttk.Frame(self.root)
        self.ui.pack(fill="both", expand=True)
        self._build_card()
        nb = ttk.Notebook(self.ui)
        nb.pack(fill="both", expand=True, padx=6, pady=(6, 6))
        self.nb = nb
        self.tab_pc = ttk.Frame(nb, padding=10)
        self.tab_who = ttk.Frame(nb, padding=10)
        self.tab_lib = ttk.Frame(nb, padding=10)
        tabs = [(self.tab_pc, tr("🖥  This computer", "🖥  الجهاز ده")),
                (self.tab_who, tr("🔍  Who is this?", "🔍  مين ده؟")),
                (self.tab_lib, tr("📚  Library", "📚  المكتبة"))]
        for frame, text in (reversed(tabs) if rtl() else tabs):  # Arabic: the first tab is on the right
            nb.add(frame, text=f" {text} ")
        nb.select(self.tab_pc)
        nb.bind("<<NotebookTabChanged>>", lambda e: self.on_tab())
        self._build_pc()
        self._build_who()
        self._build_library()
        self.refresh_all()

    def on_tab(self) -> None:
        if self.nb.select() == str(self.tab_lib):
            self.refresh_library()

    def change_language(self) -> None:
        if self.busy:
            messagebox.showinfo("Face Sorter", tr("Wait until the current work finishes.",
                                                  "استنى لما الشغل اللي شغال يخلص."))
            return
        new = "en" if language() == "ar" else "ar"
        self.settings["language"] = new
        self.remember()
        set_language(new)
        for w in self.root.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.destroy()
        if self.ui is not None:
            self.ui.destroy()
        self.setup_style()
        self.build_ui()

    # ============================================================ this computer
    def _build_pc(self) -> None:
        f = self.tab_pc
        pad = {"padx": 6, "pady": 3}
        src = ttk.Frame(f)
        src.pack(fill="x", **pad)
        self.lib_label = ttk.Label(src, font=self.f_bold)
        self.lib_label.pack(side=start())
        ttk.Button(src, text="...", width=4, command=lambda: self.pick(self.src_var)).pack(side=end(), padx=(4, 0))
        src_entry = ttk.Entry(src, textvariable=self.src_var)
        src_entry.pack(side=start(), fill="x", expand=True, padx=6)
        if TkinterDnD:  # drop a folder of photos here
            src_entry.drop_target_register(DND_FILES)
            src_entry.dnd_bind("<<Drop>>", lambda e: self.drop_folder(e.data))

        head = ttk.Frame(f)
        head.pack(fill="x", **pad)
        self.settings_btn = ttk.Button(head, command=lambda: self.show_settings(not self.settings_shown, True))
        self.settings_btn.pack(side=start())
        self.settings_summary = WrapLabel(head, foreground="#555")
        self.settings_summary.pack(side=start(), fill="x", expand=True, padx=8)
        box = ttk.Frame(f, style="Card.TFrame", padding=(8, 2, 8, 4))
        self.settings_head, self.settings_box = head, box

        def row(title: str) -> Flow:
            r = Flow(box, padx=5, pady=2)
            r.pack(fill="x")
            r.add(ttk.Label(r, text=title, font=self.f_bold, foreground="#345"))
            return r
        modes = row(tr("Mode:", "الوضع:"))
        for m in MODES:
            modes.add(ttk.Radiobutton(modes, text=mode_label(m), value=m, variable=self.mode_var,
                                      command=self.mode_changed))
        modes.add(ttk.Label(modes, text=tr("Sorted photos to:", "الصور المرتبة في:")))
        self.dest_entry = ttk.Entry(modes, textvariable=self.dest_var, width=30)
        modes.add(self.dest_entry)
        self.dest_btn = ttk.Button(modes, text="...", width=4, command=lambda: self.pick(self.dest_var))
        modes.add(self.dest_btn)

        filt = row(tr("Skip:", "تجاهل:"))
        filt.add(ttk.Checkbutton(filt, text=tr("Screenshots", "سكرين شوت"), variable=self.f_shots))
        filt.add(ttk.Checkbutton(filt, text=tr("Stickers", "ستيكرات"), variable=self.f_stickers))
        filt.add(ttk.Checkbutton(filt, text=tr("Small pictures (<300px)", "صور صغيرة (أقل من 300 نقطة)"),
                                 variable=self.f_small))
        filt.add(ttk.Label(filt, text=tr("Folders (comma-separated):", "فولدرات (بينهم فاصلة):")))
        filt.add(ttk.Entry(filt, textvariable=self.f_exclude, width=24))

        opts = row(tr("Options:", "اختيارات:"))
        opts.add(ttk.Label(opts, text=tr("Strictness", "الدقة")))
        s_label = ttk.Label(opts, width=3)

        def on_strict(_=None):
            self.strict_var.set(round(float(self.strict_var.get())))
            s_label.config(text=str(self.strict_var.get()))
        opts.add(ttk.Scale(opts, from_=1, to=10, variable=self.strict_var, command=on_strict, length=140))
        opts.add(s_label)
        on_strict()
        opts.add(ttk.Label(opts, text=tr("Min photos for a new person", "أقل عدد صور لشخص جديد")))
        opts.add(ttk.Spinbox(opts, from_=1, to=20, textvariable=self.minp_var, width=4))
        opts.add(ttk.Checkbutton(opts, text=tr("Copy Others / No_faces too", "انسخ Others و No_faces كمان"),
                                 variable=self.others_var))

        speed = row(tr("Speed:", "السرعة:"))
        for lvl in LEVELS:
            speed.add(ttk.Radiobutton(speed, text=speed_label(lvl), value=lvl, variable=self.speed_var,
                                      command=self.on_speed))
        cb = ttk.Checkbutton(speed, text=tr("🔋 Quiet on battery", "🔋 هادي على البطارية"), variable=self.battery_var,
                             command=self.on_speed)
        speed.add(cb)
        if psutil is None:
            cb.config(state="disabled")
        self.battery_label = ttk.Label(speed, foreground="#c60")
        speed.add(self.battery_label)
        for var in (self.mode_var, self.speed_var, self.strict_var):
            var.trace_add("write", lambda *_: self.update_settings_summary())
        self.settings_shown = False
        self.show_settings(self.settings.get("settings_shown", True), False)
        self.tab_pc.bind("<Configure>", lambda e: self.root.after(300, self.fold_if_short), add="+")

        btns = Flow(f, padx=3)
        btns.pack(side="bottom", fill="x", **pad)
        self.who_btn = ttk.Button(btns, text=tr("🔍 Who is this?", "🔍 مين ده؟"),
                                  command=lambda: self.nb.select(self.tab_who))
        self.resume_btn = ttk.Button(btns, text=tr("Continue review", "كمّل المراجعة"), command=self.resume_review)
        self.start_btn = ttk.Button(btns, text=tr("▶ Start", "▶ ابدأ"), command=self.start)
        self.pause_btn = ttk.Button(btns, text=tr("⏸ Pause", "⏸ إيقاف مؤقت"), state="disabled",
                                    command=self.toggle_pause)
        self.stop_btn = ttk.Button(btns, text=tr("■ Stop", "■ وقّف"), state="disabled", command=self.throttle.stop.set)
        self.btns = btns
        btns.add(self.who_btn)
        btns.add(self.resume_btn)
        for b in (self.start_btn, self.stop_btn, self.pause_btn):
            btns.add(b, end=True)

        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.pack(fill="x", padx=6, pady=(10, 2))
        info = ttk.Frame(f)
        info.pack(fill="x", padx=6)
        self.progress_label = ttk.Label(info, font=self.f_bold)
        self.progress_label.pack(side=start())
        self.time_label = ttk.Label(info)
        self.time_label.pack(side=end())
        self.logbox = tk.Text(f, height=5, state="disabled", wrap="word")
        self.logbox.tag_configure("line", justify=justify())
        self.logbox.pack(fill="both", expand=True, **pad)
        self.mode_changed()

    def show_settings(self, shown: bool, by_user: bool) -> None:
        """The settings can be folded away, so the messages keep their room on small screens."""
        self.settings_shown = shown
        if by_user:
            self.settings["settings_shown"] = shown
            self.settings_touched = True
            self.remember()
        if shown:
            self.settings_box.pack(fill="x", padx=6, pady=3, after=self.settings_head)
        else:
            self.settings_box.pack_forget()
        self.settings_btn.config(text=("▾ " if shown else "▸ ") + tr("⚙ Settings", "⚙ الإعدادات"))
        self.update_settings_summary()

    def update_settings_summary(self) -> None:
        if not hasattr(self, "settings_summary") or not self.settings_summary.winfo_exists():
            return
        text = "" if self.settings_shown else (
            mode_label(self.mode_var.get()) + "  ·  " + speed_label(self.speed_var.get()) + "  ·  "
            + tr("strictness ", "الدقة ") + str(self.strict_var.get()))
        self.settings_summary.config(text=text)

    def fold_if_short(self) -> None:
        """A short window (small screen, big zoom): fold the settings once, unless the user chose."""
        if getattr(self, "settings_touched", False) or not self.settings_shown:
            return
        if not self.root.winfo_ismapped() or self.root.winfo_height() < 300 or not self.logbox.winfo_exists():
            return
        if self.nb.select() == str(self.tab_pc) and (not self.logbox.winfo_ismapped()
                                                      or self.logbox.winfo_height() < 50):
            self.show_settings(False, False)

    def mode_changed(self) -> None:
        state = "disabled" if self.mode_var.get() == "learn" else "normal"
        self.dest_entry.config(state=state)
        self.dest_btn.config(state=state)

    def pick(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def on_speed(self) -> None:
        self.throttle.level = self.speed_var.get()
        self.throttle.battery_saver = self.battery_var.get()
        self.remember()

    def toggle_pause(self) -> None:
        if self.throttle.pause.is_set():
            self.throttle.pause.clear()
            st, t0, d0 = self.stage_start
            self.stage_start = (st, t0 + (time.monotonic() - self.paused_at), d0)
            self.pause_btn.config(text=tr("⏸ Pause", "⏸ إيقاف مؤقت"))
            self._append(tr("Continuing...", "بكمّل..."))
        else:
            self.throttle.pause.set()
            self.paused_at = time.monotonic()
            self.pause_btn.config(text=tr("▶ Continue", "▶ كمّل"))
            self._append(tr("Pausing after the current small batch...", "هقف بعد الدفعة الصغيرة اللي شغالة..."))

    def filters(self) -> Filters:
        return Filters(self.f_shots.get(), self.f_stickers.get(), 300 if self.f_small.get() else 0,
                       tuple(x.strip() for x in self.f_exclude.get().split(",") if x.strip()))

    def start(self) -> None:
        src = self.src_var.get().strip()
        mode = self.mode_var.get()
        if not src:
            messagebox.showwarning("Face Sorter", tr("Choose the photos folder first.", "اختار فولدر الصور الأول."))
            return
        if mode != "learn" and not self.dest_var.get().strip():
            d = filedialog.askdirectory(title=tr("Where to put the sorted photos", "تحط الصور المرتبة فين"))
            if not d:
                return
            self.dest_var.set(d)
        self.remember()
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.config(state="disabled")
        lib, flt, strict, minp = self.lib, self.filters(), self.strict_var.get(), max(1, int(self.minp_var.get()))
        self.on_speed()

        def job():
            sid = scan(lib, Path(src), flt, self.throttle, self.on_progress, self.log)
            if self.throttle.stop.is_set():
                return
            self.msgs.put(("review", build_review(lib, sid, strict, minp, self.on_progress)))
        self.run_job(job)

    def resume_review(self) -> None:
        p = pending_session(self.lib)
        if not p:
            return
        lib, strict, minp = self.lib, self.strict_var.get(), max(1, int(self.minp_var.get()))
        self.run_job(lambda: self.msgs.put(("review", build_review(lib, p[0], strict, minp, self.on_progress))))

    def open_review(self, review) -> None:
        mode = self.mode_var.get()
        ReviewWindow(self, review, mode, self.dest_var.get().strip() if mode != "learn" else None)

    def finish_save(self, report: str) -> None:
        self.msgs.put(("report", report))

    # ================================================================ library tab
    def _build_library(self) -> None:
        f = self.tab_lib
        self.lib_stats = WrapLabel(f, font=self.f_bold)
        self.lib_stats.pack(fill="x")
        bar = Flow(f, padx=3)
        bar.pack(fill="x", pady=(6, 0))
        self.lib_buttons = []
        for text, cmd in ((tr("🔍 Who is this?", "🔍 مين ده؟"), lambda: self.nb.select(self.tab_who)),
                          (tr("Possible duplicates", "يمكن متكررين"), self.open_duplicates),
                          (tr("Sessions & undo", "الجلسات والتراجع"), self.open_sessions),
                          (tr("Log", "السجل"), self.open_log),
                          (tr("Backups", "النسخ الاحتياطية"), self.open_backups),
                          (tr("Reset learning", "امسح التعلم التلقائي"), self.reset_learning),
                          (tr("About", "عن البرنامج"), self.show_about),
                          (tr("↻ Refresh", "↻ حدّث"), self.refresh_library)):
            b = ttk.Button(bar, text=text, command=cmd)
            bar.add(b)
            self.lib_buttons.append(b)

        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        for pane, weight in ((right, 5), (left, 2)) if rtl() else ((left, 2), (right, 5)):
            body.add(pane, weight=weight)

        pbar = ttk.Frame(left)
        pbar.pack(side="bottom", fill="x", pady=4)
        WrapLabel(pbar, text=tr("Selected people (Ctrl/Shift for more):", "الناس المحددين (Ctrl أو Shift لأكتر من واحد):"),
                  foreground="#666").pack(fill="x")
        prow = Flow(pbar, padx=2)
        prow.pack(fill="x")
        for text, cmd in ((tr("Name", "سمّي"), self.rename_person),
                          (tr("Join", "ادمج"), self.join_people),
                          (tr("Isolate", "اعزل"), self.isolate_people),
                          (tr("Delete", "امسح"), self.delete_person),
                          (tr("Export…", "صدّر…"), self.export_people)):
            b = ttk.Button(prow, text=text, command=cmd)
            prow.add(b)
            self.lib_buttons.append(b)
        self.tree = ttk.Treeview(left, columns=("n", "seen"), style="People.Treeview", selectmode="extended")
        self.tree.heading("#0", text=tr("Person", "الشخص"), anchor=anchor())
        self.tree.heading("n", text=tr("Faces", "وشوش"))
        self.tree.heading("seen", text=tr("Seen", "اتشاف"))
        self.tree.column("#0", width=210, stretch=True)
        for col, title in (("n", tr("Faces", "وشوش")), ("seen", tr("Seen", "اتشاف"))):
            self.tree.column(col, width=max(56, self.f_bold.measure(title) + 18), anchor="center", stretch=False)
        for lvl, col in LEVEL_COLORS.items():
            self.tree.tag_configure(lvl, foreground=col)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=end(), fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_person())

        self.p_head = WrapLabel(right, font=self.f_head)
        self.p_head.pack(fill="x")
        self.p_info = WrapLabel(right, foreground="#555")
        self.p_info.pack(fill="x")
        act = Flow(right, padx=2)
        act.pack(fill="x", pady=4)
        b = ttk.Button(act, text=tr("Not this person", "مش الشخص ده"), command=self.not_this_person)
        act.add(b)
        self.lib_buttons.append(b)
        self.move_var = tk.StringVar()
        self.move_box = ttk.Combobox(act, textvariable=self.move_var, width=20, justify=justify())
        b = ttk.Button(act, text=tr("Move to →", "انقلهم لـ ←"), command=self.move_prints)
        act.add(b, end=True)
        act.add(self.move_box, end=True)
        self.lib_buttons.append(b)
        self.p_grid = Grid(right, size=88, on_click=self.toggle_print)
        self.p_grid.pack(fill="both", expand=True)
        self.tree_rows: dict[str, int] = {}
        self.tree_images: list = []
        self.p_selected: set[int] = set()

    def refresh_library(self) -> None:
        lib = self.lib
        st = lib.stats()
        self.lib_stats.config(text=tr(
            f"{st['known']} people · {st['unknown']} unknown · {st['prints']} face prints · "
            f"{st['sessions']} visits on {st['devices']} computer(s) · {st['size_mb']} MB",
            f"{st['known']} شخص · {st['unknown']} مجهول · {st['prints']} بصمة وش · "
            f"{st['sessions']} زيارة على {st['devices']} جهاز · {st['size_mb']} ميجا"))
        keep = self.selected_pid()
        self.tree.delete(*self.tree.get_children())
        self.tree_rows.clear()
        self.tree_images.clear()
        known = lib.people("known")
        unknown = lib.people("unknown")
        k = self.tree.insert("", "end", text=tr(f" Known ({len(known)})", f" معروفين ({len(known)})"), open=True)
        u = self.tree.insert("", "end", text=tr(f" Unknown ({len(unknown)})", f" مجهولين ({len(unknown)})"), open=True)
        frequent = {p.id for p in lib.frequent_unknowns()}
        reselect = None
        for parent, people in ((k, known), (u, unknown)):
            for p in people:
                img = photo_from_array(lib.thumb(p.id), 38)
                self.tree_images.append(img)
                text = f" {person_label(p.label)}" + ("  ⭐" if p.id in frequent else "")
                item = self.tree.insert(parent, "end", text=text, image=img, values=(p.prints, p.seen_count),
                                        tags=(p.level,) if p.kind == "known" else ())
                self.tree_rows[item] = p.id
                if p.id == keep:
                    reselect = item
        self.move_box.config(values=lib.names())
        if reselect:
            self.tree.selection_set(reselect)
            self.tree.see(reselect)
        else:
            self.p_grid.set_items([])
            self.p_head.config(text=tr("Choose a person", "اختار شخص"))
            self.p_info.config(text="")

    def selected_pid(self) -> int | None:
        pids = self.selected_pids()
        return pids[0] if pids else None

    def selected_pids(self) -> list[int]:
        return [self.tree_rows[i] for i in self.tree.selection() if i in self.tree_rows]

    def join_people(self) -> None:
        pids = self.selected_pids()
        if len(pids) < 2:
            messagebox.showinfo(tr("Join", "دمج"), tr("Select two or more people (Ctrl+click).",
                                                      "اختار شخصين أو أكتر (Ctrl مع الضغط)."))
            return
        names = [p.name for p in (self.lib.person(x) for x in pids) if p and p.name]
        name = simpledialog.askstring(tr("Join", "دمج"), tr(f"Join {len(pids)} people as one. Name:",
                                                            f"دمج {len(pids)} شخص في شخص واحد. الاسم:"),
                                      initialvalue=names[0] if names else "", parent=self.root)
        if name and name.strip():
            self._changed(self.lib.merge_many(pids, name))

    def isolate_people(self) -> None:
        pids = [x for x in self.selected_pids() if (p := self.lib.person(x)) and p.kind == "known"]
        if not pids:
            messagebox.showinfo(tr("Isolate", "عزل"), tr("Select known people to isolate.", "اختار ناس معروفين."))
            return
        labels = ", ".join(person_label(self.lib.person(x).label) for x in pids)
        if ask_yes(self.root, tr("Isolate", "عزل"), tr(
                f"Isolate {labels}?\nThey go back to the unknown people, to be named again.",
                f"تعزل {labels}؟\nهيرجعوا للمجهولين عشان يتسمّوا تاني.")):
            self._changed(self.lib.isolate(pids))

    def export_people(self) -> None:
        pids = self.selected_pids()
        if not pids:
            messagebox.showinfo(tr("Export", "تصدير"), tr("Select the people to export.", "اختار الناس الأول."))
            return
        d = filedialog.askdirectory(title=tr("Where to create the new library", "تعمل المكتبة الجديدة فين"))
        if not d:
            return
        dest = Path(d) / f"FaceSorter_Library_export_{datetime.now():%Y-%m-%d_%H%M}"
        path = self.lib.export_people(pids, dest)
        messagebox.showinfo(tr("Exported", "اتصدّرت"), tr(f"A new library with {len(pids)} people:\n{path}\n\n"
                                                          "(It has no password.)",
                                                          f"مكتبة جديدة فيها {len(pids)} شخص:\n{path}\n\n"
                                                          "(من غير باسورد.)"))

    def show_person(self) -> None:
        pid = self.selected_pid()
        if pid is None:
            return
        p = self.lib.person(pid)
        if p is None:
            return
        self.p_selected = set()
        level = tr(f"  ·  knows them: {level_text(p.level)}", f"  ·  معرفته بيه: {level_text(p.level)}") \
            if p.kind == "known" else ""
        self.p_head.config(text=f"{person_label(p.label)}{level}")
        bits = [tr(f"{p.manual} confirmed + {p.auto} learned faces", f"{p.manual} وش متأكد + {p.auto} اتعلم لوحده"),
                tr(f"{p.looks} look(s)", f"{p.looks} شكل"),
                tr(f"seen {p.seen_count} times", f"اتشاف {p.seen_count} مرة"),
                tr("on: ", "على: ") + (", ".join(p.devices) or "-"),
                tr("last: ", "آخر مرة: ") + (p.last_seen or "-")]
        if p.kind == "unknown":
            bits.insert(0, tr("no name yet: use 'Name' to remember them", "لسه من غير اسم: دوس 'سمّي' عشان يفتكره"))
        if p.note:
            bits.insert(0, p.note)
        self.p_info.config(text="   ·   ".join(bits))
        items, headers, last_year = [], {}, None
        for k, (print_id, crop, date, source, quality, device) in enumerate(self.lib.prints_of(pid)):
            year = (date or "")[:4] or "?"
            if year != last_year:
                headers[k] = year if year != "?" else tr("No date", "من غير تاريخ")
                last_year = year
            items.append({"key": f"p{print_id}", "jpg": crop, "on": None, "pid": print_id,
                          "caption": tr("auto", "تلقائي") if source == "auto" else ""})
        self.p_grid.set_items(items, headers)

    def toggle_print(self, k: int) -> None:
        it = self.p_grid.items[k]
        if it["pid"] in self.p_selected:
            self.p_selected.discard(it["pid"])
            it["on"] = None
        else:
            self.p_selected.add(it["pid"])
            it["on"] = False
        self.p_grid.draw()

    def _changed(self, msg: str) -> None:
        if msg:
            self._append(msg)
        self.refresh_all()
        self.refresh_library()

    def rename_person(self) -> None:
        pid = self.selected_pid()
        p = self.lib.person(pid) if pid else None
        if not p:
            return
        new = simpledialog.askstring(tr("Name", "الاسم"), tr("Name (a name already in the library joins them):",
                                                             "الاسم (لو الاسم موجود، الاتنين هيتجمعوا):"),
                                     initialvalue=p.name or "", parent=self.root)
        if new and new.strip():
            self._changed(self.lib.rename(pid, new))

    def delete_person(self) -> None:
        people = [p for p in (self.lib.person(x) for x in self.selected_pids()) if p]
        if not people:
            return
        labels = ", ".join(person_label(p.label) for p in people)
        if ask_yes(self.root, tr("Delete", "مسح"), tr(f"Delete {labels} and everything the library knows about them?",
                                                      f"تمسح {labels} وكل اللي المكتبة تعرفه عنهم؟")):
            for p in people:
                self._changed(self.lib.delete_person(p.id))

    def not_this_person(self) -> None:
        if not self.p_selected:
            messagebox.showinfo("Face Sorter", tr("Click the wrong faces first.", "دوس على الوشوش الغلط الأول."))
            return
        self._changed(self.lib.remove_prints(sorted(self.p_selected)))

    def move_prints(self) -> None:
        name = self.move_var.get().strip()
        if not self.p_selected or not name:
            messagebox.showinfo("Face Sorter", tr("Click some faces and choose a name.", "اختار وشوش واسم."))
            return
        self._changed(self.lib.move_prints(sorted(self.p_selected), name))

    def reset_learning(self) -> None:
        pid = self.selected_pid()
        p = self.lib.person(pid) if pid else None
        known = p is not None and p.kind == "known"
        who = person_label(p.label) if known else tr("everyone", "كل الناس")
        if ask_yes(self.root, tr("Reset learning", "مسح التعلم التلقائي"), tr(
                f"Delete what was learned automatically for {who}? What you confirmed stays.",
                f"تمسح اللي اتعلم لوحده لـ {who}؟ اللي إنت أكدته هيفضل.")):
            n = self.lib.reset_auto(p.id if known else None)
            self._changed(tr(f"Reset learning for {who}: {n} faces removed.",
                             f"اتمسح التعلم التلقائي لـ {who}: {n} وش."))

    # ============================================================ who is this? tab
    def _build_who(self) -> None:
        f = self.tab_who
        drop = tk.Canvas(f, height=80, background="#eef4ff", highlightthickness=2, highlightbackground="#89a")
        drop.pack(fill="x")
        hint = tr("🖼  Drop photos here, or click to choose", "🖼  اسحب الصور وافلتها هنا، أو دوس عشان تختار") \
            if TkinterDnD else tr("🖼  Click to choose photos", "🖼  دوس عشان تختار الصور")
        text_id = drop.create_text(0, 40, text=hint, font=self.f_title, fill="#345")
        drop.bind("<Configure>", lambda e: drop.coords(text_id, e.width // 2, e.height // 2))
        drop.bind("<Button-1>", lambda e: self.who_choose())
        if TkinterDnD:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", lambda e: self.who_add(dropped_paths(self.root, e.data)))
        bar = Flow(f, padx=3)
        bar.pack(fill="x", pady=4)
        bar.add(ttk.Button(bar, text=tr("Choose photos…", "اختار صور…"), command=self.who_choose))
        bar.add(ttk.Button(bar, text=tr("Clear the list", "امسح القائمة"), command=self.who_clear))
        bar.add(ttk.Label(bar, foreground="#666", text=tr("Nothing changes in the library unless you press Save.",
                                                          "المكتبة مش بتتغير غير لما تدوس احفظ.")), end=True)
        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True)
        self.who_list = tk.Listbox(body, width=16, justify=justify())
        self.who_list.bind("<<ListboxSelect>>", lambda e: self.who_show())
        self.who_canvas = tk.Canvas(body, background="#222", highlightthickness=0, width=240)
        self.who_canvas.bind("<Configure>", lambda e: self.who_draw())
        right = ttk.Frame(body)
        panes = [(self.who_list, 1), (self.who_canvas, 3), (right, 2)]
        for pane, weight in (reversed(panes) if rtl() else panes):
            body.add(pane, weight=weight)
        holder = tk.Canvas(right, highlightthickness=0, width=280)
        sb = ttk.Scrollbar(right, orient="vertical", command=holder.yview)
        self.who_faces = ttk.Frame(holder)
        self.who_faces.bind("<Configure>", lambda e: holder.configure(scrollregion=holder.bbox("all")))
        win_id = holder.create_window((0, 0), window=self.who_faces, anchor="nw")
        holder.bind("<Configure>", lambda e: holder.itemconfigure(win_id, width=e.width))
        holder.configure(yscrollcommand=sb.set)
        sb.pack(side=end(), fill="y")
        holder.pack(fill="both", expand=True)
        self.who_paths: list[Path] = []
        self.who_result = None   # (path, img, results)
        self.who_images: list = []

    def who_choose(self) -> None:
        files = filedialog.askopenfilenames(title=tr("Who is this?", "مين ده؟"),
                                            filetypes=[(tr("Photos", "صور"),
                                                        "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.heic *.heif"),
                                                       (tr("All files", "كل الملفات"), "*.*")])
        if files:
            self.who_add([Path(f) for f in files])

    def who_add(self, paths: list[Path]) -> None:
        from .engine import IMAGE_EXTS, find_photos
        new = []
        for p in paths:
            new += find_photos(p)[:200] if p.is_dir() else ([p] if p.suffix.lower() in IMAGE_EXTS else [])
        for p in new:
            self.who_paths.append(p)
            self.who_list.insert("end", p.name)
        if new:
            self.nb.select(self.tab_who)
            self.who_list.selection_clear(0, "end")
            self.who_list.selection_set(len(self.who_paths) - len(new))
            self.who_show()

    def who_clear(self) -> None:
        self.who_paths.clear()
        self.who_list.delete(0, "end")
        self.who_result = None
        self.who_canvas.delete("all")
        for w in self.who_faces.winfo_children():
            w.destroy()

    def who_show(self) -> None:
        sel = self.who_list.curselection()
        if not sel:
            return
        path = self.who_paths[sel[0]]
        try:
            if self.engine is None:
                self.engine = FaceEngine(self.log)
            img, results = identify(self.lib, self.engine, path)
        except RuntimeError as e:
            messagebox.showerror("Face Sorter", str(e))
            return
        self.who_result = (path, img, results)
        self.who_draw()
        self.who_cards()

    def who_draw(self) -> None:
        c = self.who_canvas
        c.delete("all")
        if not self.who_result:
            return
        _, img, results = self.who_result
        w, h = max(50, c.winfo_width()), max(50, c.winfo_height())
        ih, iw = img.shape[:2]
        scale = min(w / iw, h / ih)
        shown = cv2.resize(img, (max(1, int(iw * scale)), max(1, int(ih * scale))), interpolation=cv2.INTER_AREA)
        self._who_photo = tk.PhotoImage(data=_png(shown))
        ox, oy = (w - shown.shape[1]) // 2, (h - shown.shape[0]) // 2
        c.create_image(ox, oy, image=self._who_photo, anchor="nw")
        for k, r in enumerate(results):
            x, y, bw, bh = r["box"]
            x0, y0, x1, y1 = ox + x * scale, oy + y * scale, ox + (x + bw) * scale, oy + (y + bh) * scale
            color = VERDICT_COLORS[r["verdict"]]
            c.create_rectangle(x0, y0, x1, y1, outline=color, width=3)
            label = f"{k + 1}. " + (r["matches"][0][0] if r["matches"] and r["verdict"] != "unknown" else "?")
            t = c.create_text(x0 + 4, y0 - 10, text=label, anchor="w", fill="black", font=self.f_bold)
            bx = c.bbox(t)
            c.tag_lower(c.create_rectangle(bx[0] - 4, bx[1] - 2, bx[2] + 4, bx[3] + 2, fill=color, outline=color), t)

    def who_cards(self) -> None:
        for w in self.who_faces.winfo_children():
            w.destroy()
        self.who_images = []
        _, _, results = self.who_result
        if not results:
            ttk.Label(self.who_faces, text=tr("No faces found.", "مفيش وشوش."), padding=10).pack(anchor=anchor())
            return
        names = self.lib.names()
        for k, r in enumerate(results):
            card = ttk.Frame(self.who_faces, padding=6, relief="groove")
            card.pack(fill="x", pady=3, padx=2)
            img = photo_from_array(r["crop"], 72)
            self.who_images.append(img)
            ttk.Label(card, image=img).pack(side=start(), anchor="n")
            inner = ttk.Frame(card)
            inner.pack(side=start(), fill="x", expand=True, padx=6)
            mark = {"sure": "✅", "maybe": "❓", "unknown": "❔"}[r["verdict"]]
            if r["matches"] and r["verdict"] != "unknown":
                text = f"{k + 1}. {mark} {r['matches'][0][0]}  ({r['matches'][0][1]:.2f})"
                if len(r["matches"]) > 1:
                    text += "\n" + tr("or: ", "أو: ") + ", ".join(f"{n} ({sc:.2f})" for n, sc in r["matches"][1:3])
            else:
                text = f"{k + 1}. {mark} " + tr("Unknown", "مش معروف")
                if r["seen_before"]:
                    text += "\n" + tr("Seen before: ", "اتشاف قبل كده: ") + str(r["seen_before"])
            if not r["good"]:
                text += "\n" + tr("(low quality)", "(مش واضح)")
            WrapLabel(inner, text=text).pack(fill="x")
            row = Flow(inner, padx=2)
            row.pack(fill="x", pady=2)
            var = tk.StringVar(value=r["matches"][0][0] if r["matches"] and r["verdict"] != "unknown" else "")
            status = WrapLabel(inner, foreground="#2a2")

            def save(r=r, var=var, status=status):
                msg = self.lib.save_face(var.get(), r["face"])
                if msg:
                    status.config(text="✓ " + msg)
                    self._append(msg)
                    self.refresh_card()
            row.add(ttk.Combobox(row, textvariable=var, values=names, width=12, justify=justify()))
            row.add(ttk.Button(row, text=tr("Save", "احفظ"), command=save))
            status.pack(fill="x")

    # ------------------------------------------------------------- small windows
    def small_window(self, title: str, width: int, height: int) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.title(title)
        fit_window(win, width, height)
        return win

    def open_duplicates(self) -> None:
        pairs = self.lib.duplicates()
        win = self.small_window(tr("Possible duplicates", "يمكن نفس الشخص"), 900, 560)
        if not pairs:
            ttk.Label(win, text=tr("No possible duplicates.", "مفيش تكرار."), padding=20).pack()
            return
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        win.images = []
        for a, b, score in pairs[:20]:
            pa, pb = self.lib.person(a), self.lib.person(b)
            row = Flow(frame, padx=3)
            row.pack(fill="x", pady=2)
            for p in (pa, pb):
                img = photo_from_array(self.lib.thumb(p.id), 64)
                win.images.append(img)
                row.add(ttk.Label(row, image=img))
                row.add(ttk.Label(row, text=f"{person_label(p.label)} ({p.prints})"))
            row.add(ttk.Label(row, text=f"{score:.2f}", foreground="#666"))

            def merge(src, dst, r=row):
                self._changed(self.lib.merge(src, dst))
                r.destroy()

            def differ(x=a, y=b, r=row):
                self.lib.not_duplicate(x, y)
                r.destroy()
            row.add(ttk.Button(row, text=tr("Different", "مختلفين"), command=differ), end=True)
            row.add(ttk.Button(row, text=tr("Join into ", "ادمج في ") + person_label(pb.label),
                               command=lambda s=a, d=b: merge(s, d)), end=True)
            row.add(ttk.Button(row, text=tr("Join into ", "ادمج في ") + person_label(pa.label),
                               command=lambda s=b, d=a: merge(s, d)), end=True)

    def open_sessions(self) -> None:
        win = self.small_window(tr("Sessions", "الجلسات"), 900, 560)
        b = Flow(win, padx=4)
        b.pack(side="bottom", fill="x", padx=10, pady=8)
        lst = tk.Listbox(win, height=10, justify=justify())
        lst.pack(fill="x", padx=10, pady=8)
        txt = tk.Text(win, wrap="word", height=12)
        txt.tag_configure("line", justify=justify())
        txt.pack(fill="both", expand=True, padx=10)
        sessions = self.lib.sessions()
        for sid, started, device, folder, mode, status, report in sessions:
            lst.insert("end", f"#{sid}  {started or ''}  {device or ''}  {status}  {folder or ''}")

        def show(_=None):
            sel = lst.curselection()
            txt.delete("1.0", "end")
            if sel:
                txt.insert("end", sessions[sel[0]][6] or tr("(no report)", "(مفيش تقرير)"), "line")
        lst.bind("<<ListboxSelect>>", show)

        def undo():
            last = self.lib.last_saved_session()
            if not last:
                messagebox.showinfo(tr("Undo", "تراجع"), tr("Nothing to undo.", "مفيش حاجة تتلغي."), parent=win)
                return
            if ask_yes(win, tr("Undo", "تراجع"), tr(
                    f"Undo session #{last[0]}? Everything it added to the library is removed "
                    f"(also changes made after it).",
                    f"تلغي الجلسة رقم {last[0]}؟ كل اللي ضافته للمكتبة هيتشال (وكمان التغييرات اللي بعدها).")):
                self._changed(self.lib.undo_last_session())
                win.destroy()
        b.add(ttk.Button(b, text=tr("Undo last session", "الغي آخر جلسة"), command=undo))
        b.add(ttk.Button(b, text=tr("Close", "قفل"), command=win.destroy), end=True)

    def open_backups(self) -> None:
        backups = self.lib.backups()
        win = self.small_window(tr("Backups", "النسخ الاحتياطية"), 680, 420)
        b = Flow(win, padx=4)
        b.pack(side="bottom", fill="x", padx=10, pady=8)
        lst = tk.Listbox(win, justify=justify())
        lst.pack(fill="both", expand=True, padx=10, pady=8)
        for bk in backups:
            lst.insert("end", bk.name)

        def restore():
            sel = lst.curselection()
            if sel and ask_yes(win, tr("Restore", "رجوع"), tr(f"Put the library back to\n{backups[sel[0]].name}?",
                                                              f"ترجع المكتبة للنسخة دي؟\n{backups[sel[0]].name}")):
                self.lib.backup("before_restore")
                self.lib.restore(backups[sel[0]])
                self._changed(tr(f"Library restored from {backups[sel[0]].name}",
                                 f"المكتبة رجعت من {backups[sel[0]].name}"))
                win.destroy()
        b.add(ttk.Button(b, text=tr("Restore", "رجّع"), command=restore))
        b.add(ttk.Button(b, text=tr("Open folder", "افتح الفولدر"), command=lambda: open_path(self.lib.folder)))
        b.add(ttk.Button(b, text=tr("Close", "قفل"), command=win.destroy), end=True)

    def open_log(self) -> None:
        win = self.small_window(tr("Log", "السجل"), 900, 560)
        kind = tk.StringVar(value="history")
        top = Flow(win, padx=6)
        top.pack(fill="x", padx=8, pady=8)
        txt = tk.Text(win, wrap="word")
        txt.tag_configure("line", justify=justify())
        txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        def show():
            kinds = {"history": ("history",), "memory": ("memory",), "all": ("history", "memory", "run")}[kind.get()]
            txt.config(state="normal")
            txt.delete("1.0", "end")
            for at, k, t in self.lib.log_entries(kinds):
                txt.insert("end", f"{at}  [{k}]  {t}\n", "line")
            txt.config(state="disabled")
        for value, label in (("history", tr("History", "التاريخ")), ("memory", tr("Memory", "الذاكرة")),
                             ("all", tr("Everything", "كل حاجة"))):
            top.add(ttk.Radiobutton(top, text=label, value=value, variable=kind, command=show))

        def save():
            f = filedialog.asksaveasfilename(parent=win, defaultextension=".txt", initialfile="face_sorter_log.txt")
            if f:
                self.lib.export_log(Path(f))
        top.add(ttk.Button(top, text=tr("Save as text…", "احفظ كملف نص…"), command=save), end=True)
        show()

    # ============================================================ library card
    def _build_card(self) -> None:
        card = ttk.Frame(self.ui, padding=(12, 8), style="Card.TFrame")
        card.pack(fill="x", padx=8, pady=(8, 0))
        head = ttk.Frame(card)
        head.pack(fill="x")
        other = "en" if language() == "ar" else "ar"
        self.lang_btn = ttk.Button(head, text="🌐 " + LANGUAGES[other], command=self.change_language)
        self.lang_btn.pack(side=end(), anchor="n")
        ttk.Label(head, text="📚", font=self.f_title).pack(side=start(), anchor="n")
        self.card_name = WrapLabel(head, font=self.f_title)
        self.card_name.pack(side=start(), fill="x", expand=True, padx=6)
        self.card_path = ttk.Label(card, foreground="#345", anchor=anchor())
        self.card_path.pack(fill="x", pady=(2, 0))
        self.card_path.bind("<Configure>", lambda e: self.show_card_path())
        Tip(self.card_path, lambda: str(self.lib.path) if self.lib else "")
        self.card_info = WrapLabel(card, foreground="#555")
        self.card_info.pack(fill="x", pady=(2, 0))
        self.card_size_text = ""
        Tip(self.card_info, lambda: self.card_size_text)
        row = Flow(card, padx=3)
        row.pack(fill="x", pady=(4, 0))
        self.card_buttons = []
        for text, cmd, at_end in ((tr("Open folder", "افتح الفولدر"), lambda: open_path(self.lib.folder), False),
                                  (tr("Save a copy…", "احفظ نسخة…"), self.card_copy, False),
                                  (tr("Move…", "انقل…"), self.card_move, False),
                                  (tr("Open another…", "افتح مكتبة تانية…"), self.card_open_other, False),
                                  (tr("Default library", "المكتبة الافتراضية"), self.card_default, False),
                                  (tr("🔒 Password…", "🔒 الباسورد…"), self.card_password, True),
                                  (tr("Clean…", "نضّف…"), self.card_clean, True)):
            b = ttk.Button(row, text=text, command=cmd)
            row.add(b, end=at_end)
            self.card_buttons.append(b)
        if TkinterDnD:  # drop a library file on the card to open or merge it
            for w in (card, self.card_name, self.card_path, self.card_info):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: self.drop_library(e.data))

    def show_card_path(self) -> None:
        if not self.lib:
            return
        width = self.card_path.winfo_width()
        font = tkfont.nametofont("TkDefaultFont")
        prefix = tr("Folder: ", "الفولدر: ")
        self.card_path.config(text=prefix + shorten_middle(str(self.lib.folder), font,
                                                           width - font.measure(prefix) - 8))

    def refresh_card(self) -> None:
        lib = self.lib
        st, size = lib.stats(), lib.size_info()
        lock = "  🔒" if lib.encrypted else ""
        default = tr("  (default)", "  (الافتراضية)") if lib.folder == LIBRARY_DIR.resolve() else ""
        self.card_name.config(text=f"{lib.path.stem}{lock}{default}")
        self.show_card_path()
        self.card_info.config(text=tr(
            f"Created {lib.created}  ·  Updated {lib.updated}  ·  {st['known']} people  ·  {st['unknown']} unknown"
            f"  ·  {size['file']} MB",
            f"اتعملت {lib.created}  ·  آخر تحديث {lib.updated}  ·  {st['known']} شخص  ·  {st['unknown']} مجهول"
            f"  ·  {size['file']} ميجا"))
        self.card_size_text = tr(
            f"Size: people {size['people']} MB, unknown {size['unknown']} MB, scan memory {size['cache']} MB, "
            f"backups {size['backups']} MB",
            f"الحجم: الأشخاص {size['people']} ميجا، المجهولين {size['unknown']} ميجا، ذاكرة الفحص {size['cache']} ميجا، "
            f"النسخ الاحتياطية {size['backups']} ميجا")

    def ask_password(self, where: Path, wrong: bool = False) -> str | None:
        msg = tr(f"{'Wrong password. ' if wrong else ''}This library has a password:\n{where}\n\n"
                 "Type the password or the recovery key.",
                 f"{'الباسورد غلط. ' if wrong else ''}المكتبة دي عليها باسورد:\n{where}\n\n"
                 "اكتب الباسورد أو مفتاح الاسترجاع.")
        return simpledialog.askstring(tr("Password", "الباسورد"), msg, show="•", parent=self.root)

    def choose_file(self, files: list[Path]) -> Path | None:
        win = tk.Toplevel(self.root)
        win.title(tr("Which library?", "أنهي مكتبة؟"))
        fit_window(win, 760, 340)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, padding=10, justify=justify(), text=tr(
            "This folder has more than one library file. Choose one:",
            "الفولدر فيه أكتر من ملف مكتبة، اختار واحد:")).pack(anchor=anchor())
        ok_btn = ttk.Button(win, text=tr("Open", "افتح"))
        ok_btn.pack(side="bottom", pady=8)
        lst = tk.Listbox(win, justify=justify())
        lst.pack(fill="both", expand=True, padx=10)
        for f in files:
            lst.insert("end", f.name)
        chosen: list[Path] = []

        def ok():
            sel = lst.curselection()
            if sel:
                chosen.append(files[sel[0]])
            win.destroy()
        ok_btn.config(command=ok)
        self.root.wait_window(win)
        return chosen[0] if chosen else None

    def open_library(self, folder: Path | None = None, file: Path | None = None,
                     create: bool = True) -> Library | None:
        """Open a library, asking for the password or which file when needed. None if cancelled."""
        password = None
        wrong = False
        while True:
            try:
                return Library(folder or (file.parent if file else LIBRARY_DIR), password, file=file, create=create)
            except MultipleLibrariesError as e:
                file = self.choose_file(e.files)
                if file is None:
                    return None
            except LockedError:
                password = self.ask_password(file or folder, wrong)
                if password is None:
                    return None
                wrong = True
            except FileNotFoundError:
                messagebox.showerror("Face Sorter", tr(f"No library in\n{folder}", f"مفيش مكتبة في\n{folder}"))
                return None

    def startup_library(self) -> Library | None:
        place = setting_path(self.settings.get("library"))
        if place and not place.exists():
            answer = choose(self.root, tr("Library not found", "المكتبة مش موجودة"), tr(
                f"The library is not at:\n{place}\n(Maybe the USB stick is not connected?)",
                f"المكتبة مش موجودة في:\n{place}\n(يمكن الفلاشة مش متوصلة؟)"),
                [tr("Use the default library", "استخدم المكتبة الافتراضية"), tr("Choose another…", "اختار مكتبة…"),
                 tr("Close", "اقفل")])
            if answer in (None, 2):
                return None
            if answer == 0:
                place = None
            else:
                d = filedialog.askdirectory(title=tr("Library folder", "فولدر المكتبة"))
                if not d:
                    return None
                place = Path(d)
        lib = self.open_library(place or LIBRARY_DIR)
        if lib is None and place:  # cancelled: fall back to the default one
            lib = self.open_library(LIBRARY_DIR)
        return lib

    def switch_to(self, lib: Library | None) -> None:
        if lib is None:
            return
        if self.lib is not None and lib.path != self.lib.path:
            self.lib.close()
        self.lib = lib
        self.settings["library"] = None if lib.folder == LIBRARY_DIR.resolve() else path_setting(lib.folder)
        self.remember()
        self.refresh_all()
        self.refresh_library()
        self._append(tr("Library: ", "المكتبة: ") + str(lib.path))

    def card_copy(self) -> None:
        d = filedialog.askdirectory(title=tr("Where to save the copy", "تحفظ النسخة فين"))
        if d:
            folder = self.lib.copy_to(Path(d))
            messagebox.showinfo(tr("Saved", "اتحفظت"), tr(f"Copy saved in:\n{folder}\n\nThis library stays where it is.",
                                                          f"النسخة اتحفظت في:\n{folder}\n\nالمكتبة الأصلية زي ما هي."))

    def card_move(self) -> None:
        d = filedialog.askdirectory(title=tr("Move the library to", "تنقل المكتبة فين"))
        if not d:
            return
        dest = Path(d) / "Library"
        if ask_yes(self.root, tr("Move", "نقل"), tr(
                f"Move the library (with its backups) to\n{dest}\nIt will be used from there from now on.",
                f"تنقل المكتبة (ومعاها النسخ الاحتياطية) لـ\n{dest}\nوهتتفتح من هناك بعد كده؟")):
            try:
                self.lib.move_to(dest)
            except RuntimeError as e:
                messagebox.showerror("Face Sorter", str(e))
                return
            self.switch_to(self.lib)

    def card_open_other(self) -> None:
        d = filedialog.askdirectory(title=tr("Library folder to open", "فولدر المكتبة اللي هتفتحها"))
        if d:
            self.switch_to(self.open_library(Path(d), create=False))

    def card_default(self) -> None:
        self.switch_to(self.open_library(LIBRARY_DIR))

    def card_password(self) -> None:
        lib = self.lib
        if lib.encrypted:
            answer = choose(self.root, tr("Password", "الباسورد"), tr(
                "This library has a password. What do you want to do?", "المكتبة دي عليها باسورد. عايز تعمل إيه؟"),
                [tr("Change the password", "غيّر الباسورد"),
                 tr("Remove the password", "شيل الباسورد"), tr("Cancel", "إلغاء")])
            if answer in (None, 2):
                return
            if answer == 1:
                if ask_yes(self.root, tr("Remove password", "شيل الباسورد"), tr(
                        "Really remove the password? The library will no longer be encrypted.",
                        "متأكد إنك عايز تشيل الباسورد؟ المكتبة مش هتبقى متشفرة.")):
                    lib.remove_password()
                    self.refresh_card()
                return
        new = simpledialog.askstring(tr("Password", "الباسورد"), tr("New password:", "الباسورد الجديد:"), show="•",
                                     parent=self.root)
        if not new:
            return
        again = simpledialog.askstring(tr("Password", "الباسورد"), tr("Type it again:", "اكتبه تاني:"), show="•",
                                       parent=self.root)
        if new != again:
            messagebox.showerror("Face Sorter", tr("The two passwords are different.", "الباسوردين مختلفين."))
            return
        recovery = lib.set_password(new)
        self.refresh_card()
        if recovery:
            self.show_recovery_key(recovery)
        else:
            messagebox.showinfo(tr("Password", "الباسورد"), tr("Password changed. The recovery key stays the same.",
                                                               "الباسورد اتغير، ومفتاح الاسترجاع زي ما هو."))

    def show_recovery_key(self, key: str) -> None:
        win = tk.Toplevel(self.root)
        win.title(tr("Recovery key", "مفتاح الاسترجاع"))
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, padding=12, wraplength=540, justify=justify(), text=tr(
            "The library is now encrypted. Keep this recovery key somewhere safe (on paper, NOT on the same USB "
            "stick). If you forget the password, it is the ONLY way to open the library.",
            "المكتبة اتشفرت. احفظ مفتاح الاسترجاع ده في مكان آمن (على ورقة، مش على نفس الفلاشة). لو نسيت "
            "الباسورد، ده الطريقة الوحيدة لفتح المكتبة.")).pack(anchor=anchor())
        e = ttk.Entry(win, font=("Courier", 16, "bold"), justify="center", width=32)
        e.insert(0, key)
        e.config(state="readonly")
        e.pack(padx=12, pady=8)

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(key)

        def save():
            f = filedialog.asksaveasfilename(parent=win, defaultextension=".txt",
                                             initialfile="FaceSorter_recovery_key.txt")
            if f:
                Path(f).write_text(f"Face Sorter recovery key for {self.lib.path.name}:\n{key}\n", encoding="utf-8")
        b = ttk.Frame(win, padding=8)
        b.pack()
        ttk.Button(b, text=tr("Copy", "انسخ"), command=copy).pack(side=start(), padx=4)
        ttk.Button(b, text=tr("Save as text…", "احفظه كملف…"), command=save).pack(side=start(), padx=4)
        ttk.Button(b, text=tr("I saved it", "حفظته"), command=win.destroy).pack(side=start(), padx=4)
        self.root.wait_window(win)

    def card_clean(self) -> None:
        size = self.lib.size_info()
        if ask_yes(self.root, tr("Clean", "تنضيف"), tr(
                f"Forget the scan results of {size['cache_photos']} photos ({size['cache']} MB)?\n"
                f"No person or face is deleted; photos met again are simply checked again.",
                f"تمسح نتايج فحص {size['cache_photos']} صورة ({size['cache']} ميجا)؟\n"
                f"مفيش أي شخص أو وش هيتمسح، والصور لو اتقابلت تاني هتتفحص من جديد.")):
            self._changed(self.lib.clean())

    def drop_library(self, data: str) -> None:
        for p in dropped_paths(self.root, data):
            if p.is_dir() or p.suffix.lower() == ".db":
                answer = choose(self.root, tr("Library", "مكتبة"), str(p),
                                [tr("Open this library", "افتح المكتبة دي"),
                                 tr("Merge it into the current library", "ادمجها في المكتبة الحالية"),
                                 tr("Cancel", "إلغاء")])
                if answer == 0:
                    self.switch_to(self.open_library(p if p.is_dir() else p.parent, file=None if p.is_dir() else p,
                                                     create=False))
                elif answer == 1:
                    self.merge_library(p)
                return

    def merge_library(self, p: Path) -> None:
        password = None
        while True:
            try:
                self._changed(self.lib.merge_from(p, password))
                return
            except LockedError:
                password = self.ask_password(p, password is not None)
                if password is None:
                    return
            except (RuntimeError, FileNotFoundError) as e:
                messagebox.showerror("Face Sorter", str(e))
                return

    def drop_folder(self, data: str) -> None:
        for p in dropped_paths(self.root, data):
            self.src_var.set(str(p if p.is_dir() else p.parent))
            return

    def show_about(self) -> None:
        messagebox.showinfo("Face Sorter", tr(
            f"Face Sorter v{__version__}\nFree and open source (MIT license).\n\n"
            "Everything runs on this computer. No photo or data is uploaded.\n"
            "The library holds face data and names: only use photos you have the right to use, and set a "
            "password if the library is on a USB stick.\n"
            "Models: YuNet (MIT) and SFace (Apache-2.0) from OpenCV Zoo. Other licenses: THIRD_PARTY_LICENSES.md",
            f"Face Sorter v{__version__}\nبرنامج مجاني ومفتوح المصدر (رخصة MIT).\n\n"
            "كل الشغل بيحصل على الجهاز ده، ومفيش ولا صورة ولا بيانات بتترفع.\n"
            "المكتبة فيها بيانات وشوش وأسامي: استخدم البرنامج على صور ليك حق فيها، وحط باسورد لو المكتبة "
            "على فلاشة.\n"
            "النماذج: YuNet (MIT) و SFace (Apache-2.0) من OpenCV Zoo. باقي الرخص في THIRD_PARTY_LICENSES.md"))

    def on_error(self, exc, val, tb) -> None:
        text = "".join(traceback.format_exception(exc, val, tb))
        log_error("window", text)
        messagebox.showerror("Face Sorter", tr(f"Something went wrong:\n{val}\n\nDetails were saved in\n{ERRORS}",
                                               f"حصلت مشكلة:\n{val}\n\nالتفاصيل اتحفظت في\n{ERRORS}"))

    # ================================================================ plumbing
    def log(self, text: str) -> None:
        self.msgs.put(("log", text))

    def on_progress(self, stage: int, done: int, total: int) -> None:
        self.msgs.put(("progress", (stage, done, total)))

    def _append(self, text: str) -> None:
        self.logbox.config(state="normal")
        self.logbox.insert("end", text + "\n", "line")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def remember(self) -> None:
        try:
            self.settings.update({
                "source": self.src_var.get(), "dest": self.dest_var.get(), "mode": self.mode_var.get(),
                "strictness": self.strict_var.get(), "min_photos": int(self.minp_var.get()),
                "others": self.others_var.get(), "speed": self.speed_var.get(),
                "battery_saver": self.battery_var.get(), "skip_screenshots": self.f_shots.get(),
                "skip_stickers": self.f_stickers.get(), "skip_small": self.f_small.get(),
                "exclude": self.f_exclude.get(), "language": language()})
            SETTINGS.write_text(json.dumps(self.settings, ensure_ascii=False, indent=1), encoding="utf-8")
        except (OSError, ValueError, tk.TclError):
            pass

    def refresh_all(self) -> None:
        self.refresh_card()
        self.lib_label.config(text=tr(f"Photos on this computer ({device_name()}):",
                                      f"الصور اللي على الجهاز ده ({device_name()}):"))
        pending = pending_session(self.lib)
        self.resume_btn.config(state="normal" if pending and not self.busy else "disabled")
        if self.nb.select() == str(self.tab_lib):
            self.refresh_library()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for b in [self.start_btn, self.who_btn, self.resume_btn, self.lang_btn] + self.lib_buttons + self.card_buttons:
            b.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        self.pause_btn.config(state="normal" if busy else "disabled", text=tr("⏸ Pause", "⏸ إيقاف مؤقت"))
        if busy:
            self.throttle.stop.clear()
            self.throttle.pause.clear()
            self.started = time.monotonic()
            self.stage = (0, 0, 0)
            self.stage_start = (0, self.started, 0)
        else:
            self.refresh_all()

    def run_job(self, job) -> None:
        def work():
            try:
                job()
                self.msgs.put(("done", None))
            except Exception as e:  # show any problem to the user
                log_error("background job", traceback.format_exc())
                self.msgs.put(("error", f"{type(e).__name__}: {e}\n\n{tr('Details:', 'التفاصيل:')} {ERRORS}"))
        self.set_busy(True)
        threading.Thread(target=work, daemon=True).start()

    def update_progress(self) -> None:
        stage, done, total = self.stage
        if not self.busy or stage == 0:
            return
        now = time.monotonic()
        pct = 100.0 * done / total if total else 100.0
        self.bar["value"] = pct
        count = f" · {done}/{total}" if total > 1 else ""
        self.progress_label.config(text=f"{pct:.0f}%{count} · " + tr(f"Stage {min(stage, 4)}/4: ",
                                                                      f"المرحلة {min(stage, 4)}/4: ")
                                   + stage_label(stage))
        elapsed = f"⏱ {fmt_time(now - self.started)}"
        if self.throttle.pause.is_set():
            self.time_label.config(text=f"{elapsed} · " + tr("⏸ Paused", "⏸ متوقف مؤقتاً"))
            return
        st0, t0, d0 = self.stage_start
        rate = (done - d0) / (now - t0) if st0 == stage and now - t0 > 2 and done > d0 else 0
        if rate > 0 and total > done:
            left = " · " + tr("left ~", "فاضل حوالي ") + fmt_time((total - done) / rate)
        elif total and done >= total:
            left = ""
        else:
            left = " · " + tr("calculating...", "بيحسب...")
        self.time_label.config(text=elapsed + tr(" elapsed", " عدّى") + left)
        on_batt = self.throttle.on_battery and self.throttle.battery_saver
        self.battery_label.config(text=tr("🔋 On battery: quiet", "🔋 على البطارية: هادي") if on_batt else "")

    def pump(self) -> None:
        while not self.msgs.empty():
            kind, val = self.msgs.get()
            if kind == "log":
                self._append(val)
            elif kind == "progress":
                if val[0] != self.stage[0]:
                    self.stage_start = (val[0], time.monotonic(), val[1])
                self.stage = val
            elif kind == "review":
                self.open_review(val)
            elif kind == "report":
                messagebox.showinfo(tr("Saved", "اتحفظ"), val)
            elif kind == "done":
                self.bar["value"] = 100
                self.progress_label.config(text="100% · " + tr("Done", "خلص"))
                self.time_label.config(text=f"⏱ {fmt_time(time.monotonic() - self.started)}")
                self.set_busy(False)
            else:
                self.set_busy(False)
                messagebox.showerror("Face Sorter", val)
        self.update_progress()
        self.root.after(150, self.pump)

    def run(self) -> None:
        self.root.mainloop()
        self.remember()
        self.lib.close()


def main() -> None:
    App().run()
