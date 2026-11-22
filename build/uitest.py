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
from pathlib import Path
sys.path.insert(0, sys.argv[1])
photos, W, H = Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
from facesorter import gui, session
app = gui.App()
r = app.root
found = []

def settle():
    for _ in range(10):
        r.update()
        time.sleep(0.05)

def check(name, win):
    settle()
    for p in gui.layout_problems(win):
        found.append(f"{name}: {p}")
    print(f"  {name}: checked")

def steps():
    r.state("normal")
    r.geometry(f"{W}x{H}+0+0")
    settle()
    r.after(400)  # let the settings fold themselves if the window is short
    settle()
    for tab in (app.tab_pc, app.tab_who, app.tab_lib):
        app.nb.select(tab)
        check("tab " + app.nb.tab(tab, "text").strip(), r)
    sid = session.scan(app.lib, photos, gui.Filters(min_side=0))
    review = session.build_review(app.lib, sid, min_photos=1)
    rw = gui.ReviewWindow(app, review, "learn", None)
    rw.geometry(f"{W}x{H}+0+0")
    check("review window", rw)
    rw.destroy()
    session.discard_session(app.lib, sid)
    app.who_add([photos / "photo_1.jpg"])
    check("who is this", r)
    for opener in (app.open_sessions, app.open_log, app.open_backups, app.open_duplicates):
        before = set(r.winfo_children())
        opener()
        for w in set(r.winfo_children()) - before:
            check(opener.__name__, w)
            w.destroy()
    app.change_language()
    r.geometry(f"{W}x{H}+0+0")
    for tab in (app.tab_pc, app.tab_who, app.tab_lib):
        app.nb.select(tab)
        check("after switching language: " + app.nb.tab(tab, "text").strip(), r)
    r.destroy()

r.after(800, steps)
r.mainloop()
for p in found:
    print("PROBLEM", p)
print("LAYOUT OK" if not found else f"{len(found)} LAYOUT PROBLEMS")
"""

failed = False
for lang in ("ar", "en"):
    for scale in ("1.0", "1.25", "1.5"):
        lib = work / f"lib_{lang}_{scale}"
        env = dict(os.environ, FACE_SORTER_LIBRARY=str(lib), FACE_SORTER_LANGUAGE=lang, FACE_SORTER_UI_SCALE=scale)
        print(f"== {lang} at {float(scale):.0%}, 1024x700")
        out = subprocess.run([str(py), "-c", CHILD, str(app_dir), str(photos), "1024", "700"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", env=env, timeout=600)
        print(out.stdout[-6000:], out.stderr[-3000:])
        if "LAYOUT OK" not in out.stdout:
            failed = True
settings = app_dir.parent / "settings.json"
if settings.exists():
    settings.unlink()
if failed:
    sys.exit("FAILED: some buttons or texts do not fit in the window")
print("ALL INTERFACE TESTS PASSED")
