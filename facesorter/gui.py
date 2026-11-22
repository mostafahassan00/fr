"""The window (Tkinter): "This computer" tab, the review window, and the "Library" tab."""
from __future__ import annotations

import base64
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np

from . import __version__
from .engine import FaceEngine, Filters, decode_jpg
from .library import _ROOT, LIBRARY_DIR, Library, LockedError, MultipleLibrariesError, device_name
from .perf import LABELS, LEVELS, Throttle, psutil
from .session import (MODE_LABELS, MODES, STAGES, build_review, discard_session, identify, pending_session,
                      save_review, scan)

try:  # drag and drop of files onto the window
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # pragma: no cover - the portable build has it
    TkinterDnD = None
    DND_FILES = None

SETTINGS = _ROOT / "settings.json"
ERRORS = _ROOT / "errors.txt"
VERDICT_COLORS = {"sure": "#2c2", "maybe": "#e90", "unknown": "#999"}
CELL = 96
SECTION_LABELS = {"maybe": "❓ Is this…? | محتاج تأكيد", "sure": "✅ Sure | متأكد",
                  "seen_before": "👤 Seen before | اتشاف قبل كده", "new": "🆕 New people | ناس جداد"}
LEVEL_COLORS = {"weak": "#c33", "medium": "#c80", "strong": "#2a2"}
LEVEL_TEXT = {"weak": "weak | ضعيف", "medium": "medium | متوسط", "strong": "strong | قوي"}


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
        sb.pack(side="right", fill="y")
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
                c.create_text(8, y + 12, text=self.headers[k], anchor="w", fill="#ddd",
                              font=("TkDefaultFont", 11, "bold"))
                y += 28
            x = col * cell + 6
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


# ============================================================== review window
class ReviewWindow(tk.Toplevel):
    def __init__(self, app: "App", review, mode: str, dest: str | None):
        super().__init__(app.root)
        self.app, self.review, self.mode, self.dest = app, review, mode, dest
        self.title(f"Review | المراجعة  —  {review.device}  ·  {review.folder}")
        self.geometry("1150x760")
        self.transient(app.root)
        self.names = app.lib.names()

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        c = review.counts()
        ttk.Label(top, font=("TkDefaultFont", 10, "bold"),
                  text=f"{len(review.photos)} photos · {len(review.faces)} faces   —   "
                       f"❓ {c['maybe']}   ✅ {c['sure']}   👤 {c['seen_before']}   🆕 {c['new']}").pack(side="left")
        ttk.Label(top, foreground="#666",
                  text="Keys: ←→↑↓ move · Space ✓/✗ · Del ✗ · Enter = OK, next · N = name · PgDn next").pack(side="right")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(body)
        body.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("n",), selectmode="browse", show="tree headings")
        self.tree.heading("#0", text="Groups | المجموعات")
        self.tree.heading("n", text="Faces")
        self.tree.column("#0", width=270)
        self.tree.column("n", width=50, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_group())

        right = ttk.Frame(body)
        body.add(right, weight=3)
        self.head = ttk.Label(right, font=("TkDefaultFont", 12, "bold"))
        self.head.pack(anchor="w")
        self.info = ttk.Label(right, foreground="#555")
        self.info.pack(anchor="w")
        namebar = ttk.Frame(right)
        namebar.pack(fill="x", pady=4)
        ttk.Label(namebar, text="Name | الاسم:").pack(side="left")
        self.name_var = tk.StringVar()
        self.name_box = ttk.Combobox(namebar, textvariable=self.name_var, width=28, values=self.names)
        self.name_box.pack(side="left", padx=6)
        self.name_box.bind("<<ComboboxSelected>>", lambda e: self.name_changed())
        self.name_box.bind("<KeyRelease>", lambda e: self.name_changed())
        self.name_box.bind("<Return>", lambda e: (self.name_changed(), self.focus_set(), "break")[-1])
        self.cands = ttk.Frame(namebar)
        self.cands.pack(side="left", padx=6)
        self.isolate_btn = ttk.Button(namebar, text="Isolate this person | اعزل الشخص ده", command=self.isolate)
        self.isolate_btn.pack(side="right", padx=6)
        ttk.Button(namebar, text="All ✓", command=lambda: self.set_all(True)).pack(side="right", padx=2)
        ttk.Button(namebar, text="All ✗", command=lambda: self.set_all(False)).pack(side="right", padx=2)
        self.grid = Grid(right, on_click=self.click)
        self.grid.pack(fill="both", expand=True)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Save | احفظ", command=self.save).pack(side="right", padx=4)
        ttk.Button(bottom, text="Later | بعدين", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bottom, text="Discard | الغي الجلسة", command=self.discard).pack(side="right", padx=4)
        where = f"  →  {Path(dest).name}" if dest else ""
        ttk.Label(bottom, text=f"Mode: {MODE_LABELS[mode]}{where}").pack(side="left")

        self.group_items: dict[str, int] = {}
        self.fill_tree()
        self.bind("<Left>", lambda e: self.move(-1))
        self.bind("<Right>", lambda e: self.move(1))
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
            return f"Unknown_{g.person_id:04d} · {g.seen_count}× · {g.devices} PC"
        return f"New person {self.review.groups.index(g) + 1}"

    def fill_tree(self) -> None:
        for sec in ("maybe", "sure", "seen_before", "new"):
            groups = [k for k, g in enumerate(self.review.groups) if g.section == sec]
            if not groups:
                continue
            parent = self.tree.insert("", "end", text=SECTION_LABELS[sec], values=(len(groups),), open=True)
            for k in groups:
                g = self.review.groups[k]
                item = self.tree.insert(parent, "end", text=self.label(g), values=(len(g.faces),))
                self.group_items[item] = k
        first = next(iter(self.group_items), None)
        if first:
            self.tree.selection_set(first)
        else:
            self.head.config(text="No faces found in these photos. | مفيش وشوش في الصور دي.")

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
        self.head.config(text=f"{SECTION_LABELS[g.section]}   —   {self.label(g)}")
        bits = [f"{len(g.faces)} faces"]
        low = sum(1 for i in g.faces if not rv.faces[i].good)
        if low:
            bits.append(f"{low} low quality (dark: sorted, not learned)")
        if g.already:
            bits.append(f"{len(g.already)} already learned")
        if g.section == "seen_before":
            bits.append(f"seen {g.seen_count} times on {g.devices} computer(s): give a name to remember them")
        if g.pool:
            bits.append(f"+{len(g.pool)} faces from earlier visits")
        self.info.config(text="   ·   ".join(bits))
        self.name_var.set(g.name)
        known = g.section in ("sure", "maybe") and g.person_id is not None
        self.isolate_btn.config(state="normal" if known else "disabled")
        for w in self.cands.winfo_children():
            w.destroy()
        for cname, score in g.candidates[:3]:
            ttk.Button(self.cands, text=f"{cname} ({score:.2f})",
                       command=lambda n=cname: (self.name_var.set(n), self.name_changed())).pack(side="left", padx=2)
        self.draw_faces(cursor=0)

    def draw_faces(self, cursor: int | None = None) -> None:
        g = self.current()
        if g is None:
            return
        rv = self.review
        items = [{"key": i, "jpg": rv.faces[i].crop, "on": g.ok(i), "dim": not rv.faces[i].good,
                  "caption": "✓ learned" if i in g.already else ""} for i in g.faces]
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
            self.head.config(text=f"{SECTION_LABELS[g.section]}   —   {self.label(g)}")

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
        if not messagebox.askyesno("Isolate | عزل", f"Take '{old}' out of the known people? Their faces go back to "
                                                    f"the unknown people, to be named again.\n\nتعزل '{old}'؟ "
                                                    f"وشوشه هترجع للمجهولين عشان تتسمّى تاني.", parent=self):
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
            dest = filedialog.askdirectory(parent=self, title="Where to put the sorted photos | تحط الصور فين")
            if not dest:
                return
        maybe_left = sum(1 for g in self.review.groups if g.section == "maybe" and not any(g.ok(i) for i in g.faces))
        if maybe_left and not messagebox.askyesno(
                "Save", f"{maybe_left} '❓ Is this…?' group(s) were not confirmed; they stay unknown. Save anyway?\n"
                        f"فيه {maybe_left} مجموعة محتاجة تأكيد متأكدتهاش، هتفضل مجهولة. تحفظ؟", parent=self):
            return
        review, mode, app = self.review, self.mode, self.app
        others = app.others_var.get()
        self.destroy()
        app.run_job(lambda: app.finish_save(save_review(app.lib, review, mode, Path(dest) if dest else None,
                                                        others, app.on_progress, app.log)))

    def discard(self) -> None:
        if messagebox.askyesno("Discard", "Forget this session without learning anything?\n"
                                          "تلغي الجلسة دي من غير ما تتعلم حاجة؟", parent=self):
            discard_session(self.app.lib, self.review.session_id)
            self.app.refresh_all()
            self.destroy()


# ===================================================================== app
class App:
    def __init__(self) -> None:
        enable_sharp_text()
        self.root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        self.root.title(f"Face Sorter v{__version__} - مكتبة الوشوش")
        self.root.geometry("1180x820")
        self.root.minsize(1040, 700)
        self.root.report_callback_exception = self.on_error
        try:
            self.settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.settings = {}
        st = self.settings
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

        self._build_card()
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        self.tab_pc = ttk.Frame(nb, padding=10)
        self.tab_who = ttk.Frame(nb, padding=10)
        self.tab_lib = ttk.Frame(nb, padding=10)
        nb.add(self.tab_pc, text="  This computer | الجهاز ده  ")
        nb.add(self.tab_who, text="  Who is this? | مين ده؟  ")
        nb.add(self.tab_lib, text="  Library | المكتبة  ")
        nb.bind("<<NotebookTabChanged>>", lambda e: nb.index("current") == 2 and self.refresh_library())
        self._build_pc()
        self._build_who()
        self._build_library()
        self.refresh_all()
        self.root.after(100, self.pump)

    # ============================================================ this computer
    def _build_pc(self) -> None:
        pad = {"padx": 8, "pady": 3}
        f = self.tab_pc
        f.columnconfigure(1, weight=1)
        self.lib_label = ttk.Label(f, foreground="#2a6")
        self.lib_label.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        ttk.Label(f, text="Photos on this computer | الصور").grid(row=1, column=0, sticky="w", **pad)
        src_entry = ttk.Entry(f, textvariable=self.src_var)
        src_entry.grid(row=1, column=1, sticky="ew", **pad)
        if TkinterDnD:  # drop a folder of photos here
            src_entry.drop_target_register(DND_FILES)
            src_entry.dnd_bind("<<Drop>>", lambda e: self.drop_folder(e.data))
        ttk.Button(f, text="...", width=4, command=lambda: self.pick(self.src_var)).grid(row=1, column=2, **pad)

        modes = ttk.LabelFrame(f, text=" Mode | الوضع ", padding=6)
        modes.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        for m in MODES:
            ttk.Radiobutton(modes, text=MODE_LABELS[m], value=m, variable=self.mode_var,
                            command=self.mode_changed).pack(side="left", padx=8)
        ttk.Label(modes, text="  Sorted photos to | الصور المرتبة في:").pack(side="left")
        self.dest_entry = ttk.Entry(modes, textvariable=self.dest_var, width=30)
        self.dest_entry.pack(side="left", padx=4)
        self.dest_btn = ttk.Button(modes, text="...", width=4, command=lambda: self.pick(self.dest_var))
        self.dest_btn.pack(side="left")

        filt = ttk.LabelFrame(f, text=" Skip | تجاهل ", padding=6)
        filt.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(filt, text="Screenshots | سكرين شوت", variable=self.f_shots).pack(side="left", padx=6)
        ttk.Checkbutton(filt, text="Stickers | ستيكرات", variable=self.f_stickers).pack(side="left", padx=6)
        ttk.Checkbutton(filt, text="Small pictures (<300px) | صور صغيرة", variable=self.f_small).pack(side="left", padx=6)
        ttk.Label(filt, text="  Folders | فولدرات:").pack(side="left")
        ttk.Entry(filt, textvariable=self.f_exclude, width=24).pack(side="left", padx=4)
        ttk.Label(filt, text="(names, comma-separated)", foreground="#777").pack(side="left")

        opts = ttk.Frame(f)
        opts.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(opts, text="Strictness | الدقة").pack(side="left")
        s_label = ttk.Label(opts, width=3)

        def on_strict(_=None):
            self.strict_var.set(round(float(self.strict_var.get())))
            s_label.config(text=str(self.strict_var.get()))
        ttk.Scale(opts, from_=1, to=10, variable=self.strict_var, command=on_strict, length=150).pack(
            side="left", padx=6)
        s_label.pack(side="left")
        on_strict()
        ttk.Label(opts, text="   Min photos for a new person").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=20, textvariable=self.minp_var, width=4).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="Copy Others / No_faces too", variable=self.others_var).pack(side="left", padx=10)

        speed = ttk.LabelFrame(f, text=" Speed | السرعة (can change while running) ", padding=6)
        speed.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        for lvl in LEVELS:
            ttk.Radiobutton(speed, text=LABELS[lvl], value=lvl, variable=self.speed_var,
                            command=self.on_speed).pack(side="left", padx=8)
        cb = ttk.Checkbutton(speed, text="🔋 Quiet on battery | هادي على البطارية", variable=self.battery_var,
                             command=self.on_speed)
        cb.pack(side="left", padx=14)
        if psutil is None:
            cb.config(state="disabled")
        self.battery_label = ttk.Label(speed, foreground="#c60")
        self.battery_label.pack(side="left")

        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=(10, 2))
        info = ttk.Frame(f)
        info.grid(row=7, column=0, columnspan=3, sticky="ew", padx=8)
        self.progress_label = ttk.Label(info, font=("TkDefaultFont", 10, "bold"))
        self.progress_label.pack(side="left")
        self.time_label = ttk.Label(info)
        self.time_label.pack(side="right")
        self.logbox = tk.Text(f, height=7, state="disabled", wrap="word")
        self.logbox.grid(row=8, column=0, columnspan=3, sticky="nsew", **pad)
        f.rowconfigure(8, weight=1)

        btns = ttk.Frame(f)
        btns.grid(row=9, column=0, columnspan=3, sticky="ew", **pad)
        self.who_btn = ttk.Button(btns, text="Who is this? | مين ده؟", command=lambda: self.nb.select(1))
        self.resume_btn = ttk.Button(btns, text="Continue review | كمّل المراجعة", command=self.resume_review)
        self.start_btn = ttk.Button(btns, text="Start | ابدأ", command=self.start)
        self.pause_btn = ttk.Button(btns, text="⏸ Pause | إيقاف مؤقت", state="disabled", command=self.toggle_pause)
        self.stop_btn = ttk.Button(btns, text="Stop", state="disabled", command=self.throttle.stop.set)
        self.who_btn.pack(side="left", padx=3)
        self.resume_btn.pack(side="left", padx=3)
        for b in (self.start_btn, self.stop_btn, self.pause_btn):
            b.pack(side="right", padx=3)
        self.mode_changed()

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
            self.pause_btn.config(text="⏸ Pause | إيقاف مؤقت")
            self._append("Continuing...")
        else:
            self.throttle.pause.set()
            self.paused_at = time.monotonic()
            self.pause_btn.config(text="▶ Continue | كمّل")
            self._append("Pausing after the current small batch...")

    def filters(self) -> Filters:
        return Filters(self.f_shots.get(), self.f_stickers.get(), 300 if self.f_small.get() else 0,
                       tuple(x.strip() for x in self.f_exclude.get().split(",") if x.strip()))

    def start(self) -> None:
        src = self.src_var.get().strip()
        mode = self.mode_var.get()
        if not src:
            messagebox.showwarning("Face Sorter", "Choose the photos folder first.\nاختار فولدر الصور الأول.")
            return
        if mode != "learn" and not self.dest_var.get().strip():
            d = filedialog.askdirectory(title="Where to put the sorted photos | تحط الصور المرتبة فين")
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
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.lib_stats = ttk.Label(top, font=("TkDefaultFont", 10, "bold"))
        self.lib_stats.pack(side="left")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(6, 0))
        self.lib_buttons = []
        for text, cmd in (("Who is this? | مين ده؟", lambda: self.nb.select(1)),
                          ("Possible duplicates | تكرار", self.open_duplicates),
                          ("Sessions & undo | الجلسات", self.open_sessions),
                          ("Log | السجل", self.open_log),
                          ("Backups | نسخ احتياطية", self.open_backups),
                          ("Reset learning", self.reset_learning),
                          ("About | عن البرنامج", self.show_about),
                          ("↻", self.refresh_library)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=2)
            self.lib_buttons.append(b)

        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(body)
        body.add(left, weight=2)
        ttk.Style().configure("People.Treeview", rowheight=44)
        self.tree = ttk.Treeview(left, columns=("n", "seen"), style="People.Treeview", selectmode="extended")
        self.tree.heading("#0", text="Person | الشخص")
        self.tree.heading("n", text="Faces")
        self.tree.heading("seen", text="Seen")
        self.tree.column("#0", width=210, stretch=True)
        self.tree.column("n", width=48, anchor="center", stretch=False)
        self.tree.column("seen", width=48, anchor="center", stretch=False)
        for lvl, col in LEVEL_COLORS.items():
            self.tree.tag_configure(lvl, foreground=col)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_person())

        right = ttk.Frame(body)
        body.add(right, weight=5)
        self.p_head = ttk.Label(right, font=("TkDefaultFont", 12, "bold"))
        self.p_head.pack(anchor="w")
        self.p_info = ttk.Label(right, foreground="#555")
        self.p_info.pack(anchor="w")
        act = ttk.Frame(right)
        act.pack(fill="x", pady=4)
        pbar = ttk.Frame(left)
        pbar.pack(fill="x", pady=4)
        ttk.Label(pbar, text="Selected | المحددين (Ctrl/Shift):", foreground="#666").pack(anchor="w")
        prow = ttk.Frame(pbar)
        prow.pack(fill="x")
        prow2 = ttk.Frame(pbar)
        prow2.pack(fill="x", pady=(2, 0))
        for parent, text, cmd in ((prow, "Name | سمّي", self.rename_person),
                                  (prow, "Join | ادمج", self.join_people),
                                  (prow, "Isolate | اعزل", self.isolate_people),
                                  (prow2, "Delete | امسح", self.delete_person),
                                  (prow2, "Export… | صدّر", self.export_people)):
            b = ttk.Button(parent, text=text, command=cmd)
            b.pack(side="left", padx=2)
            self.lib_buttons.append(b)
        b = ttk.Button(act, text="Not this person | مش الشخص ده", command=self.not_this_person)
        b.pack(side="left", padx=2)
        self.lib_buttons.append(b)
        self.move_var = tk.StringVar()
        self.move_box = ttk.Combobox(act, textvariable=self.move_var, width=20)
        b = ttk.Button(act, text="Move to → | انقل لـ", command=self.move_prints)
        b.pack(side="right", padx=2)
        self.move_box.pack(side="right", padx=2)
        self.lib_buttons.append(b)
        self.p_grid = Grid(right, size=88, on_click=self.toggle_print)
        self.p_grid.pack(fill="both", expand=True)
        self.tree_rows: dict[str, int] = {}
        self.tree_images: list = []
        self.p_selected: set[int] = set()

    def refresh_library(self) -> None:
        lib = self.lib
        st = lib.stats()
        self.lib_stats.config(text=f"{st['known']} people · {st['unknown']} unknown · {st['prints']} face prints · "
                                   f"{st['sessions']} visits on {st['devices']} computer(s) · {st['size_mb']} MB")
        keep = self.selected_pid()
        self.tree.delete(*self.tree.get_children())
        self.tree_rows.clear()
        self.tree_images.clear()
        known = lib.people("known")
        unknown = lib.people("unknown")
        k = self.tree.insert("", "end", text=f" Known | معروفين ({len(known)})", open=True)
        u = self.tree.insert("", "end", text=f" Unknown | مجهولين ({len(unknown)})", open=True)
        frequent = {p.id for p in lib.frequent_unknowns()}
        reselect = None
        for parent, people in ((k, known), (u, unknown)):
            for p in people:
                img = photo_from_array(lib.thumb(p.id), 38)
                self.tree_images.append(img)
                text = f" {p.label}" + ("  ⭐" if p.id in frequent else "")
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
            self.p_head.config(text="Choose a person | اختار شخص")
            self.p_info.config(text="")

    def selected_pid(self) -> int | None:
        pids = self.selected_pids()
        return pids[0] if pids else None

    def selected_pids(self) -> list[int]:
        return [self.tree_rows[i] for i in self.tree.selection() if i in self.tree_rows]

    def join_people(self) -> None:
        pids = self.selected_pids()
        if len(pids) < 2:
            messagebox.showinfo("Join | دمج", "Select two or more people (Ctrl+click).\nاختار شخصين أو أكتر.")
            return
        names = [p.name for p in (self.lib.person(x) for x in pids) if p and p.name]
        name = simpledialog.askstring("Join | دمج", f"Join {len(pids)} people as one. Name | الاسم:",
                                      initialvalue=names[0] if names else "", parent=self.root)
        if name and name.strip():
            self._changed(self.lib.merge_many(pids, name))

    def isolate_people(self) -> None:
        pids = [x for x in self.selected_pids() if (p := self.lib.person(x)) and p.kind == "known"]
        if not pids:
            messagebox.showinfo("Isolate | عزل", "Select known people to isolate.\nاختار ناس معروفين.")
            return
        labels = ", ".join(self.lib.person(x).label for x in pids)
        if messagebox.askyesno("Isolate | عزل", f"Isolate {labels}?\nThey go back to the unknown people, to be named "
                                                f"again.\n\nيرجعوا للمجهولين عشان يتسمّوا تاني."):
            self._changed(self.lib.isolate(pids))

    def export_people(self) -> None:
        pids = self.selected_pids()
        if not pids:
            messagebox.showinfo("Export | تصدير", "Select the people to export.\nاختار الناس الأول.")
            return
        d = filedialog.askdirectory(title="Where to create the new library | المكتبة الجديدة فين")
        if not d:
            return
        dest = Path(d) / f"FaceSorter_Library_export_{datetime.now():%Y-%m-%d_%H%M}"
        path = self.lib.export_people(pids, dest)
        messagebox.showinfo("Exported | اتصدّرت", f"A new library with {len(pids)} people:\n{path}\n\n"
                                                  "(It has no password.)")

    def show_person(self) -> None:
        pid = self.selected_pid()
        if pid is None:
            return
        p = self.lib.person(pid)
        if p is None:
            return
        self.p_selected = set()
        level = f"  ·  knows them: {LEVEL_TEXT[p.level]}" if p.kind == "known" else ""
        self.p_head.config(text=f"{p.label}{level}")
        bits = [f"{p.manual} confirmed + {p.auto} learned faces", f"{p.looks} look(s)",
                f"seen {p.seen_count} times", f"on: {', '.join(p.devices) or '-'}", f"last: {p.last_seen or '-'}"]
        if p.kind == "unknown":
            bits.insert(0, "no name yet: use 'Name' to remember them")
        if p.note:
            bits.insert(0, p.note)
        self.p_info.config(text="   ·   ".join(bits))
        items, headers, last_year = [], {}, None
        for k, (print_id, crop, date, source, quality, device) in enumerate(self.lib.prints_of(pid)):
            year = (date or "")[:4] or "?"
            if year != last_year:
                headers[k] = year if year != "?" else "No date | من غير تاريخ"
                last_year = year
            items.append({"key": f"p{print_id}", "jpg": crop, "on": None, "pid": print_id,
                          "caption": "auto" if source == "auto" else ""})
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
        new = simpledialog.askstring("Name", "Name (a saved name joins them) | الاسم:", initialvalue=p.name or "",
                                     parent=self.root)
        if new and new.strip():
            self._changed(self.lib.rename(pid, new))

    def delete_person(self) -> None:
        people = [p for p in (self.lib.person(x) for x in self.selected_pids()) if p]
        if not people:
            return
        labels = ", ".join(p.label for p in people)
        if messagebox.askyesno("Delete", f"Delete {labels} and everything the library knows about them?\n"
                                         f"تمسح {labels} وكل اللي المكتبة تعرفه عنهم؟"):
            for p in people:
                self._changed(self.lib.delete_person(p.id))

    def not_this_person(self) -> None:
        if not self.p_selected:
            messagebox.showinfo("Face Sorter", "Click the wrong faces first.\nدوس على الوشوش الغلط الأول.")
            return
        self._changed(self.lib.remove_prints(sorted(self.p_selected)))

    def move_prints(self) -> None:
        name = self.move_var.get().strip()
        if not self.p_selected or not name:
            messagebox.showinfo("Face Sorter", "Click some faces and choose a name.\nاختار وشوش واسم.")
            return
        self._changed(self.lib.move_prints(sorted(self.p_selected), name))

    def reset_learning(self) -> None:
        pid = self.selected_pid()
        p = self.lib.person(pid) if pid else None
        known = p is not None and p.kind == "known"
        who = p.label if known else "everyone"
        if messagebox.askyesno("Reset learning", f"Delete what was learned automatically for {who}? "
                                                 f"What you confirmed stays.\nامسح التعلم التلقائي لـ {who}؟"):
            n = self.lib.reset_auto(p.id if known else None)
            self._changed(f"Reset learning for {who}: {n} faces removed.")

    # ------------------------------------------------------------- small windows
    # ============================================================ who is this? tab
    def _build_who(self) -> None:
        f = self.tab_who
        drop = tk.Canvas(f, height=90, background="#eef4ff", highlightthickness=2, highlightbackground="#89a")
        drop.pack(fill="x")
        hint = ("Drop photos here | اسحب الصور وافلتها هنا" if TkinterDnD else
                "Choose photos with the button | اختار الصور بالزرار")
        drop.create_text(10, 45, anchor="w", text="🖼  " + hint, font=("TkDefaultFont", 14, "bold"), fill="#345")
        drop.bind("<Button-1>", lambda e: self.who_choose())
        if TkinterDnD:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", lambda e: self.who_add(dropped_paths(self.root, e.data)))
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="Choose photos… | اختار صور", command=self.who_choose).pack(side="left")
        ttk.Button(bar, text="Clear | امسح القائمة", command=self.who_clear).pack(side="left", padx=4)
        ttk.Label(bar, text="Nothing changes in the library unless you press Save | المكتبة مش بتتغير غير لما تدوس احفظ",
                  foreground="#666").pack(side="right")
        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True)
        self.who_list = tk.Listbox(body, width=28)
        body.add(self.who_list, weight=1)
        self.who_list.bind("<<ListboxSelect>>", lambda e: self.who_show())
        self.who_canvas = tk.Canvas(body, background="#222", highlightthickness=0)
        body.add(self.who_canvas, weight=3)
        self.who_canvas.bind("<Configure>", lambda e: self.who_draw())
        right = ttk.Frame(body)
        body.add(right, weight=3)
        holder = tk.Canvas(right, highlightthickness=0, width=420)
        sb = ttk.Scrollbar(right, orient="vertical", command=holder.yview)
        self.who_faces = ttk.Frame(holder)
        self.who_faces.bind("<Configure>", lambda e: holder.configure(scrollregion=holder.bbox("all")))
        holder.create_window((0, 0), window=self.who_faces, anchor="nw")
        holder.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        holder.pack(fill="both", expand=True)
        self.who_paths: list[Path] = []
        self.who_result = None   # (path, img, results)
        self.who_images: list = []

    def who_choose(self) -> None:
        files = filedialog.askopenfilenames(title="Who is this? | مين ده؟",
                                            filetypes=[("Photos", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
                                                       ("All files", "*.*")])
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
            self.nb.select(1)
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
            c.create_rectangle(x0, y0 - 18, x0 + 8 * len(label) + 8, y0, fill=color, outline=color)
            c.create_text(x0 + 4, y0 - 9, text=label, anchor="w", fill="black", font=("TkDefaultFont", 10, "bold"))

    def who_cards(self) -> None:
        for w in self.who_faces.winfo_children():
            w.destroy()
        self.who_images = []
        _, _, results = self.who_result
        if not results:
            ttk.Label(self.who_faces, text="No faces found. | مفيش وشوش.", padding=10).pack()
            return
        names = self.lib.names()
        for k, r in enumerate(results):
            card = ttk.Frame(self.who_faces, padding=6, relief="groove")
            card.pack(fill="x", pady=3, padx=2)
            img = photo_from_array(r["crop"], 72)
            self.who_images.append(img)
            ttk.Label(card, image=img).grid(row=0, column=0, rowspan=3, sticky="n")
            mark = {"sure": "✅", "maybe": "❓", "unknown": "❔"}[r["verdict"]]
            if r["matches"] and r["verdict"] != "unknown":
                text = f"{k + 1}. {mark} {r['matches'][0][0]}  ({r['matches'][0][1]:.2f})"
                if len(r["matches"]) > 1:
                    text += "\nor | أو: " + ", ".join(f"{n} ({sc:.2f})" for n, sc in r["matches"][1:3])
            else:
                text = f"{k + 1}. {mark} Unknown | مش معروف" + (f"\nSeen before: {r['seen_before']}"
                                                                 if r["seen_before"] else "")
            if not r["good"]:
                text += "\n(low quality | مش واضح)"
            ttk.Label(card, text=text, wraplength=300, justify="left").grid(row=0, column=1, columnspan=2, sticky="w",
                                                                            padx=6)
            var = tk.StringVar(value=r["matches"][0][0] if r["matches"] and r["verdict"] != "unknown" else "")
            box = ttk.Combobox(card, textvariable=var, values=names, width=18)
            box.grid(row=1, column=1, sticky="w", padx=6, pady=2)
            status = ttk.Label(card, foreground="#2a2")
            status.grid(row=2, column=1, sticky="w", padx=6)

            def save(r=r, var=var, status=status):
                msg = self.lib.save_face(var.get(), r["face"])
                if msg:
                    status.config(text="✓ " + msg)
                    self._append(msg)
                    self.refresh_card()
            ttk.Button(card, text="Save | احفظ", command=save).grid(row=1, column=2, padx=4)

    def open_duplicates(self) -> None:
        pairs = self.lib.duplicates()
        win = tk.Toplevel(self.root)
        win.title("Possible duplicates | يمكن نفس الشخص")
        win.geometry("820x520")
        if not pairs:
            ttk.Label(win, text="No possible duplicates. | مفيش تكرار.", padding=20).pack()
            return
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        win.images = []
        for a, b, score in pairs[:20]:
            pa, pb = self.lib.person(a), self.lib.person(b)
            row = ttk.Frame(frame, padding=4)
            row.pack(fill="x")
            for p in (pa, pb):
                img = photo_from_array(self.lib.thumb(p.id), 64)
                win.images.append(img)
                ttk.Label(row, image=img).pack(side="left")
                ttk.Label(row, text=f"{p.label} ({p.prints})", padding=(4, 0, 12, 0)).pack(side="left")
            ttk.Label(row, text=f"{score:.2f}").pack(side="left", padx=8)

            def merge(src, dst, r=row):
                self._changed(self.lib.merge(src, dst))
                r.destroy()

            def differ(x=a, y=b, r=row):
                self.lib.not_duplicate(x, y)
                r.destroy()
            ttk.Button(row, text="Different | مختلفين", command=differ).pack(side="right", padx=2)
            ttk.Button(row, text=f"→ {pb.label}", command=lambda s=a, d=b: merge(s, d)).pack(side="right", padx=2)
            ttk.Button(row, text=f"→ {pa.label}", command=lambda s=b, d=a: merge(s, d)).pack(side="right", padx=2)

    def open_sessions(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Sessions | الجلسات")
        win.geometry("860x540")
        lst = tk.Listbox(win, height=10)
        lst.pack(fill="x", padx=10, pady=8)
        txt = tk.Text(win, wrap="word", height=12)
        txt.pack(fill="both", expand=True, padx=10)
        sessions = self.lib.sessions()
        for sid, started, device, folder, mode, status, report in sessions:
            lst.insert("end", f"#{sid}  {started or ''}  {device or ''}  {status}  {folder or ''}")

        def show(_=None):
            sel = lst.curselection()
            txt.delete("1.0", "end")
            if sel:
                txt.insert("end", sessions[sel[0]][6] or "(no report)")
        lst.bind("<<ListboxSelect>>", show)

        def undo():
            last = self.lib.last_saved_session()
            if not last:
                messagebox.showinfo("Undo", "Nothing to undo.", parent=win)
                return
            if messagebox.askyesno("Undo", f"Undo session #{last[0]}? Everything it added to the library is "
                                           f"removed (also changes made after it).\nتلغي آخر جلسة؟", parent=win):
                self._changed(self.lib.undo_last_session())
                win.destroy()
        b = ttk.Frame(win, padding=10)
        b.pack(fill="x")
        ttk.Button(b, text="Undo last session | الغي آخر جلسة", command=undo).pack(side="left")
        ttk.Button(b, text="Close | قفل", command=win.destroy).pack(side="right")

    def open_backups(self) -> None:
        backups = self.lib.backups()
        win = tk.Toplevel(self.root)
        win.title("Backups | النسخ الاحتياطية")
        win.geometry("620x380")
        lst = tk.Listbox(win)
        lst.pack(fill="both", expand=True, padx=10, pady=8)
        for bk in backups:
            lst.insert("end", bk.name)

        def restore():
            sel = lst.curselection()
            if sel and messagebox.askyesno("Restore", f"Put the library back to\n{backups[sel[0]].name}?\n"
                                                      "ترجع المكتبة للنسخة دي؟", parent=win):
                self.lib.backup("before_restore")
                self.lib.restore(backups[sel[0]])
                self._changed(f"Library restored from {backups[sel[0]].name}")
                win.destroy()
        b = ttk.Frame(win, padding=10)
        b.pack(fill="x")
        ttk.Button(b, text="Restore | رجّع", command=restore).pack(side="left")
        ttk.Button(b, text="Open folder", command=lambda: open_path(self.lib.folder)).pack(side="left", padx=6)
        ttk.Button(b, text="Close | قفل", command=win.destroy).pack(side="right")

    def open_log(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Log | السجل")
        win.geometry("880x520")
        kind = tk.StringVar(value="history")
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        txt = tk.Text(win, wrap="word")
        txt.pack(fill="both", expand=True, padx=8)

        def show():
            kinds = {"history": ("history",), "memory": ("memory",), "all": ("history", "memory", "run")}[kind.get()]
            txt.config(state="normal")
            txt.delete("1.0", "end")
            for at, k, t in self.lib.log_entries(kinds):
                txt.insert("end", f"{at}  [{k}]  {t}\n")
            txt.config(state="disabled")
        for value, label in (("history", "History | التاريخ"), ("memory", "Memory | الذاكرة"),
                             ("all", "Everything | كل حاجة")):
            ttk.Radiobutton(top, text=label, value=value, variable=kind, command=show).pack(side="left", padx=6)

        def save():
            f = filedialog.asksaveasfilename(parent=win, defaultextension=".txt", initialfile="face_sorter_log.txt")
            if f:
                self.lib.export_log(Path(f))
        ttk.Button(top, text="Save as text | احفظ", command=save).pack(side="right")
        show()

    # ============================================================ library card
    def _build_card(self) -> None:
        card = ttk.LabelFrame(self.root, text=" 📚 Library | المكتبة ", padding=(10, 4))
        card.pack(fill="x", padx=10, pady=(8, 0))
        self.card_path = ttk.Label(card, font=("TkDefaultFont", 10, "bold"))
        self.card_path.pack(anchor="w")
        self.card_info = ttk.Label(card, foreground="#555")
        self.card_info.pack(anchor="w")
        row = ttk.Frame(card)
        row.pack(fill="x", pady=(4, 0))
        self.card_buttons = []
        for text, cmd, side in (("Open folder | افتح الفولدر", lambda: open_path(self.lib.folder), "left"),
                                ("Save a copy… | احفظ نسخة", self.card_copy, "left"),
                                ("Move… | انقل", self.card_move, "left"),
                                ("Open another… | افتح تانية", self.card_open_other, "left"),
                                ("Default | الافتراضية", self.card_default, "left"),
                                ("Clean… | نضّف", self.card_clean, "right"),
                                ("🔒 Password… | الباسورد", self.card_password, "right")):
            b = ttk.Button(row, text=text, command=cmd)
            b.pack(side=side, padx=2)
            self.card_buttons.append(b)
        if TkinterDnD:  # drop a library file on the card to open or merge it
            for w in (card, self.card_path, self.card_info):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: self.drop_library(e.data))

    def refresh_card(self) -> None:
        lib = self.lib
        st, size = lib.stats(), lib.size_info()
        lock = "  🔒" if lib.encrypted else ""
        default = "  (default | الافتراضية)" if lib.folder == LIBRARY_DIR.resolve() else ""
        self.card_path.config(text=f"{lib.path}{lock}{default}")
        self.card_info.config(text=(
            f"Created | اتعملت {lib.created}   ·   Updated | آخر تحديث {lib.updated}   ·   "
            f"{size['file']} MB (people {size['people']}, unknown {size['unknown']}, scan memory {size['cache']}, "
            f"backups {size['backups']})   ·   {st['known']} people · {st['unknown']} unknown"))

    def ask_password(self, where: Path, wrong: bool = False) -> str | None:
        msg = (f"{'Wrong password. ' if wrong else ''}This library has a password:\n{where}\n\n"
               "Type the password or the recovery key.\nاكتب الباسورد أو مفتاح الاسترجاع.")
        return simpledialog.askstring("Password | الباسورد", msg, show="•", parent=self.root)

    def choose_file(self, files: list[Path]) -> Path | None:
        win = tk.Toplevel(self.root)
        win.title("Which library? | أنهي مكتبة؟")
        win.geometry("700x300")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, padding=10, text="This folder has more than one library file. Choose one:\n"
                                        "الفولدر فيه أكتر من مكتبة، اختار واحدة:").pack(anchor="w")
        lst = tk.Listbox(win)
        lst.pack(fill="both", expand=True, padx=10)
        for f in files:
            lst.insert("end", f.name)
        chosen: list[Path] = []

        def ok():
            sel = lst.curselection()
            if sel:
                chosen.append(files[sel[0]])
            win.destroy()
        ttk.Button(win, text="Open | افتح", command=ok).pack(pady=8)
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
                messagebox.showerror("Face Sorter", f"No library in\n{folder}\nمفيش مكتبة هنا.")
                return None

    def startup_library(self) -> Library | None:
        place = setting_path(self.settings.get("library"))
        if place and not place.exists():
            answer = messagebox.askyesnocancel(
                "Library not found | المكتبة مش موجودة",
                f"The library is not at:\n{place}\n(Maybe the USB stick is not connected?)\n\n"
                f"Yes = use the default library next to the program\nNo = choose another library\nCancel = close\n\n"
                f"المكتبة مش موجودة. Yes = الافتراضية، No = اختار مكتبة، Cancel = اقفل")
            if answer is None:
                return None
            if answer:
                place = None
            else:
                d = filedialog.askdirectory(title="Library folder | فولدر المكتبة")
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
        self._append(f"Library: {lib.path}")

    def card_copy(self) -> None:
        d = filedialog.askdirectory(title="Where to save the copy | تحفظ النسخة فين")
        if d:
            folder = self.lib.copy_to(Path(d))
            messagebox.showinfo("Saved | اتحفظت", f"Copy saved in:\n{folder}\n\nThis library stays where it is.\n"
                                                  f"المكتبة الأصلية زي ما هي.")

    def card_move(self) -> None:
        d = filedialog.askdirectory(title="Move the library to | تنقل المكتبة فين")
        if not d:
            return
        dest = Path(d) / "Library"
        if messagebox.askyesno("Move | نقل", f"Move the library (with its backups) to\n{dest}\n"
                                             "It will be used from there from now on.\nتنقل المكتبة هناك؟"):
            try:
                self.lib.move_to(dest)
            except RuntimeError as e:
                messagebox.showerror("Face Sorter", str(e))
                return
            self.switch_to(self.lib)

    def card_open_other(self) -> None:
        d = filedialog.askdirectory(title="Library folder to open | فولدر المكتبة")
        if d:
            self.switch_to(self.open_library(Path(d), create=False))

    def card_default(self) -> None:
        self.switch_to(self.open_library(LIBRARY_DIR))

    def card_password(self) -> None:
        lib = self.lib
        if lib.encrypted:
            answer = messagebox.askyesnocancel(
                "Password | الباسورد", "Yes = change the password\nNo = remove the password (the library is no longer "
                                       "encrypted)\n\nYes = غيّر الباسورد، No = شيل الباسورد")
            if answer is None:
                return
            if not answer:
                if messagebox.askyesno("Remove password", "Really remove the password?\nتشيل الباسورد؟"):
                    lib.remove_password()
                    self.refresh_card()
                return
        new = simpledialog.askstring("Password | الباسورد", "New password | الباسورد الجديد:", show="•",
                                     parent=self.root)
        if not new:
            return
        again = simpledialog.askstring("Password | الباسورد", "Again | تاني:", show="•", parent=self.root)
        if new != again:
            messagebox.showerror("Face Sorter", "The two passwords are different.\nالباسوردين مختلفين.")
            return
        recovery = lib.set_password(new)
        self.refresh_card()
        if recovery:
            self.show_recovery_key(recovery)
        else:
            messagebox.showinfo("Password | الباسورد", "Password changed. The recovery key stays the same.\n"
                                                       "الباسورد اتغير، ومفتاح الاسترجاع زي ما هو.")

    def show_recovery_key(self, key: str) -> None:
        win = tk.Toplevel(self.root)
        win.title("Recovery key | مفتاح الاسترجاع")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, padding=12, wraplength=520, text=(
            "The library is now encrypted. Keep this recovery key somewhere safe (on paper, NOT on the same USB "
            "stick). If you forget the password, it is the ONLY way to open the library.\n\n"
            "المكتبة اتشفرت. احفظ مفتاح الاسترجاع ده في مكان آمن (ورقة، مش على نفس الفلاشة). لو نسيت الباسورد، "
            "ده الطريقة الوحيدة لفتح المكتبة.")).pack()
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
        ttk.Button(b, text="Copy | انسخ", command=copy).pack(side="left", padx=4)
        ttk.Button(b, text="Save as text… | احفظ", command=save).pack(side="left", padx=4)
        ttk.Button(b, text="I saved it | حفظته", command=win.destroy).pack(side="left", padx=4)
        self.root.wait_window(win)

    def card_clean(self) -> None:
        size = self.lib.size_info()
        if messagebox.askyesno("Clean | نضّف", f"Forget the scan results of {size['cache_photos']} photos "
                                               f"({size['cache']} MB)?\nNo person or face is deleted; photos met "
                                               f"again are simply checked again.\n\nتمسح نتايج فحص الصور القديمة؟ "
                                               f"مفيش أي شخص أو وش هيتمسح."):
            self._changed(self.lib.clean())

    def drop_library(self, data: str) -> None:
        for p in dropped_paths(self.root, data):
            if p.is_dir() or p.suffix.lower() == ".db":
                answer = messagebox.askyesnocancel(
                    "Library | مكتبة", f"{p}\n\nYes = open this library\nNo = merge it into the current library\n\n"
                                      f"Yes = افتحها، No = ادمجها في المكتبة الحالية")
                if answer is None:
                    return
                if answer:
                    self.switch_to(self.open_library(p if p.is_dir() else p.parent, file=None if p.is_dir() else p,
                                                     create=False))
                else:
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
        messagebox.showinfo("Face Sorter", (
            f"Face Sorter v{__version__}\nFree and open source (MIT license).\n\n"
            "Everything runs on this computer. No photo or data is uploaded.\n"
            "The library holds face data and names: only use photos you have the right to use, and set a "
            "password if the library is on a USB stick.\n"
            "Models: YuNet (MIT) and SFace (Apache-2.0) from OpenCV Zoo. Other licenses: THIRD_PARTY_LICENSES.md\n\n"
            "برنامج مجاني ومفتوح المصدر. كل الشغل على الجهاز ومفيش حاجة بتترفع. المكتبة فيها بيانات وشوش: "
            "استخدمه على صور ليك حق فيها، وحط باسورد لو المكتبة على فلاشة."))

    def on_error(self, exc, val, tb) -> None:
        text = "".join(traceback.format_exception(exc, val, tb))
        log_error("window", text)
        messagebox.showerror("Face Sorter", f"Something went wrong:\n{val}\n\nDetails were saved in\n{ERRORS}\n"
                                            f"حصلت مشكلة، والتفاصيل اتحفظت في errors.txt")

    # ================================================================ plumbing
    def log(self, text: str) -> None:
        self.msgs.put(("log", text))

    def on_progress(self, stage: int, done: int, total: int) -> None:
        self.msgs.put(("progress", (stage, done, total)))

    def _append(self, text: str) -> None:
        self.logbox.config(state="normal")
        self.logbox.insert("end", text + "\n")
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
                "exclude": self.f_exclude.get()})
            SETTINGS.write_text(json.dumps(self.settings, ensure_ascii=False, indent=1), encoding="utf-8")
        except (OSError, ValueError, tk.TclError):
            pass

    def refresh_all(self) -> None:
        self.refresh_card()
        self.lib_label.config(text=f"This computer | الجهاز ده: {device_name()}")
        pending = pending_session(self.lib)
        self.resume_btn.config(state="normal" if pending and not self.busy else "disabled")
        if self.nb.index("current") == 2:
            self.refresh_library()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for b in [self.start_btn, self.who_btn, self.resume_btn] + self.lib_buttons + self.card_buttons:
            b.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        self.pause_btn.config(state="normal" if busy else "disabled", text="⏸ Pause | إيقاف مؤقت")
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
                self.msgs.put(("error", f"{type(e).__name__}: {e}\n\nDetails: {ERRORS}"))
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
        self.progress_label.config(text=f"{pct:.0f}%{count} · Stage {min(stage, 4)}/4: {STAGES.get(stage, '')}")
        elapsed = f"⏱ {fmt_time(now - self.started)}"
        if self.throttle.pause.is_set():
            self.time_label.config(text=f"{elapsed} · ⏸ Paused | متوقف مؤقتاً")
            return
        st0, t0, d0 = self.stage_start
        rate = (done - d0) / (now - t0) if st0 == stage and now - t0 > 2 and done > d0 else 0
        if rate > 0 and total > done:
            left = f" · left ~{fmt_time((total - done) / rate)} | فاضل"
        elif total and done >= total:
            left = ""
        else:
            left = " · calculating... | بيحسب"
        self.time_label.config(text=f"{elapsed} elapsed | عدّى{left}")
        on_batt = self.throttle.on_battery and self.throttle.battery_saver
        self.battery_label.config(text="🔋 On battery: quiet | على البطارية: هادي" if on_batt else "")

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
                messagebox.showinfo("Saved | اتحفظ", val)
            elif kind == "done":
                self.bar["value"] = 100
                self.progress_label.config(text="100% · Done | خلص")
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
