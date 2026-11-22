"""The look of the window: colours, fonts, and the building blocks every page is made of.

    theme(root)        colours and fonts for all widgets (one calm, light style)
    Card               a white box with a title, the main building block of every page
    Flow               a row of widgets that wraps onto the next line instead of hiding things
    WrapLabel          text that breaks into lines to fit its width
    ScrollFrame        a page that scrolls when it is taller than the window
    Grid               a scrollable grid of pictures (faces or photos)
    Tip                a small note when the mouse rests on a widget
    choose()           a question with clear buttons instead of Yes / No
    layout_problems()  what does not fit (used by the build test)

Arabic is laid out from the right: start() is "right" in Arabic and "left" in English.
"""
from __future__ import annotations

import base64
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

import cv2
import numpy as np

from .engine import decode_jpg
from .i18n import rtl

# ---------------------------------------------------------------- colours
C = {
    "bg": "#F3F5F9",          # behind the cards
    "card": "#FFFFFF",
    "border": "#E1E6EE",
    "text": "#1E293B",
    "muted": "#64748B",
    "faint": "#94A3B8",
    "nav": "#14213D",         # side menu
    "nav_text": "#C7D2E4",
    "nav_hover": "#1F2F52",
    "nav_active": "#2A3C66",
    "accent": "#2563EB",      # the one colour for "do this"
    "accent_hover": "#1D4ED8",
    "accent_soft": "#DBEAFE",
    "ok": "#15803D",
    "ok_soft": "#DCFCE7",
    "warn": "#B45309",
    "warn_soft": "#FEF3C7",
    "danger": "#B91C1C",
    "grid": "#E9EDF4",        # behind pictures
    "drop": "#F0F6FF",
    "button": "#EAEFF6",      # ordinary buttons: light grey, the main one is blue
    "button_edge": "#D6DEEA",
}
VERDICT_COLORS = {"sure": "#16A34A", "maybe": "#F59E0B", "unknown": "#94A3B8"}
LEVEL_COLORS = {"weak": "#B91C1C", "medium": "#B45309", "strong": "#15803D"}


class Fonts:
    """Named fonts, made once the window exists."""
    body = small = bold = head = title = big = nav = icon = None

    @classmethod
    def make(cls, root: tk.Misc) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=10)
            except tk.TclError:
                pass
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        cls.body = tkfont.nametofont("TkDefaultFont")
        cls.small = tkfont.Font(root, family=family, size=9)
        cls.bold = tkfont.Font(root, family=family, size=10, weight="bold")
        cls.head = tkfont.Font(root, family=family, size=12, weight="bold")
        cls.title = tkfont.Font(root, family=family, size=16, weight="bold")
        cls.big = tkfont.Font(root, family=family, size=18, weight="bold")
        cls.nav = tkfont.Font(root, family=family, size=11)
        cls.icon = tkfont.Font(root, family=family, size=14)


# ---------------------------------------------------------------- direction
def start() -> str:
    """The side a row starts from: right in Arabic, left in English."""
    return "right" if rtl() else "left"


def end() -> str:
    return "left" if rtl() else "right"


def anchor() -> str:
    return "e" if rtl() else "w"


def justify() -> str:
    return "right" if rtl() else "left"


# ---------------------------------------------------------------- theme
def theme(root: tk.Misc) -> ttk.Style:
    Fonts.make(root)
    s = ttk.Style(root)
    s.theme_use("clam")
    c = C
    s.configure(".", background=c["card"], foreground=c["text"], font=Fonts.body, bordercolor=c["border"],
                lightcolor=c["card"], darkcolor=c["card"], troughcolor=c["grid"], focuscolor=c["accent"],
                selectbackground=c["accent_soft"], selectforeground=c["text"], insertcolor=c["text"])
    s.configure("TFrame", background=c["card"])
    s.configure("Page.TFrame", background=c["bg"])
    s.configure("TLabel", background=c["card"], foreground=c["text"])
    s.configure("Muted.TLabel", foreground=c["muted"])
    s.configure("Bold.TLabel", font=Fonts.bold)
    s.configure("CardTitle.TLabel", font=Fonts.head)
    s.configure("Page.TLabel", background=c["bg"])
    s.configure("PageTitle.TLabel", background=c["bg"], font=Fonts.title)
    s.configure("PageSub.TLabel", background=c["bg"], foreground=c["muted"])
    s.configure("Ok.TLabel", foreground=c["ok"])
    s.configure("Warn.TLabel", foreground=c["warn"])
    # buttons: white with a thin border; the one main action of a page is blue
    s.configure("TButton", padding=(12, 6), background=c["button"], foreground=c["text"], bordercolor=c["button_edge"],
                lightcolor=c["button"], darkcolor=c["button"], relief="flat", focusthickness=0)
    s.map("TButton", background=[("disabled", "#F1F4F8"), ("pressed", "#D5DEEA"), ("active", "#DCE6F5")],
          foreground=[("disabled", c["faint"])],
          bordercolor=[("disabled", "#E6EBF2"), ("active", "#B9C9E2"), ("focus", c["accent"])],
          lightcolor=[("disabled", "#F1F4F8"), ("active", "#DCE6F5")],
          darkcolor=[("disabled", "#F1F4F8"), ("active", "#DCE6F5")])
    s.configure("Primary.TButton", background=c["accent"], foreground="#FFFFFF", bordercolor=c["accent"],
                lightcolor=c["accent"], darkcolor=c["accent"], font=Fonts.bold, padding=(16, 7))
    s.map("Primary.TButton", background=[("disabled", "#A9C2F5"), ("pressed", "#1E40AF"), ("active", c["accent_hover"])],
          foreground=[("disabled", "#F8FAFC")], bordercolor=[("disabled", "#A9C2F5"), ("active", c["accent_hover"])],
          lightcolor=[("active", c["accent_hover"])], darkcolor=[("active", c["accent_hover"])])
    s.configure("TMenubutton", padding=(12, 6), background=c["button"], foreground=c["text"],
                bordercolor=c["button_edge"], lightcolor=c["button"], darkcolor=c["button"], arrowcolor=c["muted"],
                relief="flat")
    s.map("TMenubutton", background=[("disabled", "#F1F4F8"), ("active", "#DCE6F5")],
          foreground=[("disabled", c["faint"])], lightcolor=[("active", "#DCE6F5")], darkcolor=[("active", "#DCE6F5")])
    s.configure("Danger.TButton", foreground=c["danger"])
    s.map("Danger.TButton", background=[("active", "#FDE2E2"), ("pressed", "#FBCACA")],
          lightcolor=[("active", "#FDE2E2")], darkcolor=[("active", "#FDE2E2")], bordercolor=[("active", "#F5B5B5")])
    s.configure("Link.TButton", padding=(4, 2), background=c["card"], foreground=c["accent"], bordercolor=c["card"],
                font=Fonts.bold)
    s.map("Link.TButton", background=[("active", c["card"])], bordercolor=[("active", c["card"]), ("focus", c["card"])],
          foreground=[("active", c["accent_hover"]), ("disabled", c["faint"])])
    s.configure("PageLink.TButton", padding=(4, 2), background=c["bg"], foreground=c["accent"], bordercolor=c["bg"],
                lightcolor=c["bg"], darkcolor=c["bg"], font=Fonts.bold)
    s.map("PageLink.TButton", background=[("active", c["bg"])], bordercolor=[("active", c["bg"]), ("focus", c["bg"])],
          foreground=[("active", c["accent_hover"])])
    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(w, background=c["card"], foreground=c["text"], indicatorbackground="#FFFFFF",
                    indicatorforeground=c["accent"], indicatormargin=(4, 2, 6, 2), padding=(2, 3))
        s.map(w, background=[("active", c["card"])], indicatorbackground=[("selected", c["accent"]), ("disabled", "#E5E7EB")],
              indicatorforeground=[("selected", "#FFFFFF")], foreground=[("disabled", c["faint"])])
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        s.configure(w, fieldbackground="#FFFFFF", bordercolor=c["border"], lightcolor=c["border"],
                    darkcolor=c["border"], padding=(6, 4), arrowcolor=c["muted"])
        s.map(w, bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])],
              fieldbackground=[("disabled", "#F4F6FA"), ("readonly", "#FFFFFF")], foreground=[("disabled", c["faint"])])
    s.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor="#E5EAF2", bordercolor="#E5EAF2",
                lightcolor=c["accent"], darkcolor=c["accent"], thickness=10)
    s.configure("Horizontal.TScale", background=c["accent"], troughcolor="#E5EAF2", bordercolor=c["border"],
                lightcolor=c["accent"], darkcolor=c["accent"])
    s.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground=c["text"],
                bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"], rowheight=30)
    s.map("Treeview", background=[("selected", c["accent_soft"])], foreground=[("selected", c["text"])])
    s.configure("Treeview.Heading", background="#F8FAFC", foreground=c["muted"], font=Fonts.bold, relief="flat",
                bordercolor=c["border"], lightcolor="#F8FAFC", darkcolor="#F8FAFC", padding=(6, 4))
    s.map("Treeview.Heading", background=[("active", "#EEF2F7")])
    s.configure("People.Treeview", rowheight=46)
    s.configure("Vertical.TScrollbar", background="#D5DCE6", troughcolor=c["card"], bordercolor=c["card"],
                lightcolor="#D5DCE6", darkcolor="#D5DCE6", arrowcolor=c["muted"], gripcount=0)
    s.map("Vertical.TScrollbar", background=[("active", "#B8C3D3")])
    s.configure("TPanedwindow", background=c["bg"])
    s.configure("Sash", sashthickness=8, gripcount=0, background=c["bg"], lightcolor=c["bg"], bordercolor=c["bg"])
    root.option_add("*TCombobox*Listbox.background", "#FFFFFF")
    root.option_add("*TCombobox*Listbox.selectBackground", c["accent_soft"])
    root.option_add("*TCombobox*Listbox.selectForeground", c["text"])
    return s


def style_tk(w: tk.Widget, **extra) -> tk.Widget:
    """Colours for the plain Tk widgets (lists, text boxes) so they match the rest."""
    opts = dict(background="#FFFFFF", foreground=C["text"], relief="flat", borderwidth=0, highlightthickness=1,
                highlightbackground=C["border"], highlightcolor=C["accent"], selectbackground=C["accent_soft"],
                selectforeground=C["text"])
    opts.update(extra)
    for k, v in opts.items():
        try:
            w.configure(**{k: v})
        except tk.TclError:
            pass
    return w


# ---------------------------------------------------------------- pictures
def _png(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode()


def photo_from_jpg(data: bytes | None, size: int) -> tk.PhotoImage:
    img = decode_jpg(data) if data else None
    return photo_from_array(img, size)


def photo_from_array(img: np.ndarray | None, size: int, square: bool = True) -> tk.PhotoImage:
    if img is None:
        img = np.full((size, size, 3), 200, np.uint8)
    if square:
        img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    else:  # keep the shape, fit inside size x size
        h, w = img.shape[:2]
        k = size / max(h, w)
        img = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
    return tk.PhotoImage(data=_png(img))


def app_icon(size: int = 64) -> tk.PhotoImage:
    """The program's icon (drawn here, so there is no extra file): a face in a blue rounded square."""
    s = size * 4  # draw big, then shrink: smooth edges
    img = np.zeros((s, s, 4), np.uint8)
    blue = (235, 99, 37, 255)   # BGR + alpha of C["accent"]
    r = s // 5
    cv2.rectangle(img, (r, 0), (s - r, s), blue, -1)
    cv2.rectangle(img, (0, r), (s, s - r), blue, -1)
    for cx, cy in ((r, r), (s - r, r), (r, s - r), (s - r, s - r)):
        cv2.circle(img, (cx, cy), r, blue, -1, lineType=cv2.LINE_AA)
    white = (255, 255, 255, 255)
    cv2.circle(img, (s // 2, s // 2), int(s * 0.30), white, int(s * 0.06), lineType=cv2.LINE_AA)
    for ex in (0.41, 0.59):
        cv2.circle(img, (int(s * ex), int(s * 0.44)), int(s * 0.035), white, -1, lineType=cv2.LINE_AA)
    cv2.ellipse(img, (s // 2, int(s * 0.54)), (int(s * 0.12), int(s * 0.08)), 0, 10, 170, white, int(s * 0.04),
                lineType=cv2.LINE_AA)
    img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode())


def round_thumb(img: np.ndarray | None, size: int) -> tk.PhotoImage:
    """A face in a circle (for the people list)."""
    if img is None:
        img = np.full((size, size, 3), 200, np.uint8)
    img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    mask = np.zeros((size, size), np.uint8)
    cv2.circle(mask, (size // 2, size // 2), size // 2 - 1, 255, -1, lineType=cv2.LINE_AA)
    bg = np.full_like(img, 255)
    a = (mask.astype(np.float32) / 255)[..., None]
    out = (img * a + bg * (1 - a)).astype(np.uint8)
    return tk.PhotoImage(data=_png(out))


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
        self.hidden: set = set()
        self._pending = False
        self._width = 0
        self.bind("<Configure>", self._on_configure)

    def add(self, widget: tk.Widget, end: bool = False) -> tk.Widget:
        self.items.append((widget, end))
        widget.bind("<Configure>", lambda e: self.relayout(), add="+")
        self.relayout()
        return widget

    def set_visible(self, widget: tk.Widget, visible: bool) -> None:
        if visible:
            self.hidden.discard(widget)
        else:
            self.hidden.add(widget)
            widget.place_forget()
        self.relayout()

    def clear(self) -> None:
        for w, _ in self.items:
            if w.winfo_exists():
                w.destroy()
        self.items.clear()
        self.relayout()

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
        shown = [(w, e) for w, e in self.items if w.winfo_exists() and w not in self.hidden]
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
        y = self.pady if shown else 0
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
        y = max(y, 1)
        if int(self.cget("height")) != y:
            self.configure(height=y)


class WrapLabel(ttk.Label):
    """A label that breaks its text into lines to fit the width it is given, instead of cutting it."""

    def __init__(self, parent, **kw):
        kw.setdefault("justify", justify())
        kw.setdefault("anchor", anchor())
        super().__init__(parent, width=1, **kw)
        self.bind("<Configure>", lambda e: self.configure(wraplength=max(40, e.width - 4)))


class Card(tk.Frame):
    """A white box with a thin border. `body` is where the content goes.

    Card(parent, title, subtitle)   the title row can also hold buttons: card.head_add(widget)
    """

    def __init__(self, parent, title: str = "", subtitle: str = "", padding=(16, 12), **kw):
        super().__init__(parent, background=C["card"], highlightthickness=1, highlightbackground=C["border"],
                         highlightcolor=C["border"], **kw)
        self.head = None
        if title or subtitle:
            self.head = Flow(self, padx=0, pady=0)
            self.head.pack(fill="x", padx=padding[0], pady=(padding[1], 0))
            if title:
                self.head.add(ttk.Label(self.head, text=title, style="CardTitle.TLabel"))
            if subtitle:
                WrapLabel(self, text=subtitle, style="Muted.TLabel").pack(fill="x", padx=padding[0], pady=(2, 0))
        self.body = ttk.Frame(self, padding=(padding[0], 8 if (title or subtitle) else padding[1],
                                             padding[0], padding[1]))
        self.body.pack(fill="both", expand=True)

    def head_add(self, widget: tk.Widget) -> tk.Widget:
        return self.head.add(widget, end=True)


class ScrollFrame(ttk.Frame):
    """A page that scrolls when its content is taller than the window. Put things in `.inner`."""

    def __init__(self, parent, style: str = "Page.TFrame"):
        super().__init__(parent, style=style)
        self.canvas = tk.Canvas(self, highlightthickness=0, background=C["bg"], borderwidth=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style=style)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._bar_set)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.pack(side=start(), fill="both", expand=True)
        for ev, d in (("<MouseWheel>", None), ("<Button-4>", -1), ("<Button-5>", 1)):
            self.canvas.bind_all(ev, lambda e, d=d: self._wheel(e, d), add="+")

    def _bar_set(self, lo, hi) -> None:
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.bar.pack_forget()
        elif not self.bar.winfo_ismapped():
            self.bar.pack(side=end(), fill="y", before=self.canvas)
        self.bar.set(lo, hi)

    def _wheel(self, e, d) -> None:
        if not self.winfo_ismapped():
            return
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self:
            if isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview)) or (isinstance(w, tk.Canvas) and w is not self.canvas):
                return  # the widget under the mouse scrolls itself
            w = w.master
        if w is self and self.bar.winfo_ismapped():
            self.canvas.yview_scroll(d or (-1 if e.delta > 0 else 1), "units")


class SearchBox(ttk.Frame):
    """A text box with its hint written inside it (shown while it is empty)."""

    def __init__(self, parent, var: tk.StringVar, hint: str, **kw):
        super().__init__(parent, **kw)
        self.var = var
        self.entry = ttk.Entry(self, textvariable=var, justify=justify())
        self.entry.pack(fill="x")
        self.hint = tk.Label(self.entry, text="🔎  " + hint, background="#FFFFFF", foreground=C["faint"],
                             font=Fonts.body, cursor="xterm")
        self.hint.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self.update_hint(), add="+")
        self.entry.bind("<FocusOut>", lambda e: self.update_hint(), add="+")
        var.trace_add("write", lambda *_: self.update_hint())
        self.update_hint()

    def update_hint(self) -> None:
        if not self.hint.winfo_exists():
            return
        if self.var.get() or self.focus_get() is self.entry:
            self.hint.place_forget()
        elif rtl():
            self.hint.place(relx=1.0, x=-8, rely=0.5, anchor="e")
        else:
            self.hint.place(x=8, rely=0.5, anchor="w")


def menu_button(parent, text: str, items: list[tuple[str, object]], style: str = "TMenubutton") -> ttk.Menubutton:
    """A button that opens a short menu (several actions in the space of one button)."""
    mb = ttk.Menubutton(parent, text=text, style=style, direction="below")
    m = tk.Menu(mb, tearoff=False, background="#FFFFFF", foreground=C["text"], activebackground=C["accent_soft"],
                activeforeground=C["text"], font=Fonts.body, borderwidth=1, relief="solid")
    for label, cmd in items:
        if label == "-":
            m.add_separator()
        else:
            m.add_command(label=label, command=cmd)
    mb["menu"] = m
    return mb


class Tip:
    """A small note when the mouse rests on a widget (e.g. the full library path)."""

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
        tk.Label(self.win, text=text, background="#1E293B", foreground="#F8FAFC", padx=8, pady=4,
                 justify=justify(), wraplength=700, font=Fonts.small).pack()

    def hide(self, _=None) -> None:
        if self.win:
            self.win.destroy()
            self.win = None


def chip(parent, text: str, color: str = "", soft: str = "") -> tk.Label:
    """A small rounded-looking label (a number, a state)."""
    return tk.Label(parent, text=text, background=soft or "#EEF2F7", foreground=color or C["muted"],
                    font=Fonts.small, padx=8, pady=2)


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


def choose(parent: tk.Misc, title: str, message: str, options: list[str]) -> int | None:
    """Ask a question with clear buttons (instead of Yes / No). Returns the button's index, None if closed.
    The first option is the main one (blue)."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(background=C["card"])
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    body = ttk.Frame(win, padding=20)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=title, style="CardTitle.TLabel").pack(anchor=anchor())
    ttk.Label(body, text=message, justify=justify(), wraplength=560).pack(anchor=anchor(), pady=(6, 16))
    row = ttk.Frame(body)
    row.pack(fill="x")
    answer: list[int] = []

    def pick(k: int) -> None:
        answer.append(k)
        win.destroy()
    for k, text in enumerate(options):
        ttk.Button(row, text=text, style="Primary.TButton" if k == 0 else "TButton",
                   command=lambda k=k: pick(k)).pack(side=start(), padx=(0, 8) if not rtl() else (8, 0))
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()
    parent.wait_window(win)
    return answer[0] if answer else None


def fit_window(win: tk.Misc, width: int, height: int) -> None:
    """Open a window at a size that fits the screen (small laptops, big zoom)."""
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = min(width, sw - 40), min(height, sh - 90)
    win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")


def layout_problems(root: tk.Misc) -> list[str]:
    """Widgets whose text does not fit, or that stick out of their window (used by the build test)."""
    problems: list[str] = []
    text_kinds = ("TButton", "TLabel", "TCheckbutton", "TRadiobutton", "TMenubutton", "Button", "Label",
                  "Checkbutton", "Radiobutton")

    def walk(w: tk.Misc, scrolled: bool = False) -> None:
        if isinstance(w, tk.Toplevel) and w is not root:
            return  # other windows are checked on their own
        scrolled = scrolled or isinstance(w, tk.Canvas)  # scrolled content may be taller than the window
        for c in w.winfo_children():
            if not c.winfo_ismapped():
                if c.winfo_manager() in ("pack", "grid", "place") and not isinstance(c, tk.Toplevel) \
                        and w.winfo_ismapped() and not scrolled:
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
                if px < -1 or px + c.winfo_width() > p.winfo_width() + 1 or \
                        (not scrolled and (py < -1 or py + c.winfo_height() > p.winfo_height() + 1)):
                    problems.append(f"cut by its box: {text!r}")
            walk(c, scrolled)
    walk(root)
    return problems


class Grid(ttk.Frame):
    """A scrollable grid of small pictures with a state per picture and an optional keyboard cursor.

    items: dicts with  key, jpg (bytes) or img (array), on (True/False/None), dim (bool), caption (str)
    headers: {index: text} -> a full-width title row before that item (e.g. years)
    """

    def __init__(self, parent, size: int = 96, on_click=None, on_double=None, empty: str = ""):
        super().__init__(parent)
        self.size = size
        self.on_click = on_click
        self.on_double = on_double
        self.empty = empty
        self.canvas = tk.Canvas(self, highlightthickness=0, background=C["grid"], borderwidth=0)
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
                if it.get("img") is not None:
                    self.images[it["key"]] = photo_from_array(it["img"], self.size, square=False)
                else:
                    self.images[it["key"]] = photo_from_jpg(it.get("jpg"), self.size)
        self.canvas.yview_moveto(0)
        self.draw()

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        width = max(1, c.winfo_width())
        if not self.items:
            if self.empty:
                c.create_text(width // 2, 60, text=self.empty, fill=C["muted"], font=Fonts.body,
                              width=max(100, width - 40), justify="center")
            c.configure(scrollregion=(0, 0, width, 1))
            return
        cell = self.size + 14
        cols = max(1, (width - 10) // cell)
        y = 8
        col = 0
        self._pos = []
        for k, it in enumerate(self.items):
            if k in self.headers:
                if col:
                    y += cell
                    col = 0
                c.create_text(width - 12 if rtl() else 12, y + 12, text=self.headers[k], anchor=anchor(),
                              fill=C["text"], font=Fonts.bold)
                y += 30
            x = width - (col + 1) * cell + 4 if rtl() else col * cell + 10
            self._pos.append((x, y))
            on = it.get("on")
            img = self.images[it["key"]]
            iw, ih = img.width(), img.height()
            ox, oy = x + (self.size - iw) // 2, y + (self.size - ih) // 2
            if k == self.cursor:
                c.create_rectangle(x - 6, y - 6, x + self.size + 6, y + self.size + 6, outline="#F59E0B", width=3)
            color = {True: "#16A34A", False: "#DC2626", None: ""}[on]
            tags = [c.create_rectangle(x - 1, y - 1, x + self.size + 1, y + self.size + 1, fill="#FFFFFF",
                                       outline=color or C["border"], width=3 if color else 1)]
            tags.append(c.create_image(ox, oy, image=img, anchor="nw"))
            if it.get("dim"):
                tags.append(c.create_rectangle(ox, oy, ox + iw, oy + ih, fill="#000", stipple="gray50", outline=""))
            if on is False:
                tags.append(c.create_text(x + self.size - 12, y + 12, text="✗", fill="#DC2626", font=Fonts.head))
            if it.get("caption"):
                t = c.create_text(x + 4, y + self.size - 4, text=it["caption"], anchor="sw", fill="#FFFFFF",
                                  font=Fonts.small)
                bx = c.bbox(t)
                bg = c.create_rectangle(bx[0] - 3, bx[1] - 1, bx[2] + 3, bx[3] + 1, fill="#1E293B", outline="")
                c.tag_lower(bg, t)
                tags += [bg, t]
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
        return max(1, (max(1, self.canvas.winfo_width()) - 10) // (self.size + 14))
