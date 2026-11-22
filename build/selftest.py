"""Self-test of the portable build, run on a clean Windows machine by GitHub Actions.

    python selftest.py <dist folder> <a photo with one clear face>

Makes two folders of slightly different copies of the face, then uses the
portable Face Sorter (its own Python, no system Python) to:
learn the person, name them, recognise them in new photos, and answer "who is this?".
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
dist, sample = Path(sys.argv[1]), sys.argv[2]
py = dist / "FaceSorter" / "python" / "python.exe"
app = dist / "FaceSorter" / "app" / "face_sorter.py"
work = Path(tempfile.mkdtemp())
lib = work / "Library"
img = cv2.imread(sample)
assert img is not None, "sample photo missing"


def variants(folder: Path, seeds) -> None:
    folder.mkdir(parents=True)
    for s in seeds:
        rng = np.random.default_rng(s)
        v = img.astype(np.float32) * rng.uniform(0.8, 1.2) + rng.uniform(-20, 20)
        v = np.clip(v, 0, 255).astype(np.uint8)
        if s % 2:
            v = cv2.flip(v, 1)
        scale = rng.uniform(0.85, 1.15)
        v = cv2.resize(v, None, fx=scale, fy=scale)
        cv2.imwrite(str(folder / f"photo_{s}.jpg"), v)


def run(*args) -> str:
    out = subprocess.run([str(py), str(app), "--library", str(lib), *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    print(">", " ".join(args), "\n", out.stdout, out.stderr)
    if out.returncode:
        sys.exit(f"FAILED: {' '.join(args)}")
    return out.stdout


variants(work / "pc1", range(1, 5))
variants(work / "pc2", range(11, 14))
out = run("scan", str(work / "pc1"), "--keep-small")
assert "1 new" in out or "new" in out, "no new person found"
run("name", "Unknown_0001", "Test Person")
out = run("scan", str(work / "pc2"), "--keep-small", "--mode", "learn_sort", "--dest", str(work / "sorted"))
assert "Recognised: Test Person" in out, "did not recognise the person on the second visit"
assert (work / "sorted" / "Test Person").is_dir(), "sorted folder missing"
assert "Test Person" in run("who", str(work / "pc2" / "photo_11.jpg")), "who-is-this failed"
assert "Test Person" in run("people")
run("undo")

ENC = r"""
import sys; sys.path.insert(0, sys.argv[1])
from pathlib import Path
from facesorter import store
from facesorter.library import Library, LockedError
assert store.HAS_CIPHER, "SQLCipher missing"
folder = Path(sys.argv[2])
lib = Library(folder)
assert store.parse_name(lib.path), "library file has no dated name: " + lib.path.name
people = len(lib.people(None))
key = lib.set_password("كلمة سر")
lib.close()
assert store.is_encrypted(lib.path), "file not encrypted"
for bad in (None, "wrong"):
    try:
        Library(folder, bad); raise SystemExit("opened without the right password!")
    except LockedError:
        pass
for good in ("كلمة سر", key, key.lower()):
    l = Library(folder, good); assert len(l.people(None)) == people; l.close()
print("encryption ok")
"""
enc = subprocess.run([str(py), "-c", ENC, str(app.parent), str(lib)], capture_output=True, text=True,
                     encoding="utf-8", errors="replace")
print(enc.stdout, enc.stderr)
assert "encryption ok" in enc.stdout, "encryption test failed"

tk = subprocess.run([str(py), "-c", "import tkinter as t; r=t.Tk(); r.update(); r.destroy(); "
                     "from tkinterdnd2 import TkinterDnD; d=TkinterDnD.Tk(); d.update(); d.destroy(); "
                     "import sys; sys.path.insert(0, r'" + str(app.parent) + "'); import facesorter.gui; print('window ok')"],
                    capture_output=True, text=True)
print(tk.stdout, tk.stderr)
assert "window ok" in tk.stdout, "the window (Tk) does not work"
print("ALL SELF-TESTS PASSED")
