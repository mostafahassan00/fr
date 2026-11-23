"""The window (Tkinter).

    ┌──────────┬───────────────────────────────────────────────┐
    │ side     │ the library: name, folder, numbers   🌐  ⚙    │
    │ menu     ├───────────────────────────────────────────────┤
    │ 🖥 scan   │ the page: a title, then white cards           │
    │ 🔍 who    │                                               │
    │ 🔎 find   │                                               │
    │ 👥 people │                                               │
    │ 📚 library├───────────────────────────────────────────────┤
    │          │ status: the result of the last thing you did  │
    └──────────┴───────────────────────────────────────────────┘

One language at a time (Arabic by default, laid out from the right; English from the 🌐 button).
Rows of buttons wrap onto a new line instead of hiding what does not fit; the side menu shrinks
to icons on narrow windows. The review window opens after a scan.
"""
from __future__ import annotations

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
from .engine import HEIF_EXTS, FaceEngine, Filters, read_heif
from .i18n import LANGUAGES, language, person_label, rtl, set_language, tr
from .library import _ROOT, LIBRARY_DIR, Library, LockedError, MultipleLibrariesError, copy_file, device_name
from .perf import LEVELS, Throttle, psutil, speed_label
from .session import (MODES, build_review, discard_session, find_people, identify, mode_label, pending_session,
                      save_review, scan, stage_label)
from .ui_kit import (C, LEVEL_COLORS, VERDICT_COLORS, Card, ErrorGate, Flow, Fonts, Grid, ScrollFrame, SearchBox,
                     Tip, WrapLabel, anchor, app_icon, chip, choose, end, fit_window, justify, layout_problems,
                     menu_button, photo_from_array, round_thumb, shorten_middle, start, style_tk, theme)

try:  # drag and drop of files onto the window
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # pragma: no cover - the portable build has it
    TkinterDnD = None
    DND_FILES = None

__all__ = ["App", "ReviewWindow", "main", "layout_problems", "Filters", "messagebox", "simpledialog", "choose"]

SETTINGS = _ROOT / "settings.json"
ERRORS = _ROOT / "errors.txt"
PAGES = ("scan", "who", "find", "people", "library")
FIND_LIMIT = 600      # photos shown by "find" (the copy button still takes all of them)


def page_names() -> dict[str, tuple[str, str]]:
    return {"scan": ("🖥", tr("This computer", "الجهاز ده")),
            "who": ("🔍", tr("Who is this?", "مين ده؟")),
            "find": ("🔎", tr("Find", "دوّر")),
            "people": ("👥", tr("People", "الناس")),
            "library": ("📚", tr("Library", "المكتبة"))}


def section_label(section: str) -> str:
    return {"maybe": tr("❓ Is this…?", "❓ محتاج تأكيد"), "sure": tr("✅ Sure", "✅ متأكد"),
            "seen_before": tr("👤 Seen before", "👤 اتشاف قبل كده"), "new": tr("🆕 New people", "🆕 ناس جداد")}[section]


def level_text(level: str) -> str:
    return {"weak": tr("weak", "ضعيفة"), "medium": tr("medium", "متوسطة"), "strong": tr("strong", "قوية")}[level]


def mode_help(mode: str) -> str:
    return {"learn": tr("The program gets to know the people in the photos and remembers them. "
                        "No photo is copied.",
                        "البرنامج بيتعرف على الناس اللي في الصور ويفتكرهم، من غير ما ينسخ أي صورة."),
            "learn_sort": tr("Learns the people, then copies the photos into a folder for each person.",
                             "بيتعرف على الناس، وبعدين بينسخ الصور في فولدر لكل شخص."),
            "sort": tr("Only copies the photos into a folder for each person it already knows. "
                       "The library does not change.",
                       "بينسخ الصور بس في فولدر لكل شخص يعرفه. المكتبة مش بتتغير.")}[mode]


# ---------------------------------------------------------------- helpers
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


def ask_yes(parent: tk.Misc, title: str, message: str) -> bool:
    return messagebox.askyesno(title, message, parent=parent)


def photo_thumb(path: Path, size: int) -> np.ndarray | None:
    """A small picture of a photo file (for 'find'), read quickly."""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if path.suffix.lower() in HEIF_EXTS:
        img, _ = read_heif(data.tobytes())
    else:
        flag = cv2.IMREAD_REDUCED_COLOR_4 if len(data) > 600_000 else cv2.IMREAD_COLOR
        img = cv2.imdecode(data, flag)
    if img is None:
        return None
    h, w = img.shape[:2]
    k = size / max(h, w)
    return cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)


def page_bg_window(win: tk.Toplevel) -> None:
    win.configure(background=C["bg"])


# ============================================================== review window
class ReviewWindow(tk.Toplevel):
    def __init__(self, app: "App", review, mode: str, dest: str | None):
        super().__init__(app.root)
        page_bg_window(self)
        self.app, self.review, self.mode, self.dest = app, review, mode, dest
        self.title(f"{tr('Review', 'المراجعة')}  —  {review.device}  ·  {review.folder}")
        fit_window(self, 1240, 820)
        self.transient(app.root)
        self.names = app.lib.names()

        head = ttk.Frame(self, style="Page.TFrame", padding=(16, 12, 16, 4))
        head.pack(fill="x")
        top = Flow(head, padx=4, style="Page.TFrame")
        top.pack(fill="x")
        c = review.counts()
        top.add(ttk.Label(top, text=tr("Review", "المراجعة"), style="PageTitle.TLabel"))
        top.add(chip(top, tr(f"{len(review.photos)} photos", f"{len(review.photos)} صورة")))
        top.add(chip(top, tr(f"{len(review.faces)} faces", f"{len(review.faces)} وش")))
        for sec, color, soft in (("maybe", C["warn"], C["warn_soft"]), ("sure", C["ok"], C["ok_soft"]),
                                 ("seen_before", C["muted"], ""), ("new", C["accent"], C["accent_soft"])):
            top.add(chip(top, f"{section_label(sec)}  {c[sec]}", color, soft))
        WrapLabel(head, style="PageSub.TLabel", text=tr(
            "Click a face to accept ✓ or reject ✗ it.  Keys: arrows move · Space ✓/✗ · Del ✗ · "
            "Enter = OK, next group · N = name · PgDn next group",
            "دوس على أي وش عشان تقبله ✓ أو ترفضه ✗.  الكيبورد: الأسهم للتنقل · Space ✓/✗ · Del ✗ · "
            "Enter تمام واللي بعدها · N الاسم · PgDn المجموعة اللي بعدها")).pack(fill="x", pady=(4, 0))

        bottom = Flow(self, padx=4, style="Page.TFrame")
        bottom.pack(side="bottom", fill="x", padx=16, pady=10)
        where = f"  →  {Path(dest).name}" if dest else ""
        bottom.add(ttk.Label(bottom, text=tr("Mode: ", "الوضع: ") + mode_label(mode) + where, style="PageSub.TLabel"))
        bottom.add(ttk.Button(bottom, text=tr("✓ Save", "✓ احفظ"), style="Primary.TButton", command=self.save),
                   end=True)
        bottom.add(ttk.Button(bottom, text=tr("Later", "بعدين"), command=self.destroy), end=True)
        bottom.add(ttk.Button(bottom, text=tr("Discard session", "الغي الجلسة"), style="Danger.TButton",
                              command=self.discard), end=True)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        left = Card(body, tr("Groups", "المجموعات"), padding=(10, 10))
        right = Card(body, padding=(14, 12))
        self.tree = ttk.Treeview(left.body, columns=("n",), selectmode="browse", show="tree")
        self.tree.column("#0", width=250)
        self.tree.column("n", width=max(60, Fonts.bold.measure("00/00") + 20), anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_group())
        for pane, weight in ((right, 3), (left, 1)) if rtl() else ((left, 1), (right, 3)):
            body.add(pane, weight=weight)

        r = right.body
        self.head = WrapLabel(r, font=Fonts.head)
        self.head.pack(fill="x")
        self.info = WrapLabel(r, style="Muted.TLabel")
        self.info.pack(fill="x", pady=(2, 6))
        namebar = Flow(r, padx=3)
        namebar.pack(fill="x", pady=2)
        namebar.add(ttk.Label(namebar, text=tr("Name:", "الاسم:"), style="Bold.TLabel"))
        self.name_var = tk.StringVar()
        self.name_box = ttk.Combobox(namebar, textvariable=self.name_var, width=24, values=self.names,
                                     justify=justify())
        namebar.add(self.name_box)
        self.name_box.bind("<<ComboboxSelected>>", lambda e: self.name_changed())
        self.name_box.bind("<KeyRelease>", lambda e: self.name_changed())
        self.name_box.bind("<Return>", lambda e: (self.name_changed(), self.focus_set(), "break")[-1])
        self.isolate_btn = ttk.Button(namebar, text=tr("Isolate this person", "اعزل الشخص ده"), command=self.isolate)
        namebar.add(self.isolate_btn, end=True)
        namebar.add(ttk.Button(namebar, text=tr("All ✓", "الكل ✓"), command=lambda: self.set_all(True)), end=True)
        namebar.add(ttk.Button(namebar, text=tr("All ✗", "الكل ✗"), command=lambda: self.set_all(False)), end=True)
        self.cands = Flow(r, padx=2)
        self.cands.pack(fill="x")
        self.grid = Grid(r, on_click=self.click)
        self.grid.pack(fill="both", expand=True, pady=(6, 0))

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
        self.cands.clear()
        if g.candidates[:3]:
            self.cands.add(ttk.Label(self.cands, text=tr("Maybe:", "يمكن:"), style="Muted.TLabel"))
        for cname, score in g.candidates[:3]:
            self.cands.add(ttk.Button(self.cands, text=f"{cname} ({score:.2f})",
                                      command=lambda n=cname: (self.name_var.set(n), self.name_changed())))
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
        self.app.notify(self.app.lib.isolate([g.person_id]))
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
        self.zoom = float(self.root.tk.call("tk", "scaling")) / (96 / 72)
        self.errors = ErrorGate()
        self.root.report_callback_exception = self.on_error
        try:
            self.settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.settings = {}
        st = self.settings
        set_language(os.environ.get("FACE_SORTER_LANGUAGE") or st.get("language", "ar"))
        theme(self.root)
        self.root.configure(background=C["bg"])
        try:
            self.icon = app_icon(64)
            self.root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        fit_window(self.root, 1320, 880)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.minsize(min(860, sw - 40), min(560, sh - 90))
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
        self.settings_shown = bool(st.get("settings_shown", False))
        self.started = 0.0
        self.stage = (0, 0, 0)
        self.stage_start = (0, 0.0, 0)
        self.paused_at = 0.0
        self.ui: tk.Frame | None = None
        self.page = "scan"
        self.who_paths: list[Path] = []
        self.find_results: list = []
        self.find_job = 0
        self.build_ui()
        self.root.bind("<Configure>", lambda e: e.widget is self.root and (self.fit_sidebar(), self.fit_height()),
                       add="+")
        for k, key in enumerate(PAGES, 1):  # Ctrl+1 … Ctrl+5: the pages of the side menu
            self.root.bind(f"<Control-Key-{k}>", lambda e, key=key: self.show_page(key))
        self.root.after(100, self.pump)

    # ================================================================ frame of the window
    def build_ui(self) -> None:
        """Make (or remake, after changing the language) everything inside the window."""
        self.root.title(f"Face Sorter v{__version__} — " + tr("face library", "مكتبة الوشوش"))
        self.ui = tk.Frame(self.root, background=C["bg"])
        self.ui.pack(fill="both", expand=True)
        self._build_sidebar()
        main = ttk.Frame(self.ui, style="Page.TFrame")
        main.pack(side=start(), fill="both", expand=True)
        self._build_header(main)
        self._build_status(main)
        self.content = ttk.Frame(main, style="Page.TFrame")
        self.content.pack(fill="both", expand=True, padx=18, pady=(14, 10))
        self.pages: dict[str, ttk.Frame] = {}
        self.subtitles: list[WrapLabel] = []
        self.short = False
        for key in PAGES:
            self.pages[key] = ttk.Frame(self.content, style="Page.TFrame")
        self._build_scan(self.pages["scan"])
        self._build_who(self.pages["who"])
        self._build_find(self.pages["find"])
        self._build_people(self.pages["people"])
        self._build_library_page(self.pages["library"])
        self.show_page(self.page if self.page in PAGES else "scan")
        self.refresh_all()
        self.fit_sidebar()
        self.fit_height()

    def _build_sidebar(self) -> None:
        sb = tk.Frame(self.ui, background=C["nav"])
        sb.pack(side=start(), fill="y")
        self.sidebar = sb
        brand = tk.Frame(sb, background=C["nav"])
        brand.pack(fill="x", padx=14, pady=(18, 22))
        self.brand_icon = tk.Label(brand, text="◉", background=C["nav"], foreground="#60A5FA", font=Fonts.big)
        self.brand_icon.pack(side=start())
        self.brand_text = tk.Frame(brand, background=C["nav"])
        self.brand_text.pack(side=start(), padx=8)
        tk.Label(self.brand_text, text="Face Sorter", background=C["nav"], foreground="#FFFFFF",
                 font=Fonts.head).pack(anchor=anchor())
        tk.Label(self.brand_text, text=tr("face library", "مكتبة الوشوش"), background=C["nav"],
                 foreground=C["nav_text"], font=Fonts.small).pack(anchor=anchor())
        self.nav: dict[str, tuple] = {}
        for key, (icon, text) in page_names().items():
            item = tk.Frame(sb, background=C["nav"], cursor="hand2")
            item.pack(fill="x", pady=1)
            bar = tk.Frame(item, background=C["nav"], width=4)
            bar.pack(side=start(), fill="y")
            ic = tk.Label(item, text=icon, background=C["nav"], foreground="#FFFFFF", font=Fonts.icon, width=2)
            ic.pack(side=start(), padx=(12, 6) if not rtl() else (6, 12), pady=9)
            tx = tk.Label(item, text=text, background=C["nav"], foreground=C["nav_text"], font=Fonts.nav,
                          anchor=anchor())
            tx.pack(side=start(), fill="x", expand=True, padx=(0, 18) if not rtl() else (18, 0))
            for w in (item, ic, tx):
                w.bind("<Button-1>", lambda e, k=key: self.show_page(k))
                w.bind("<Enter>", lambda e, k=key: self.nav_paint(k, hover=True))
                w.bind("<Leave>", lambda e, k=key: self.nav_paint(k))
            n = list(page_names()).index(key) + 1
            Tip(item, lambda t=text, n=n: f"{t}   (Ctrl+{n})" if self.compact else f"Ctrl+{n}")
            self.nav[key] = (item, bar, ic, tx)
        self.version_label = tk.Label(sb, text=f"v{__version__}", background=C["nav"], foreground="#64748B",
                                      font=Fonts.small)
        self.version_label.pack(side="bottom", pady=10)
        self.compact = False

    def nav_paint(self, key: str, hover: bool = False) -> None:
        item, bar, ic, tx = self.nav[key]
        active = key == self.page
        bg = C["nav_active"] if active else (C["nav_hover"] if hover else C["nav"])
        for w in (item, ic, tx):
            w.configure(background=bg)
        bar.configure(background="#60A5FA" if active else bg)
        tx.configure(foreground="#FFFFFF" if active else C["nav_text"],
                     font=Fonts.bold if active else Fonts.nav)

    def fit_sidebar(self) -> None:
        """Narrow window: the side menu shows icons only."""
        if not self.ui or not self.ui.winfo_exists():
            return
        compact = self.root.winfo_width() < int(1180 * self.zoom)
        if compact == self.compact:
            return
        self.compact = compact
        for item, bar, ic, tx in self.nav.values():
            if compact:
                tx.pack_forget()
            else:
                tx.pack(side=start(), fill="x", expand=True, padx=(0, 18) if not rtl() else (18, 0))
        if compact:
            self.brand_text.pack_forget()
        else:
            self.brand_text.pack(side=start(), padx=8)

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        for k, f in self.pages.items():
            if k != key:
                f.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.page = key
        for k in self.nav:
            self.nav_paint(k)
        if key == "people":
            self.refresh_people()
        elif key == "find":
            self.refresh_find_people()
        elif key == "library":
            self.refresh_library_page()

    def page_head(self, parent, title: str, subtitle: str) -> ttk.Frame:
        f = ttk.Frame(parent, style="Page.TFrame")
        f.pack(fill="x", pady=(0, 10))
        ttk.Label(f, text=title, style="PageTitle.TLabel").pack(anchor=anchor())
        sub = WrapLabel(f, text=subtitle, style="PageSub.TLabel")
        sub.pack(fill="x", pady=(2, 0))
        self.subtitles.append(sub)
        return f

    def fit_height(self) -> None:
        """A short window (small screen, big zoom): the pages drop their one-line explanations."""
        short = self.root.winfo_height() < int(740 * self.zoom)
        if short == self.short:
            return
        self.short = short
        for sub in self.subtitles:
            if sub.winfo_exists():
                if short:
                    sub.pack_forget()
                else:
                    sub.pack(fill="x", pady=(2, 0))

    def _build_header(self, main) -> None:
        hdr = tk.Frame(main, background=C["card"])
        hdr.pack(fill="x")
        tk.Frame(main, background=C["border"], height=1).pack(fill="x")
        inner = ttk.Frame(hdr, padding=(18, 12, 18, 10))
        inner.pack(fill="x")
        btns = ttk.Frame(inner)
        btns.pack(side=end(), anchor="n")
        other = "en" if language() == "ar" else "ar"
        self.lang_btn = ttk.Button(btns, text="🌐 " + LANGUAGES[other], command=self.change_language)
        self.lang_btn.pack(side=start(), padx=3)
        self.manage_btn = ttk.Button(btns, text=tr("⚙ Manage", "⚙ إدارة المكتبة"),
                                     command=lambda: self.show_page("library"))
        self.manage_btn.pack(side=start(), padx=3)
        text = ttk.Frame(inner)
        text.pack(side=start(), fill="x", expand=True)
        row = ttk.Frame(text)
        row.pack(fill="x")
        ttk.Label(row, text="📚", font=Fonts.title).pack(side=start(), anchor="n")
        self.card_name = WrapLabel(row, font=Fonts.head)
        self.card_name.pack(side=start(), fill="x", expand=True, padx=8, pady=(4, 0))
        self.card_path = ttk.Label(text, style="Muted.TLabel", anchor=anchor())
        self.card_path.pack(fill="x", pady=(2, 4))
        self.card_path.bind("<Configure>", lambda e: self.show_card_path())
        Tip(self.card_path, lambda: str(self.lib.path) if self.lib else "")
        self.chips = Flow(text, padx=3, pady=1)
        self.chips.pack(fill="x")
        if TkinterDnD:  # drop a library file on the top bar to open or merge it
            for w in (hdr, inner, self.card_name, self.card_path):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: self.drop_library(e.data))

    def _build_status(self, main) -> None:
        bar = tk.Frame(main, background=C["card"])
        bar.pack(side="bottom", fill="x")
        tk.Frame(main, background=C["border"], height=1).pack(side="bottom", fill="x")
        self.status_icon = tk.Label(bar, text="●", background=C["card"], foreground=C["ok"], font=Fonts.small)
        self.status_icon.pack(side=start(), padx=(14, 4) if not rtl() else (4, 14), pady=5)
        self.status = tk.Label(bar, text=tr("Ready.", "جاهز."), background=C["card"], foreground=C["muted"],
                               font=Fonts.small, anchor=anchor(), justify=justify())
        self.status.pack(side=start(), fill="x", expand=True)
        self.status.bind("<Configure>", lambda e: self.status.configure(wraplength=max(100, e.width - 10)))
        self.status_job = None

    def notify(self, text: str, kind: str = "ok") -> None:
        """Show the result of an action at the bottom of the window (and in the scan log)."""
        if not text:
            return
        if hasattr(self, "logbox") and self.logbox.winfo_exists():
            self._append(text)
        color = {"ok": C["ok"], "warn": C["warn"], "error": C["danger"], "info": C["accent"]}.get(kind, C["ok"])
        self.status_icon.configure(foreground=color)
        self.status.configure(text=text.replace("\n", "  ·  "), foreground=C["text"])
        if self.status_job:
            self.root.after_cancel(self.status_job)
        self.status_job = self.root.after(8000, lambda: self.status.winfo_exists() and
                                          self.status.configure(foreground=C["muted"]))

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
        self.compact = False
        self.build_ui()

    # ============================================================ page: this computer
    def _build_scan(self, page) -> None:
        actions = Flow(page, padx=4, style="Page.TFrame")
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.who_btn = ttk.Button(actions, text=tr("🔍 Who is this?", "🔍 مين ده؟"),
                                  command=lambda: self.show_page("who"))
        self.resume_btn = ttk.Button(actions, text=tr("Continue review", "كمّل المراجعة"), command=self.resume_review)
        self.start_btn = ttk.Button(actions, text=tr("▶  Start", "▶  ابدأ"), style="Primary.TButton",
                                    command=self.start)
        self.pause_btn = ttk.Button(actions, text=tr("⏸ Pause", "⏸ إيقاف مؤقت"), state="disabled",
                                    command=self.toggle_pause)
        self.stop_btn = ttk.Button(actions, text=tr("■ Stop", "■ وقّف"), state="disabled",
                                   command=self.throttle.stop.set)
        actions.add(self.who_btn)
        actions.add(self.resume_btn)
        for b in (self.start_btn, self.stop_btn, self.pause_btn):
            actions.add(b, end=True)
        self.actions = actions

        sf = ScrollFrame(page)
        sf.pack(fill="both", expand=True)
        self.scan_scroll = sf
        f = sf.inner
        self.page_head(f, tr("Sort the photos on this computer", "رتّب صور الجهاز ده"), tr(
            "Choose the photos folder: the program finds the faces, recognises the people it knows, and groups "
            "the new ones so you can name them.",
            "اختار فولدر الصور: البرنامج هيلاقي الوشوش، ويعرّفك على الناس اللي يعرفهم، ويجمّع الجداد عشان تسمّيهم."))

        src = Card(f, tr("1   The photos", "1   الصور"))
        src.pack(fill="x", pady=(0, 10))
        self.device_chip = chip(src.head, "")
        src.head_add(self.device_chip)
        row = ttk.Frame(src.body)
        row.pack(fill="x")
        ttk.Button(row, text=tr("Choose folder…", "اختار فولدر…"),
                   command=lambda: self.pick(self.src_var)).pack(side=end(), padx=(8, 0) if not rtl() else (0, 8))
        src_entry = ttk.Entry(row, textvariable=self.src_var)
        src_entry.pack(side=start(), fill="x", expand=True)
        ttk.Label(src.body, style="Muted.TLabel", text=tr(
            "Tip: you can also drag a folder from Windows and drop it here.",
            "ممكن كمان تسحب فولدر من الويندوز وتفلته هنا.")).pack(anchor=anchor(), pady=(6, 0))
        if TkinterDnD:  # drop a folder of photos here
            for w in (src, src.body, src_entry):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: self.drop_folder(e.data))

        mode = Card(f, tr("2   What to do", "2   هتعمل إيه؟"))
        mode.pack(fill="x", pady=(0, 10))
        modes = Flow(mode.body, padx=8)
        modes.pack(fill="x")
        for m in MODES:
            modes.add(ttk.Radiobutton(modes, text=mode_label(m), value=m, variable=self.mode_var,
                                      command=self.mode_changed))
        self.mode_help = WrapLabel(mode.body, style="Muted.TLabel")
        self.mode_help.pack(fill="x", pady=(4, 0))
        self.dest_row = ttk.Frame(mode.body)
        ttk.Label(self.dest_row, text=tr("Put the sorted photos in:", "حط الصور المرتبة في:"),
                  style="Bold.TLabel").pack(side=start())
        self.dest_btn = ttk.Button(self.dest_row, text=tr("Choose…", "اختار…"), command=lambda: self.pick(self.dest_var))
        self.dest_btn.pack(side=end(), padx=(8, 0) if not rtl() else (0, 8))
        self.dest_entry = ttk.Entry(self.dest_row, textvariable=self.dest_var)
        self.dest_entry.pack(side=start(), fill="x", expand=True, padx=8)

        fold = ttk.Frame(f, style="Page.TFrame")
        fold.pack(fill="x", pady=(0, 6))
        self.settings_btn = ttk.Button(fold, style="PageLink.TButton",
                                       command=lambda: self.show_settings(not self.settings_shown, True))
        self.settings_btn.pack(side=start())
        self.settings_summary = WrapLabel(fold, style="PageSub.TLabel")
        self.settings_summary.pack(side=start(), fill="x", expand=True, padx=8)
        self.settings_head = fold
        adv = Card(f)
        self.settings_box = adv

        def row(title: str) -> Flow:
            r = Flow(adv.body, padx=5, pady=3)
            r.pack(fill="x")
            r.add(ttk.Label(r, text=title, style="Bold.TLabel"))
            return r
        filt = row(tr("Skip:", "تجاهل:"))
        filt.add(ttk.Checkbutton(filt, text=tr("Screenshots", "سكرين شوت"), variable=self.f_shots))
        filt.add(ttk.Checkbutton(filt, text=tr("Stickers", "ستيكرات"), variable=self.f_stickers))
        filt.add(ttk.Checkbutton(filt, text=tr("Small pictures (<300px)", "صور صغيرة (أقل من 300 نقطة)"),
                                 variable=self.f_small))
        filt.add(ttk.Label(filt, text=tr("Folders (comma-separated):", "فولدرات (بينهم فاصلة):")))
        filt.add(ttk.Entry(filt, textvariable=self.f_exclude, width=24))
        opts = row(tr("Recognition:", "التعرف:"))
        opts.add(ttk.Label(opts, text=tr("Strictness", "الدقة")))
        s_label = ttk.Label(opts, width=3, style="Bold.TLabel")

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
        self.battery_label = ttk.Label(speed, style="Warn.TLabel")
        speed.add(self.battery_label)

        prog = Card(f, tr("Progress", "التقدم"))
        prog.pack(fill="x", pady=(4, 0))
        self.progress_card = prog
        line = ttk.Frame(prog.body)
        line.pack(fill="x")
        self.progress_label = ttk.Label(line, style="Bold.TLabel", text=tr("Not started yet.", "لسه مبدأش."))
        self.progress_label.pack(side=start())
        self.time_label = ttk.Label(line, style="Muted.TLabel")
        self.time_label.pack(side=end())
        self.bar = ttk.Progressbar(prog.body, maximum=100)
        self.bar.pack(fill="x", pady=(8, 8))
        self.logbox = style_tk(tk.Text(prog.body, height=7, state="disabled", wrap="word", font=Fonts.small,
                                       padx=8, pady=6))
        self.logbox.tag_configure("line", justify=justify())
        self.logbox.tag_configure("hint", justify=justify(), foreground=C["faint"])
        self.logbox.pack(fill="x")
        self.logbox.config(state="normal")
        self.logbox.insert("end", tr("The steps of the work appear here: how many photos, how many faces, "
                                     "and what was saved.",
                                     "هنا هتظهر خطوات الشغل: كام صورة، وكام وش، واللي اتحفظ."), "hint")
        self.logbox.config(state="disabled")
        for var in (self.mode_var, self.speed_var, self.strict_var):
            var.trace_add("write", lambda *_: self.update_settings_summary())
        self.show_settings(self.settings_shown, False)
        self.mode_changed()

    def show_settings(self, shown: bool, by_user: bool) -> None:
        """The advanced settings can be folded away."""
        self.settings_shown = shown
        if by_user:
            self.settings["settings_shown"] = shown
            self.remember()
        if shown:
            self.settings_box.pack(fill="x", pady=(0, 10), after=self.settings_head)
        else:
            self.settings_box.pack_forget()
        self.settings_btn.config(text=("▾ " if shown else ("◂ " if rtl() else "▸ ")) +
                                 tr("⚙ Advanced settings", "⚙ إعدادات متقدمة"))
        self.update_settings_summary()

    def update_settings_summary(self) -> None:
        if not hasattr(self, "settings_summary") or not self.settings_summary.winfo_exists():
            return
        text = "" if self.settings_shown else (
            speed_label(self.speed_var.get()) + "  ·  " + tr("strictness ", "الدقة ") + str(self.strict_var.get()))
        self.settings_summary.config(text=text)

    def mode_changed(self) -> None:
        m = self.mode_var.get()
        self.mode_help.config(text=mode_help(m))
        if m == "learn":
            self.dest_row.pack_forget()
        else:
            self.dest_row.pack(fill="x", pady=(8, 0))
        self.update_settings_summary()

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
        self.root.after(50, lambda: self.scan_scroll.canvas.yview_moveto(1.0))  # show the progress
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

    # ============================================================ page: who is this?
    def _build_who(self, page) -> None:
        self.page_head(page, tr("Who is this?", "مين ده؟"), tr(
            "Drop one or more photos to see who is in them. Nothing changes in the library unless you press Save.",
            "اسحب صورة أو أكتر وافلتها، وهتعرف مين فيها. المكتبة مش بتتغير غير لما تدوس احفظ."))
        drop = tk.Canvas(page, height=84, background=C["drop"], highlightthickness=0, cursor="hand2")
        drop.pack(fill="x")
        hint = tr("🖼   Drop photos here, or click to choose", "🖼   اسحب الصور وافلتها هنا، أو دوس عشان تختار") \
            if TkinterDnD else tr("🖼   Click to choose photos", "🖼   دوس عشان تختار الصور")

        def paint(e=None):
            drop.delete("all")
            w, h = drop.winfo_width(), drop.winfo_height()
            drop.create_rectangle(3, 3, w - 3, h - 3, outline="#93B4F0", width=2, dash=(6, 4))
            drop.create_text(w // 2, h // 2, text=hint, font=Fonts.head, fill=C["accent"])
        drop.bind("<Configure>", paint)
        drop.bind("<Button-1>", lambda e: self.who_choose())
        if TkinterDnD:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", lambda e: self.who_add(dropped_paths(self.root, e.data)))
        bar = Flow(page, padx=3, style="Page.TFrame")
        bar.pack(fill="x", pady=8)
        bar.add(ttk.Button(bar, text=tr("Choose photos…", "اختار صور…"), command=self.who_choose))
        bar.add(ttk.Button(bar, text=tr("Clear the list", "امسح القائمة"), command=self.who_clear))
        body = ttk.PanedWindow(page, orient="horizontal")
        body.pack(fill="both", expand=True)
        lst = Card(body, tr("Photos", "الصور"), padding=(10, 10))
        self.who_list = style_tk(tk.Listbox(lst.body, width=16, justify=justify(), activestyle="none"))
        self.who_list.pack(fill="both", expand=True)
        self.who_list.bind("<<ListboxSelect>>", lambda e: self.who_show())
        pic = Card(body, padding=(8, 8))
        self.who_canvas = tk.Canvas(pic.body, background=C["grid"], highlightthickness=0, width=240)
        self.who_canvas.pack(fill="both", expand=True)
        self.who_canvas.bind("<Configure>", lambda e: self.who_draw())
        faces = Card(body, tr("Faces", "الوشوش"), padding=(10, 10))
        panes = [(lst, 1), (pic, 3), (faces, 2)]
        for pane, weight in (reversed(panes) if rtl() else panes):
            body.add(pane, weight=weight)
        holder = tk.Canvas(faces.body, highlightthickness=0, width=280, background=C["card"])
        sb = ttk.Scrollbar(faces.body, orient="vertical", command=holder.yview)
        self.who_faces = ttk.Frame(holder)
        self.who_faces.bind("<Configure>", lambda e: holder.configure(scrollregion=holder.bbox("all")))
        win_id = holder.create_window((0, 0), window=self.who_faces, anchor="nw")
        holder.bind("<Configure>", lambda e: holder.itemconfigure(win_id, width=e.width))
        holder.configure(yscrollcommand=sb.set)
        sb.pack(side=end(), fill="y")
        holder.pack(fill="both", expand=True)
        self.who_result = None   # (path, img, results)
        self.who_images: list = []
        for p in self.who_paths:
            self.who_list.insert("end", p.name)
        self.who_empty()

    def who_empty(self) -> None:
        for w in self.who_faces.winfo_children():
            w.destroy()
        WrapLabel(self.who_faces, style="Muted.TLabel", text=tr(
            "Choose a photo: every face in it appears here with its name, and you can save a face for a name.",
            "اختار صورة: كل وش فيها هيظهر هنا باسمه، وتقدر تحفظ أي وش لاسم.")).pack(fill="x", pady=8)

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
            self.show_page("who")
            self.who_list.selection_clear(0, "end")
            self.who_list.selection_set(len(self.who_paths) - len(new))
            self.who_show()

    def who_clear(self) -> None:
        self.who_paths.clear()
        self.who_list.delete(0, "end")
        self.who_result = None
        self.who_canvas.delete("all")
        self.who_empty()

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
        self._who_photo = photo_from_array(img, int(max(iw, ih) * scale), square=False)
        sw, sh = self._who_photo.width(), self._who_photo.height()
        ox, oy = (w - sw) // 2, (h - sh) // 2
        c.create_image(ox, oy, image=self._who_photo, anchor="nw")
        for k, r in enumerate(results):
            x, y, bw, bh = r["box"]
            x0, y0, x1, y1 = ox + x * scale, oy + y * scale, ox + (x + bw) * scale, oy + (y + bh) * scale
            color = VERDICT_COLORS[r["verdict"]]
            c.create_rectangle(x0, y0, x1, y1, outline=color, width=3)
            label = f"{k + 1}. " + (r["matches"][0][0] if r["matches"] and r["verdict"] != "unknown" else "?")
            t = c.create_text(x0 + 6, y0 - 12, text=label, anchor="w", fill="#FFFFFF", font=Fonts.bold)
            bx = c.bbox(t)
            c.tag_lower(c.create_rectangle(bx[0] - 5, bx[1] - 2, bx[2] + 5, bx[3] + 2, fill=color, outline=color), t)

    def who_cards(self) -> None:
        for w in self.who_faces.winfo_children():
            w.destroy()
        self.who_images = []
        _, _, results = self.who_result
        if not results:
            WrapLabel(self.who_faces, text=tr("No faces found in this photo.", "مفيش وشوش في الصورة دي."),
                      style="Muted.TLabel").pack(fill="x", pady=8)
            return
        names = self.lib.names()
        for k, r in enumerate(results):
            card = tk.Frame(self.who_faces, background=C["card"], highlightthickness=1,
                            highlightbackground=C["border"])
            card.pack(fill="x", pady=4, padx=2)
            img = round_thumb(r["crop"], 64)
            self.who_images.append(img)
            ttk.Label(card, image=img).pack(side=start(), anchor="n", padx=8, pady=8)
            inner = ttk.Frame(card)
            inner.pack(side=start(), fill="x", expand=True, padx=(0, 8) if not rtl() else (8, 0), pady=6)
            mark = {"sure": "✅", "maybe": "❓", "unknown": "❔"}[r["verdict"]]
            if r["matches"] and r["verdict"] != "unknown":
                title = f"{k + 1}. {mark} {r['matches'][0][0]}"
                text = tr(f"match {r['matches'][0][1]:.2f}", f"التشابه {r['matches'][0][1]:.2f}")
                if len(r["matches"]) > 1:
                    text += "\n" + tr("or: ", "أو: ") + ", ".join(f"{n} ({sc:.2f})" for n, sc in r["matches"][1:3])
            else:
                title = f"{k + 1}. {mark} " + tr("Unknown", "مش معروف")
                text = (tr("Seen before: ", "اتشاف قبل كده: ") + str(r["seen_before"])) if r["seen_before"] else ""
            if not r["good"]:
                text += ("\n" if text else "") + tr("(low quality)", "(مش واضح)")
            WrapLabel(inner, text=title, style="Bold.TLabel").pack(fill="x")
            if text:
                WrapLabel(inner, text=text, style="Muted.TLabel").pack(fill="x")
            row = Flow(inner, padx=2)
            row.pack(fill="x", pady=2)
            var = tk.StringVar(value=r["matches"][0][0] if r["matches"] and r["verdict"] != "unknown" else "")
            status = WrapLabel(inner, style="Ok.TLabel")

            def save(r=r, var=var, status=status):
                msg = self.lib.save_face(var.get(), r["face"])
                if msg:
                    status.config(text="✓ " + msg)
                    self.notify(msg)
                    self.refresh_card()
            row.add(ttk.Combobox(row, textvariable=var, values=names, width=12, justify=justify()))
            row.add(ttk.Button(row, text=tr("Save", "احفظ"), command=save))
            status.pack(fill="x")

    # ============================================================ page: find
    def _build_find(self, page) -> None:
        self.page_head(page, tr("Find people in the photos on this computer", "دوّر على ناس في صور الجهاز ده"), tr(
            "Choose one person or more: you get every photo on this computer with all of them in it. It uses what "
            "was found when the photos were scanned, so scan a folder first.",
            "اختار شخص أو أكتر، وهيطلعلك كل صورة على الجهاز ده هما فيها مع بعض. البحث بيستخدم نتايج فحص الصور، "
            "فلازم الفولدر يكون اتفحص قبل كده."))
        body = ttk.PanedWindow(page, orient="horizontal")
        body.pack(fill="both", expand=True)
        who = Card(body, tr("Who?", "مين؟"), padding=(12, 10))
        res = Card(body, tr("Photos", "الصور"), padding=(12, 10))
        for pane, weight in ((res, 3), (who, 1)) if rtl() else ((who, 1), (res, 3)):
            body.add(pane, weight=weight)
        self.find_filter = tk.StringVar()
        SearchBox(who.body, self.find_filter, tr("Search names", "دوّر بالاسم")).pack(fill="x", pady=(0, 8))
        self.find_filter.trace_add("write", lambda *_: self.refresh_find_people())
        self.find_go = ttk.Button(who.body, text=tr("🔎  Find", "🔎  دوّر"), style="Primary.TButton",
                                  command=self.find_start)
        self.find_go.pack(side="bottom", fill="x", pady=(8, 0))
        self.find_picked = WrapLabel(who.body, style="Bold.TLabel")
        self.find_picked.pack(side="bottom", fill="x", pady=(6, 0))
        self.find_tree = ttk.Treeview(who.body, show="tree", style="People.Treeview", selectmode="extended")
        self.find_tree.pack(fill="both", expand=True)
        self.find_tree.bind("<<TreeviewSelect>>", lambda e: self.find_selection_changed())
        self.find_tree.bind("<Double-Button-1>", lambda e: self.find_start())
        self.find_rows: dict[str, int] = {}
        self.find_imgs: list = []
        top = Flow(res.body, padx=3)
        top.pack(fill="x")
        self.find_summary = ttk.Label(top, style="Bold.TLabel", text="")
        top.add(self.find_summary)
        self.find_open = ttk.Button(top, text=tr("Open photo", "افتح الصورة"), command=self.find_open_photo)
        self.find_folder = ttk.Button(top, text=tr("Open its folder", "افتح فولدرها"), command=self.find_open_folder)
        self.find_copy = ttk.Button(top, text=tr("Copy all to a folder…", "انسخهم كلهم لفولدر…"),
                                    command=self.find_copy_all)
        for b in (self.find_copy, self.find_folder, self.find_open):
            top.add(b, end=True)
        self.find_bar = ttk.Progressbar(res.body, maximum=100)
        self.find_grid = Grid(res.body, size=150, on_click=self.find_click, on_double=lambda k: self.find_open_photo(k),
                              empty=tr("Choose people on the side, then press Find.",
                                       "اختار ناس من الجنب، وبعدين دوس دوّر."))
        self.find_grid.pack(fill="both", expand=True, pady=(8, 0))
        self.find_selected = -1
        self.find_buttons_state()

    def refresh_find_people(self) -> None:
        if not hasattr(self, "find_tree") or not self.find_tree.winfo_exists():
            return
        keep = set(self.find_selected_pids())
        text = self.find_filter.get().strip().lower()
        self.find_tree.delete(*self.find_tree.get_children())
        self.find_rows.clear()
        self.find_imgs.clear()
        people = self.lib.people("known")
        for p in people:
            if text and text not in (p.name or "").lower():
                continue
            img = round_thumb(self.lib.thumb(p.id), 36)
            self.find_imgs.append(img)
            item = self.find_tree.insert("", "end", text=f"  {p.name}", image=img)
            self.find_rows[item] = p.id
            if p.id in keep:
                self.find_tree.selection_add(item)
        if not people:
            self.find_picked.config(text=tr("No named people yet: name people in a review or on the People page.",
                                            "لسه مفيش ناس متسمّيين: سمّيهم في المراجعة أو في صفحة الناس."))
            self.find_go.config(state="disabled")
        else:
            self.find_selection_changed()

    def find_selected_pids(self) -> list[int]:
        return [self.find_rows[i] for i in self.find_tree.selection() if i in self.find_rows]

    def find_selection_changed(self) -> None:
        if not hasattr(self, "find_tree") or not self.find_tree.winfo_exists():
            return
        names = [self.find_tree.item(i, "text").strip() for i in self.find_tree.selection()]
        self.find_picked.config(text=(tr("Together: ", "مع بعض: ") + "، ".join(names)) if names else
                                tr("Choose one person or more (Ctrl+click for more).",
                                   "اختار شخص أو أكتر (Ctrl مع الضغط لأكتر من واحد)."))
        self.find_go.config(state="normal" if names and not self.busy else "disabled")

    def find_start(self) -> None:
        pids = self.find_selected_pids()
        if not pids or self.busy:
            return
        self.find_job += 1
        job = self.find_job
        self.find_results = []
        self.find_selected = -1
        self.find_grid.empty = tr("Searching…", "بدوّر…")
        self.find_grid.set_items([])
        self.find_summary.config(text=tr("Searching…", "بدوّر…"))
        self.find_bar.pack(fill="x", pady=(6, 0), before=self.find_grid)
        self.find_bar["value"] = 0
        self.find_go.config(state="disabled")
        lib, strict = self.lib, self.strict_var.get()

        def work():
            try:
                found = find_people(lib, pids, strict)
                self.msgs.put(("find_found", (job, found)))
                thumbs = []
                for k, f in enumerate(found[:FIND_LIMIT]):
                    thumbs.append(photo_thumb(f.path, 150))
                    if k % 12 == 0:
                        self.msgs.put(("find_progress", (job, k + 1, min(len(found), FIND_LIMIT))))
                self.msgs.put(("find_done", (job, found, thumbs)))
            except Exception as e:
                log_error("find", traceback.format_exc())
                self.msgs.put(("find_done", (job, [], [], str(e))))
        threading.Thread(target=work, daemon=True).start()

    def find_show(self, found, thumbs) -> None:
        self.find_results = found
        self.find_bar.pack_forget()
        sure = sum(1 for f in found if f.sure)
        if not found:
            self.find_summary.config(text=tr("No photos found.", "ملقتش صور."))
            self.find_grid.empty = tr(
                "No photos on this computer have all these people in them. Only scanned folders are searched: if "
                "the photos are in a folder that was not scanned yet, scan it from 'This computer' first.",
                "مفيش صور على الجهاز ده فيها كل الناس دول. البحث في الفولدرات اللي اتفحصت بس: لو الصور في فولدر "
                "لسه متفحصش، افحصه الأول من «الجهاز ده».")
        else:
            more = tr(f" (showing the first {FIND_LIMIT})", f" (بيعرض أول {FIND_LIMIT})") \
                if len(found) > FIND_LIMIT else ""
            self.find_summary.config(text=tr(f"{len(found)} photos · {sure} sure · {len(found) - sure} maybe",
                                             f"{len(found)} صورة · {sure} متأكد · {len(found) - sure} يمكن") + more)
        items, headers = [], {}
        order = sorted(range(len(thumbs)), key=lambda k: not found[k].sure)
        last = None
        for k in order:
            f = found[k]
            group = (f.sure, (f.date or "")[:4])
            if group != last:
                year = (f.date or "")[:4] or tr("no date", "من غير تاريخ")
                headers[len(items)] = (tr("✅ Sure", "✅ متأكد") if f.sure else tr("❓ Maybe", "❓ يمكن")) + f"  ·  {year}"
                last = group
            items.append({"key": f"f{self.find_job}_{k}", "img": thumbs[k], "on": None, "idx": k,
                          "caption": f.date or ""})
        self.find_grid.set_items(items, headers)
        self.find_buttons_state()
        self.find_selection_changed()

    def find_click(self, k: int) -> None:
        for it in self.find_grid.items:
            it["on"] = None
        self.find_grid.items[k]["on"] = True
        self.find_selected = self.find_grid.items[k]["idx"]
        self.find_grid.draw()
        self.find_buttons_state()
        self.notify(str(self.find_results[self.find_selected].path), "info")

    def find_buttons_state(self) -> None:
        one = "normal" if self.find_selected >= 0 else "disabled"
        self.find_open.config(state=one)
        self.find_folder.config(state=one)
        self.find_copy.config(state="normal" if self.find_results else "disabled")

    def find_open_photo(self, k: int | None = None) -> None:
        if k is not None:
            self.find_click(k)
        if self.find_selected >= 0:
            open_path(self.find_results[self.find_selected].path)

    def find_open_folder(self) -> None:
        if self.find_selected >= 0:
            open_path(self.find_results[self.find_selected].path.parent)

    def find_copy_all(self) -> None:
        if not self.find_results:
            return
        d = filedialog.askdirectory(title=tr("Copy the photos to", "تنسخ الصور فين"))
        if not d:
            return
        n = 0
        for f in self.find_results:
            try:
                copy_file(f.path, Path(d))
                n += 1
            except OSError:
                pass
        self.notify(tr(f"Copied {n} photos to {d}", f"اتنسخت {n} صورة في {d}"))

    # ============================================================ page: people
    def _build_people(self, page) -> None:
        self.page_head(page, tr("People", "الناس"), tr(
            "Everyone the library knows. Select people to name, join, isolate, delete or export them; "
            "click faces to correct them.",
            "كل اللي المكتبة تعرفهم. حدد ناس عشان تسمّيهم أو تدمجهم أو تعزلهم أو تمسحهم أو تصدّرهم، "
            "ودوس على الوشوش عشان تصلّحها."))
        tools = Flow(page, padx=3, style="Page.TFrame")
        tools.pack(fill="x", pady=(0, 8))
        self.lib_stats = ttk.Label(tools, style="Page.TLabel", font=Fonts.bold)
        tools.add(self.lib_stats)
        self.lib_buttons = []
        for text, cmd in ((tr("↻ Refresh", "↻ حدّث"), self.refresh_people),
                          (tr("Reset learning", "امسح التعلم التلقائي"), self.reset_learning),
                          (tr("Possible duplicates", "يمكن متكررين"), self.open_duplicates)):
            b = ttk.Button(tools, text=text, command=cmd)
            tools.add(b, end=True)
            self.lib_buttons.append(b)
        body = ttk.PanedWindow(page, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = Card(body, padding=(12, 10))
        right = Card(body, padding=(16, 12))
        for pane, weight in ((right, 5), (left, 2)) if rtl() else ((left, 2), (right, 5)):
            body.add(pane, weight=weight)

        self.people_filter = tk.StringVar()
        SearchBox(left.body, self.people_filter, tr("Search", "دوّر بالاسم")).pack(fill="x", pady=(0, 8))
        self.people_filter.trace_add("write", lambda *_: self.refresh_people())
        pbar = Flow(left.body, padx=2)
        pbar.pack(side="bottom", fill="x", pady=(8, 0))
        b = ttk.Button(pbar, text=tr("✎ Name", "✎ سمّي"), command=self.rename_person)
        pbar.add(b)
        self.lib_buttons.append(b)
        b = menu_button(pbar, tr("With selected", "مع المحددين"), [
            (tr("Join them into one person…", "ادمجهم في شخص واحد…"), self.join_people),
            (tr("Isolate (back to unknown)", "اعزلهم (يرجعوا للمجهولين)"), self.isolate_people),
            (tr("Export to a new library…", "صدّرهم لمكتبة جديدة…"), self.export_people),
            ("-", None),
            (tr("Delete…", "امسحهم…"), self.delete_person)])
        pbar.add(b)
        self.lib_buttons.append(b)
        Tip(b, lambda: tr("Ctrl or Shift + click to select several people",
                          "Ctrl أو Shift مع الضغط عشان تحدد أكتر من شخص"))
        self.tree = ttk.Treeview(left.body, columns=("n", "seen"), style="People.Treeview", selectmode="extended")
        self.tree.heading("#0", text=tr("Person", "الشخص"), anchor=anchor())
        self.tree.heading("n", text=tr("Faces", "وشوش"))
        self.tree.heading("seen", text=tr("Seen", "اتشاف"))
        self.tree.column("#0", width=190, stretch=True)
        for col, title in (("n", tr("Faces", "وشوش")), ("seen", tr("Seen", "اتشاف"))):
            self.tree.column(col, width=max(56, Fonts.bold.measure(title) + 22), anchor="center", stretch=False)
        for lvl, col in LEVEL_COLORS.items():
            self.tree.tag_configure(lvl, foreground=col)
        self.tree.tag_configure("section", font=Fonts.bold, foreground=C["muted"])
        sb = ttk.Scrollbar(left.body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=end(), fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_person())

        r = right.body
        top = ttk.Frame(r)
        top.pack(fill="x")
        self.p_face = ttk.Label(top)
        self.p_face.pack(side=start(), padx=(0, 12) if not rtl() else (12, 0))
        txt = ttk.Frame(top)
        txt.pack(side=start(), fill="x", expand=True)
        self.p_head = WrapLabel(txt, font=Fonts.title)
        self.p_head.pack(fill="x")
        self.p_level = Flow(txt, padx=2, pady=2)
        self.p_level.pack(fill="x")
        self.p_info = WrapLabel(txt, style="Muted.TLabel")
        self.p_info.pack(fill="x")
        act = Flow(r, padx=2)
        act.pack(fill="x", pady=(8, 4))
        b = ttk.Button(act, text=tr("Not this person", "مش الشخص ده"), command=self.not_this_person)
        act.add(b)
        self.lib_buttons.append(b)
        self.move_var = tk.StringVar()
        self.move_box = ttk.Combobox(act, textvariable=self.move_var, width=18, justify=justify())
        b = ttk.Button(act, text=tr("Move to →", "انقلهم لـ ←"), command=self.move_prints)
        act.add(b, end=True)
        act.add(self.move_box, end=True)
        self.lib_buttons.append(b)
        Tip(act, lambda: tr("Click faces to select them first", "دوس على الوشوش الأول عشان تحددها"))
        self.p_grid = Grid(r, size=88, on_click=self.toggle_print)
        self.p_grid.pack(fill="both", expand=True)
        self.tree_rows: dict[str, int] = {}
        self.tree_images: list = []
        self.p_selected: set[int] = set()
        self._p_img = None

    def refresh_people(self) -> None:
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        lib = self.lib
        st = lib.stats()
        self.lib_stats.config(text=tr(
            f"{st['known']} people · {st['unknown']} unknown · {st['prints']} faces",
            f"{st['known']} شخص · {st['unknown']} مجهول · {st['prints']} وش"))
        keep = self.selected_pid()
        text = self.people_filter.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.tree_rows.clear()
        self.tree_images.clear()
        known = [p for p in lib.people("known") if not text or text in (p.name or "").lower()]
        unknown = [p for p in lib.people("unknown") if not text or text in person_label(p.label).lower()]
        k = self.tree.insert("", "end", text=tr(f" Known ({len(known)})", f" معروفين ({len(known)})"), open=True,
                             tags=("section",))
        u = self.tree.insert("", "end", text=tr(f" Unknown ({len(unknown)})", f" مجهولين ({len(unknown)})"),
                             open=True, tags=("section",))
        frequent = {p.id for p in lib.frequent_unknowns()}
        reselect = None
        for parent, people in ((k, known), (u, unknown)):
            for p in people:
                img = round_thumb(lib.thumb(p.id), 38)
                self.tree_images.append(img)
                label = f"  {person_label(p.label)}" + ("  ⭐" if p.id in frequent else "")
                item = self.tree.insert(parent, "end", text=label, image=img, values=(p.prints, p.seen_count),
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
            self._p_img = None
            self.p_face.config(image="")
            self.p_level.clear()
            if not st["known"] and not st["unknown"]:
                self.p_head.config(text=tr("No people yet", "لسه مفيش ناس"))
                self.p_info.config(text=tr("Scan a folder of photos on 'This computer': the program groups the faces "
                                           "and you name them.",
                                           "افحص فولدر صور من «الجهاز ده»: البرنامج هيجمّع الوشوش وإنت تسمّيهم."))
            else:
                self.p_head.config(text=tr("Choose a person", "اختار شخص"))
                self.p_info.config(text=tr("Their faces, from the oldest photo to the newest, appear here.",
                                           "وشوشه هتظهر هنا، من أقدم صورة لأحدث صورة."))

    def selected_pid(self) -> int | None:
        pids = self.selected_pids()
        return pids[0] if pids else None

    def selected_pids(self) -> list[int]:
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return []
        return [self.tree_rows[i] for i in self.tree.selection() if i in self.tree_rows]

    def show_person(self) -> None:
        pid = self.selected_pid()
        if pid is None:
            return
        p = self.lib.person(pid)
        if p is None:
            return
        self.p_selected = set()
        self._p_img = round_thumb(self.lib.thumb(p.id), 60)
        self.p_face.config(image=self._p_img)
        self.p_head.config(text=person_label(p.label))
        self.p_level.clear()
        if p.kind == "known":
            soft = {"weak": "#FEE2E2", "medium": C["warn_soft"], "strong": C["ok_soft"]}[p.level]
            self.p_level.add(chip(self.p_level, tr("knows them: ", "معرفته بيه: ") + level_text(p.level),
                                  LEVEL_COLORS[p.level], soft))
        else:
            self.p_level.add(chip(self.p_level, tr("no name yet", "لسه من غير اسم"), C["warn"], C["warn_soft"]))
        self.p_level.add(chip(self.p_level, tr(f"{p.prints} faces", f"{p.prints} وش")))
        self.p_level.add(chip(self.p_level, tr(f"{p.looks} look(s)", f"{p.looks} شكل")))
        self.p_level.add(chip(self.p_level, tr(f"seen {p.seen_count}×", f"اتشاف {p.seen_count} مرة")))
        bits = [tr(f"{p.manual} confirmed + {p.auto} learned by itself",
                   f"{p.manual} متأكد منهم + {p.auto} اتعلمهم لوحده"),
                tr("on: ", "على: ") + (", ".join(p.devices) or "-"),
                tr("last seen: ", "آخر مرة: ") + (p.last_seen or "-")]
        if p.kind == "unknown":
            bits.insert(0, tr("Use 'Name' to remember them.", "دوس «سمّي» عشان البرنامج يفتكره."))
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
        self.notify(msg)
        self.refresh_all()
        self.refresh_people()

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
        self.notify(tr(f"A new library with {len(pids)} people: {path} (no password)",
                       f"مكتبة جديدة فيها {len(pids)} شخص: {path} (من غير باسورد)"))

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

    # ============================================================ page: library
    def _build_library_page(self, page) -> None:
        sf = ScrollFrame(page)
        sf.pack(fill="both", expand=True)
        f = sf.inner
        self.page_head(f, tr("The library", "المكتبة"), tr(
            "The library is one file in the 'Library' folder next to the program. It keeps the people and their faces, "
            "not your photos, so it can travel on a USB stick.",
            "المكتبة ملف واحد في فولدر «Library» جنب البرنامج. فيها الناس ووشوشهم، مش صورك، عشان كده تقدر "
            "تتنقل على فلاشة."))
        self.lib_grid = ttk.Frame(f, style="Page.TFrame")
        self.lib_grid.pack(fill="both", expand=True)
        self.card_buttons = []

        file_card = Card(self.lib_grid, tr("📁  The file", "📁  الملف"))
        self.lp_file = WrapLabel(file_card.body)
        self.lp_file.pack(fill="x")
        fb = Flow(file_card.body, padx=3)
        fb.pack(fill="x", pady=(8, 0))
        for text, cmd in ((tr("Open folder", "افتح الفولدر"), lambda: open_path(self.lib.folder)),
                          (tr("Save a copy…", "احفظ نسخة…"), self.card_copy),
                          (tr("Move…", "انقل…"), self.card_move),
                          (tr("Open another…", "افتح مكتبة تانية…"), self.card_open_other),
                          (tr("Default library", "المكتبة الافتراضية"), self.card_default)):
            b = ttk.Button(fb, text=text, command=cmd)
            fb.add(b)
            self.card_buttons.append(b)
        WrapLabel(file_card.body, style="Muted.TLabel", text=tr(
            "Tip: drop a library file on the top bar to open it or merge it into this one.",
            "ممكن تسحب ملف مكتبة وتفلته على الشريط اللي فوق عشان تفتحه أو تدمجه في المكتبة دي.")).pack(
            fill="x", pady=(8, 0))

        sec = Card(self.lib_grid, tr("🔒  Password", "🔒  الباسورد"))
        self.lp_lock = WrapLabel(sec.body)
        self.lp_lock.pack(fill="x")
        self.lp_lock_btn = ttk.Button(sec.body, command=self.card_password)
        self.lp_lock_btn.pack(anchor=anchor(), pady=(8, 0))
        self.card_buttons.append(self.lp_lock_btn)

        space = Card(self.lib_grid, tr("💾  Space", "💾  المساحة"))
        self.lp_space = tk.Canvas(space.body, height=110, background=C["card"], highlightthickness=0)
        self.lp_space.pack(fill="x")
        self.lp_space.bind("<Configure>", lambda e: self.draw_space())
        b = ttk.Button(space.body, text=tr("Clean…", "نضّف…"), command=self.card_clean)
        b.pack(anchor=anchor(), pady=(6, 0))
        self.card_buttons.append(b)

        hist = Card(self.lib_grid, tr("🕘  History", "🕘  التاريخ"))
        self.lp_hist = WrapLabel(hist.body)
        self.lp_hist.pack(fill="x")
        hb = Flow(hist.body, padx=3)
        hb.pack(fill="x", pady=(8, 0))
        for text, cmd in ((tr("Sessions & undo", "الجلسات والتراجع"), self.open_sessions),
                          (tr("Log", "السجل"), self.open_log),
                          (tr("Backups", "النسخ الاحتياطية"), self.open_backups)):
            b = ttk.Button(hb, text=text, command=cmd)
            hb.add(b)
            self.lib_buttons.append(b)

        about = Card(self.lib_grid, tr("ℹ  About", "ℹ  عن البرنامج"))
        WrapLabel(about.body, text=tr(
            f"Face Sorter v{__version__}. Free and open source (MIT). Everything runs on this computer: no photo "
            "or data is uploaded. The library holds face data and names: only use photos you have the right to "
            "use, and set a password if the library is on a USB stick.",
            f"Face Sorter v{__version__}. برنامج مجاني ومفتوح المصدر (MIT). كل الشغل على الجهاز ده، ومفيش ولا صورة "
            "ولا بيانات بتترفع. المكتبة فيها بيانات وشوش وأسامي: استخدم البرنامج على صور ليك حق فيها، وحط "
            "باسورد لو المكتبة على فلاشة.")).pack(fill="x")
        b = ttk.Button(about.body, text=tr("Licenses and details", "الرخص والتفاصيل"), command=self.show_about)
        b.pack(anchor=anchor(), pady=(8, 0))
        self.lib_cards = [file_card, sec, space, hist, about]
        self.lib_cols = 0
        self.lib_grid.bind("<Configure>", lambda e: self.layout_lib_cards(e.width))
        self.layout_lib_cards(1)

    def layout_lib_cards(self, width: int) -> None:
        cols = 2 if width >= int(760 * self.zoom) else 1
        if cols == self.lib_cols:
            return
        self.lib_cols = cols
        for c in self.lib_cards:
            c.grid_forget()
        for k in range(2):
            self.lib_grid.columnconfigure(k, weight=1 if k < cols else 0, uniform="lib" if k < cols else "")
        for k, c in enumerate(self.lib_cards):
            col = (k % cols) if not rtl() else (cols - 1 - k % cols)
            c.grid(row=k // cols, column=col, sticky="nsew", padx=6, pady=6)

    def refresh_library_page(self) -> None:
        if not hasattr(self, "lp_file") or not self.lp_file.winfo_exists():
            return
        lib = self.lib
        st = lib.stats()
        default = tr("  (the default library)", "  (المكتبة الافتراضية)") if lib.folder == LIBRARY_DIR.resolve() else ""
        self.lp_file.config(text=tr(
            f"{lib.path.name}{default}\nFolder: {lib.folder}\nCreated {lib.created} · updated {lib.updated}",
            f"{lib.path.name}{default}\nالفولدر: {lib.folder}\nاتعملت {lib.created} · آخر تحديث {lib.updated}"))
        if lib.encrypted:
            self.lp_lock.config(text=tr(
                "This library is encrypted. It opens only with the password or the recovery key.",
                "المكتبة دي متشفرة، ومش بتتفتح غير بالباسورد أو مفتاح الاسترجاع."), style="Ok.TLabel")
            self.lp_lock_btn.config(text=tr("Change or remove the password…", "غيّر الباسورد أو شيله…"))
        else:
            self.lp_lock.config(text=tr(
                "No password: anyone with the file can open it. Recommended if the library is on a USB stick.",
                "من غير باسورد: أي حد معاه الملف يقدر يفتحه. يُفضّل تحط باسورد لو المكتبة على فلاشة."),
                style="Warn.TLabel")
            self.lp_lock_btn.config(text=tr("Set a password…", "حط باسورد…"))
        backups = len(lib.backups())
        self.lp_hist.config(text=tr(f"{st['sessions']} visits on {st['devices']} computer(s) · {backups} backups",
                                    f"{st['sessions']} زيارة على {st['devices']} جهاز · {backups} نسخة احتياطية"))
        self.draw_space()

    def draw_space(self) -> None:
        c = self.lp_space
        if not c.winfo_exists():
            return
        c.delete("all")
        size = self.lib.size_info()
        parts = [(tr("People", "الأشخاص"), size["people"], C["accent"]),
                 (tr("Unknown", "المجهولين"), size["unknown"], "#8B5CF6"),
                 (tr("Scan memory", "ذاكرة الفحص"), size["cache"], "#F59E0B"),
                 (tr("Backups", "النسخ الاحتياطية"), size["backups"], "#94A3B8")]
        w = max(100, c.winfo_width())
        total = sum(p[1] for p in parts)
        c.create_text(w - 2 if rtl() else 2, 10, anchor=anchor(), font=Fonts.bold, fill=C["text"],
                      text=tr(f"Library file: {size['file']} MB", f"ملف المكتبة: {size['file']} ميجا"))
        c.create_rectangle(0, 26, w, 38, fill="#E5EAF2", outline="")
        x = 0.0
        for _, mb, color in parts:
            seg = (w - 2) * mb / total if total > 0 else 0
            if seg > 0:
                x0, x1 = (w - x - seg, w - x) if rtl() else (x, x + seg)
                c.create_rectangle(x0, 26, x1, 38, fill=color, outline="")
            x += seg
        col_w = w / 2
        for k, (name, mb, color) in enumerate(parts):
            cx = (w - (k % 2) * col_w - 4) if rtl() else ((k % 2) * col_w + 4)
            yy = 56 + (k // 2) * 24
            c.create_rectangle(cx - 10 if rtl() else cx, yy - 5, cx if rtl() else cx + 10, yy + 5, fill=color,
                               outline="")
            c.create_text(cx - 16 if rtl() else cx + 16, yy, anchor=anchor(), font=Fonts.small, fill=C["text"],
                          text=f"{name}: {mb} " + tr("MB", "ميجا"))

    # ------------------------------------------------------------- small windows
    def small_window(self, title: str, width: int, height: int) -> tuple[tk.Toplevel, ttk.Frame]:
        win = tk.Toplevel(self.root)
        win.title(title)
        page_bg_window(win)
        fit_window(win, width, height)
        f = ttk.Frame(win, style="Page.TFrame", padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=title, style="PageTitle.TLabel").pack(anchor=anchor(), pady=(0, 10))
        return win, f

    def open_duplicates(self) -> None:
        pairs = self.lib.duplicates()
        win, f = self.small_window(tr("Possible duplicates", "يمكن نفس الشخص"), 920, 600)
        if not pairs:
            WrapLabel(f, text=tr("No possible duplicates.", "مفيش تكرار."), style="PageSub.TLabel").pack(fill="x")
            return
        sf = ScrollFrame(f)
        sf.pack(fill="both", expand=True)
        win.images = []
        for a, b, score in pairs[:20]:
            pa, pb = self.lib.person(a), self.lib.person(b)
            card = Card(sf.inner, padding=(10, 8))
            card.pack(fill="x", pady=4)
            row = Flow(card.body, padx=4)
            row.pack(fill="x")
            for p in (pa, pb):
                img = round_thumb(self.lib.thumb(p.id), 56)
                win.images.append(img)
                row.add(ttk.Label(row, image=img))
                row.add(ttk.Label(row, text=f"{person_label(p.label)} ({p.prints})", style="Bold.TLabel"))
            row.add(chip(row, tr(f"similar {score:.2f}", f"التشابه {score:.2f}")))

            def merge(src, dst, r=card):
                self._changed(self.lib.merge(src, dst))
                r.destroy()

            def differ(x=a, y=b, r=card):
                self.lib.not_duplicate(x, y)
                r.destroy()
            row.add(ttk.Button(row, text=tr("Different people", "مختلفين"), command=differ), end=True)
            row.add(ttk.Button(row, text=tr("Join into ", "ادمج في ") + person_label(pb.label),
                               command=lambda s=a, d=b: merge(s, d)), end=True)
            row.add(ttk.Button(row, text=tr("Join into ", "ادمج في ") + person_label(pa.label),
                               command=lambda s=b, d=a: merge(s, d)), end=True)

    def open_sessions(self) -> None:
        win, f = self.small_window(tr("Sessions", "الجلسات"), 920, 600)
        b = Flow(f, padx=4, style="Page.TFrame")
        b.pack(side="bottom", fill="x", pady=(10, 0))
        card = Card(f, padding=(10, 10))
        card.pack(fill="both", expand=True)
        lst = style_tk(tk.Listbox(card.body, height=9, justify=justify(), activestyle="none"))
        lst.pack(fill="x")
        txt = style_tk(tk.Text(card.body, wrap="word", height=10, padx=8, pady=6))
        txt.tag_configure("line", justify=justify())
        txt.pack(fill="both", expand=True, pady=(8, 0))
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
        b.add(ttk.Button(b, text=tr("Undo last session", "الغي آخر جلسة"), style="Danger.TButton", command=undo))
        b.add(ttk.Button(b, text=tr("Close", "قفل"), command=win.destroy), end=True)

    def open_backups(self) -> None:
        backups = self.lib.backups()
        win, f = self.small_window(tr("Backups", "النسخ الاحتياطية"), 700, 460)
        b = Flow(f, padx=4, style="Page.TFrame")
        b.pack(side="bottom", fill="x", pady=(10, 0))
        card = Card(f, padding=(10, 10))
        card.pack(fill="both", expand=True)
        lst = style_tk(tk.Listbox(card.body, justify=justify(), activestyle="none"))
        lst.pack(fill="both", expand=True)
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
        b.add(ttk.Button(b, text=tr("Restore", "رجّع"), style="Primary.TButton", command=restore))
        b.add(ttk.Button(b, text=tr("Open folder", "افتح الفولدر"), command=lambda: open_path(self.lib.folder)))
        b.add(ttk.Button(b, text=tr("Close", "قفل"), command=win.destroy), end=True)

    def open_log(self) -> None:
        win, f = self.small_window(tr("Log", "السجل"), 920, 600)
        kind = tk.StringVar(value="history")
        top = Flow(f, padx=6, style="Page.TFrame")
        top.pack(fill="x", pady=(0, 8))
        card = Card(f, padding=(10, 10))
        card.pack(fill="both", expand=True)
        txt = style_tk(tk.Text(card.body, wrap="word", padx=8, pady=6, font=Fonts.small))
        txt.tag_configure("line", justify=justify())
        txt.pack(fill="both", expand=True)

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
            fname = filedialog.asksaveasfilename(parent=win, defaultextension=".txt", initialfile="face_sorter_log.txt")
            if fname:
                self.lib.export_log(Path(fname))
        top.add(ttk.Button(top, text=tr("Save as text…", "احفظ كملف نص…"), command=save), end=True)
        show()

    # ============================================================ top bar
    def show_card_path(self) -> None:
        if not self.lib or not self.card_path.winfo_exists():
            return
        width = self.card_path.winfo_width()
        prefix = tr("Folder: ", "الفولدر: ")
        self.card_path.config(text=prefix + shorten_middle(str(self.lib.folder), Fonts.body,
                                                           width - Fonts.body.measure(prefix) - 12))

    def refresh_card(self) -> None:
        lib = self.lib
        st, size = lib.stats(), lib.size_info()
        self.card_name.config(text=lib.path.stem)
        self.show_card_path()
        self.chips.clear()
        if lib.encrypted:
            self.chips.add(chip(self.chips, tr("🔒 encrypted", "🔒 متشفرة"), C["ok"], C["ok_soft"]))
        if lib.folder == LIBRARY_DIR.resolve():
            self.chips.add(chip(self.chips, tr("default", "الافتراضية"), C["accent"], C["accent_soft"]))
        self.chips.add(chip(self.chips, tr(f"👥 {st['known']} people", f"👥 {st['known']} شخص")))
        self.chips.add(chip(self.chips, tr(f"❔ {st['unknown']} unknown", f"❔ {st['unknown']} مجهول")))
        self.chips.add(chip(self.chips, tr(f"💾 {size['file']} MB", f"💾 {size['file']} ميجا")))
        self.chips.add(chip(self.chips, tr(f"updated {lib.updated}", f"آخر تحديث {lib.updated}")))

    # ============================================================ library operations
    def ask_password(self, where: Path, wrong: bool = False) -> str | None:
        msg = tr(f"{'Wrong password. ' if wrong else ''}This library has a password:\n{where}\n\n"
                 "Type the password or the recovery key.",
                 f"{'الباسورد غلط. ' if wrong else ''}المكتبة دي عليها باسورد:\n{where}\n\n"
                 "اكتب الباسورد أو مفتاح الاسترجاع.")
        return simpledialog.askstring(tr("Password", "الباسورد"), msg, show="•", parent=self.root)

    def choose_file(self, files: list[Path]) -> Path | None:
        win = tk.Toplevel(self.root)
        win.title(tr("Which library?", "أنهي مكتبة؟"))
        page_bg_window(win)
        fit_window(win, 760, 360)
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, style="Page.TFrame", padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, style="PageSub.TLabel", justify=justify(), text=tr(
            "This folder has more than one library file. Choose one:",
            "الفولدر فيه أكتر من ملف مكتبة، اختار واحد:")).pack(anchor=anchor(), pady=(0, 8))
        ok_btn = ttk.Button(f, text=tr("Open", "افتح"), style="Primary.TButton")
        ok_btn.pack(side="bottom", anchor=anchor(), pady=(10, 0))
        lst = style_tk(tk.Listbox(f, justify=justify(), activestyle="none"))
        lst.pack(fill="both", expand=True)
        for p in files:
            lst.insert("end", p.name)
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
        self.refresh_people()
        self.notify(tr("Library: ", "المكتبة: ") + str(lib.path), "info")

    def card_copy(self) -> None:
        d = filedialog.askdirectory(title=tr("Where to save the copy", "تحفظ النسخة فين"))
        if d:
            folder = self.lib.copy_to(Path(d))
            self.notify(tr(f"Copy saved in {folder}. This library stays where it is.",
                           f"النسخة اتحفظت في {folder}. المكتبة الأصلية زي ما هي."))

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
                    self.refresh_all()
                    self.notify(tr("Password removed.", "الباسورد اتشال."), "warn")
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
        self.refresh_all()
        if recovery:
            self.show_recovery_key(recovery)
        else:
            self.notify(tr("Password changed. The recovery key stays the same.",
                           "الباسورد اتغير، ومفتاح الاسترجاع زي ما هو."))

    def show_recovery_key(self, key: str) -> None:
        win = tk.Toplevel(self.root)
        win.title(tr("Recovery key", "مفتاح الاسترجاع"))
        win.configure(background=C["card"])
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=tr("🔑  Your recovery key", "🔑  مفتاح الاسترجاع بتاعك"), style="CardTitle.TLabel").pack(
            anchor=anchor())
        ttk.Label(f, wraplength=540, justify=justify(), text=tr(
            "The library is now encrypted. Keep this recovery key somewhere safe (on paper, NOT on the same USB "
            "stick). If you forget the password, it is the ONLY way to open the library.",
            "المكتبة اتشفرت. احفظ مفتاح الاسترجاع ده في مكان آمن (على ورقة، مش على نفس الفلاشة). لو نسيت "
            "الباسورد، ده الطريقة الوحيدة لفتح المكتبة.")).pack(anchor=anchor(), pady=(6, 10))
        e = ttk.Entry(f, font=("Courier", 16, "bold"), justify="center", width=32)
        e.insert(0, key)
        e.config(state="readonly")
        e.pack(pady=8)

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(key)

        def save():
            fname = filedialog.asksaveasfilename(parent=win, defaultextension=".txt",
                                                 initialfile="FaceSorter_recovery_key.txt")
            if fname:
                Path(fname).write_text(f"Face Sorter recovery key for {self.lib.path.name}:\n{key}\n",
                                       encoding="utf-8")
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text=tr("I saved it", "حفظته"), style="Primary.TButton", command=win.destroy).pack(
            side=end())
        ttk.Button(b, text=tr("Copy", "انسخ"), command=copy).pack(side=start(), padx=4)
        ttk.Button(b, text=tr("Save as text…", "احفظه كملف…"), command=save).pack(side=start(), padx=4)
        self.root.wait_window(win)

    def card_clean(self) -> None:
        size = self.lib.size_info()
        if ask_yes(self.root, tr("Clean", "تنضيف"), tr(
                f"Forget the scan results of {size['cache_photos']} photos ({size['cache']} MB)?\n"
                f"No person or face is deleted; photos met again are simply checked again. "
                f"'Find' only searches photos that are scanned.",
                f"تمسح نتايج فحص {size['cache_photos']} صورة ({size['cache']} ميجا)؟\n"
                f"مفيش أي شخص أو وش هيتمسح، والصور لو اتقابلت تاني هتتفحص من جديد. "
                f"خد بالك إن «دوّر» بيدوّر في الصور اللي اتفحصت بس.")):
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
            "Models: YuNet (MIT) and SFace (Apache-2.0) from OpenCV Zoo. iPhone photos: libheif and libde265 (LGPL). "
            "All licenses: THIRD_PARTY_LICENSES.md",
            f"Face Sorter v{__version__}\nبرنامج مجاني ومفتوح المصدر (رخصة MIT).\n\n"
            "كل الشغل بيحصل على الجهاز ده، ومفيش ولا صورة ولا بيانات بتترفع.\n"
            "النماذج: YuNet (MIT) و SFace (Apache-2.0) من OpenCV Zoo. صور الآيفون: libheif و libde265 (LGPL). "
            "كل الرخص في THIRD_PARTY_LICENSES.md"))

    def on_error(self, exc, val, tb) -> None:
        text = "".join(traceback.format_exception(exc, val, tb))
        log_error("window", text)
        key = f"{exc.__name__}: {val}"
        if not self.errors.allow(key):  # the same problem again: written down, not shown again
            return
        self.errors.showing = True
        try:
            messagebox.showerror("Face Sorter", tr(
                f"Something went wrong:\n{val}\n\nDetails were saved in\n{ERRORS}",
                f"حصلت مشكلة:\n{val}\n\nالتفاصيل اتحفظت في\n{ERRORS}"))
        finally:
            self.errors.showing = False

    # ================================================================ plumbing
    def log(self, text: str) -> None:
        self.msgs.put(("log", text))

    def on_progress(self, stage: int, done: int, total: int) -> None:
        self.msgs.put(("progress", (stage, done, total)))

    def _append(self, text: str) -> None:
        self.logbox.config(state="normal")
        if self.logbox.tag_ranges("hint"):  # the first real line replaces the hint
            self.logbox.delete("1.0", "end")
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
                "exclude": self.f_exclude.get(), "language": language(), "settings_shown": self.settings_shown})
            SETTINGS.write_text(json.dumps(self.settings, ensure_ascii=False, indent=1), encoding="utf-8")
        except (OSError, ValueError, tk.TclError, AttributeError):
            pass

    def refresh_all(self) -> None:
        self.refresh_card()
        self.device_chip.config(text=tr("This computer: ", "الجهاز ده: ") + device_name())
        pending = pending_session(self.lib)
        self.resume_btn.config(state="normal" if pending and not self.busy else "disabled")
        if self.page == "people":
            self.refresh_people()
        elif self.page == "library":
            self.refresh_library_page()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for b in [self.start_btn, self.who_btn, self.resume_btn, self.lang_btn] + self.lib_buttons + self.card_buttons:
            if b.winfo_exists():
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
            self.find_selection_changed()

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
                self.notify(val)
                messagebox.showinfo(tr("Saved", "اتحفظ"), val)
            elif kind == "done":
                self.bar["value"] = 100
                self.progress_label.config(text="100% · " + tr("Done", "خلص"))
                self.time_label.config(text=f"⏱ {fmt_time(time.monotonic() - self.started)}")
                self.set_busy(False)
            elif kind == "find_found":
                job, found = val
                if job == self.find_job:
                    self.find_summary.config(text=tr(f"{len(found)} photos · loading pictures…",
                                                     f"{len(found)} صورة · بحمّل الصور…"))
            elif kind == "find_progress":
                job, k, n = val
                if job == self.find_job and n:
                    self.find_bar["value"] = 100 * k / n
            elif kind == "find_done":
                job, found, thumbs, *err = val
                if job == self.find_job and self.find_grid.winfo_exists():
                    self.find_show(found, thumbs)
                    if err:
                        self.notify(err[0], "error")
            else:
                self.set_busy(False)
                self.notify(str(val).split("\n")[0], "error")
                messagebox.showerror("Face Sorter", val)
        self.update_progress()
        self.root.after(150, self.pump)

    def run(self) -> None:
        self.root.mainloop()
        self.remember()
        self.lib.close()


def main() -> None:
    App().run()
