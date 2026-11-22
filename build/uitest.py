"""Interface test of the portable build, run on a clean Windows machine by GitHub Actions.

    python uitest.py <dist folder> <a photo with one clear face>

Opens the real window in Arabic and in English at 100%, 125% and 150% zoom, on a small
window (1024x700), and fails if any button or text does not fit (cut, squeezed or hidden):
the three tabs, the review window, "who is this?", the small windows, and after switching
the language.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
dist, sample = Path(sys.argv[1]), sys.argv[2]
py = dist / "FaceSorter" / "python" / "python.exe"
app_dir = dist / "FaceSorter" / "app"
work = Path(tempfile.mkdtemp())
img = cv2.imread(sample)
assert img is not None, "sample photo missing"
photos = work / "photos"
photos.mkdir()
for s in range(1, 5):
    rng = np.random.default_rng(s)
    v = np.clip(img.astype(np.float32) * rng.uniform(0.8, 1.2) + rng.uniform(-20, 20), 0, 255).astype(np.uint8)
    cv2.imwrite(str(photos / f"photo_{s}.jpg"), cv2.flip(v, 1) if s % 2 else v)

CHILD = r"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
sys.path.insert(0, sys.argv[1])
photos, W, H = Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
import traceback
from facesorter import gui, session
found = []

def no_popup(*args, **kw):  # a popup would wait forever on the test machine: note it and go on
    found.append("popup: " + " / ".join(str(a) for a in args))
    print("POPUP", args, flush=True)
    return None
for name in ("showinfo", "showwarning", "showerror", "askyesno", "askyesnocancel", "askokcancel"):
    setattr(gui.messagebox, name, no_popup)
gui.simpledialog.askstring = no_popup
gui.choose = no_popup

def on_error(exc, val, tb):
    found.append("error: " + "".join(traceback.format_exception(exc, val, tb)))
    print("ERROR", "".join(traceback.format_exception(exc, val, tb)), flush=True)
    r.destroy()
gui.App.on_error = lambda self, exc, val, tb: on_error(exc, val, tb)
app = gui.App()
r = app.root
r.after(280000, lambda: (found.append("timeout: the test took too long"), r.destroy()))

def settle():
    for _ in range(10):
        r.update()
        time.sleep(0.05)

def check(name, win):
    settle()
    for p in gui.layout_problems(win):
        found.append(f"{name}: {p}")
    print(f"  {name}: checked", flush=True)

def steps():
    r.state("normal")
    r.geometry(f"{W}x{H}+0+0")
    settle()
    for key in gui.PAGES:   # empty library: the pages say what to do first
        app.show_page(key)
        check("empty library, page " + key, r)
    sid = session.scan(app.lib, photos, gui.Filters(min_side=0))
    review = session.build_review(app.lib, sid, min_photos=1)
    rw = gui.ReviewWindow(app, review, "learn", None)
    rw.geometry(f"{W}x{H}+0+0")
    check("review window", rw)
    rw.destroy()
    for g in review.groups:
        g.name, g.touched = "Test Person", True
    session.save_review(app.lib, review, "learn")
    app.refresh_all()
    for key in gui.PAGES:   # a library with someone in it
        app.show_page(key)
        if key == "people" and app.tree_rows:
            app.tree.selection_set(next(iter(app.tree_rows)))
        if key == "find" and app.find_rows:
            app.find_tree.selection_set(next(iter(app.find_rows)))
            app.find_start()
            for _ in range(100):
                settle()
                if app.find_results:
                    break
            if not app.find_results:
                found.append("find: the photos of Test Person were not found")
        check("page " + key, r)
    app.who_add([photos / "photo_1.jpg"])
    check("who is this, with a photo", r)
    app.show_settings(True, False)
    app.show_page("scan")
    app.mode_var.set("learn_sort")
    app.mode_changed()
    check("scan page, advanced settings open, 'learn + sort'", r)
    for opener in (app.open_sessions, app.open_log, app.open_backups, app.open_duplicates):
        before = set(r.winfo_children())
        opener()
        for w in set(r.winfo_children()) - before:
            check(opener.__name__, w)
            w.destroy()
    app.change_language()
    r.geometry(f"{W}x{H}+0+0")
    for key in gui.PAGES:
        app.show_page(key)
        check("after switching language: page " + key, r)
    r.destroy()

def safe_steps():
    try:
        steps()
    except Exception:
        on_error(*sys.exc_info())
r.after(800, safe_steps)
r.mainloop()
for p in found:
    print("PROBLEM", p)
print("LAYOUT OK" if not found else f"{len(found)} LAYOUT PROBLEMS")
"""

failed = False
for lang in ("ar", "en"):
    for scale in ("1.0", "1.25", "1.5"):
        lib = work / f"lib_{lang}_{scale}"
        env = dict(os.environ, FACE_SORTER_LIBRARY=str(lib), FACE_SORTER_LANGUAGE=lang, FACE_SORTER_UI_SCALE=scale,
                   PYTHONIOENCODING="utf-8")
        print(f"== {lang} at {float(scale):.0%}, 1024x700")
        print("   starting...", flush=True)
        try:
            out = subprocess.run([str(py), "-c", CHILD, str(app_dir), str(photos), "1024", "700"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                                 timeout=300)
            stdout, stderr = out.stdout, out.stderr
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = "TIMEOUT: the window did not finish in 5 minutes"
        print(stdout[-6000:], stderr[-3000:], flush=True)
        if "LAYOUT OK" not in stdout:
            failed = True
settings = app_dir.parent / "settings.json"
if settings.exists():
    settings.unlink()
if failed:
    sys.exit("FAILED: some buttons or texts do not fit in the window")
print("ALL INTERFACE TESTS PASSED")
