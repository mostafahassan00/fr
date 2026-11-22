"""How accurate is the recognition on your own photos?

    python face_sorter.py accuracy <folder> [--learn N]

<folder> has one folder per person, named after them, with their photos inside:

    test_photos/
        Mona/       photo1.jpg photo2.heic ...
        Ahmed/      ...
        _strangers/ (optional) photos of people who are NOT in the library

The tool learns each person from the first half of their photos (or the first N), then tries to
recognise them in the rest, exactly like the program does, and counts at every strictness level:

    right   sure and right                  maybe   "is this X?" and X is right
    WRONG   sure, but it is someone else    asked   "is this X?" but X is wrong
    missed  not recognised (stays unknown)

The aim: no WRONG at all (a wrong "sure" would teach the program a mistake), and as few missed as
possible. Everything runs on this computer; the photos are not copied or sent anywhere.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .engine import FaceEngine, encode_jpg, find_photos, read_photo, similarity_for
from .library import Library
from .session import MAYBE_MARGIN, SURE_MARGIN

STRANGERS = "_strangers"


@dataclass
class Result:
    people: int = 0
    learned: int = 0
    tested: int = 0
    strangers: int = 0
    no_face: list[str] = field(default_factory=list)
    # per strictness: counts
    table: dict[int, dict[str, int]] = field(default_factory=dict)
    per_person: dict[str, dict[str, int]] = field(default_factory=dict)   # at the chosen strictness


def biggest_face(engine: FaceEngine, path: Path):
    """The main face of a photo in a person's folder (the biggest one)."""
    _, img, _ = read_photo(path)
    if img is None:
        return None
    faces = engine.faces_in(img)
    return max(faces, key=lambda f: f.box[2] * f.box[3]) if faces else None


def verdict(best_name: str | None, score: float, strictness: int) -> str:
    base = similarity_for(strictness)
    if best_name is None or score < base + MAYBE_MARGIN:
        return "unknown"
    return "sure" if score >= base + SURE_MARGIN else "maybe"


def measure(folder: Path, learn: int = 0, strictness: int = 5, log=print) -> Result:
    folder = Path(folder)
    people = sorted(d for d in folder.iterdir() if d.is_dir() and d.name != STRANGERS)
    if not people:
        raise RuntimeError(f"No person folders in {folder}")
    engine = FaceEngine(log)
    res = Result(people=len(people))
    tmp = Path(tempfile.mkdtemp())
    lib = Library(tmp / "Library")   # a throw-away library, deleted at the end
    tests: list[tuple[str | None, np.ndarray]] = []   # (true name or None for a stranger, print)
    try:
        for d in people:
            photos = find_photos(d)
            n = learn or max(1, len(photos) // 2)
            faces = []
            for k, p in enumerate(photos):
                f = biggest_face(engine, p)
                if f is None:
                    res.no_face.append(str(p.relative_to(folder)))
                    continue
                if k < n:
                    faces.append({"emb": f.embedding, "crop": encode_jpg(f.crop, 75), "quality": f.quality,
                                  "key": f"sha1:{d.name}/{p.name}", "date": None, "device": "test"})
                else:
                    tests.append((d.name, f.embedding))
            if faces:
                lib.add_prints(lib.person_for_name(d.name), faces, "manual")
                res.learned += len(faces)
            log(f"{d.name}: learned from {len(faces)}, testing {sum(1 for t in tests if t[0] == d.name)}")
        if (folder / STRANGERS).is_dir():
            for p in find_photos(folder / STRANGERS):
                f = biggest_face(engine, p)
                if f is None:
                    res.no_face.append(str(p.relative_to(folder)))
                else:
                    tests.append((None, f.embedding))
        lib.changed()
        res.tested = sum(1 for t in tests if t[0] is not None)
        res.strangers = len(tests) - res.tested
        matches = lib.match_known(np.array([e for _, e in tests], np.float32)) if tests else []
        for s in range(1, 11):
            row = {"right": 0, "WRONG": 0, "maybe": 0, "asked": 0, "missed": 0, "stranger_ok": 0, "stranger_WRONG": 0}
            for (truth, _), m in zip(tests, matches):
                name, score = (m[0][1], m[0][2]) if m else (None, 0.0)
                v = verdict(name, score, s)
                if truth is None:
                    row["stranger_WRONG" if v == "sure" else "stranger_ok"] += 1
                elif v == "unknown":
                    row["missed"] += 1
                elif v == "sure":
                    row["right" if name == truth else "WRONG"] += 1
                else:
                    row["maybe" if name == truth else "asked"] += 1
                if s == strictness and truth is not None:
                    pp = res.per_person.setdefault(truth, {"right": 0, "WRONG": 0, "maybe": 0, "asked": 0,
                                                           "missed": 0})
                    key = ("missed" if v == "unknown" else
                           ("right" if name == truth else "WRONG") if v == "sure" else
                           ("maybe" if name == truth else "asked"))
                    pp[key] += 1
            res.table[s] = row
    finally:
        lib.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def report(res: Result, strictness: int = 5) -> str:
    lines = [f"People: {res.people} · faces learned: {res.learned} · faces tested: {res.tested}"
             + (f" · strangers: {res.strangers}" if res.strangers else ""), ""]
    lines.append("Strictness   right   WRONG   maybe   asked   missed" + ("   strangers taken" if res.strangers else ""))
    for s, r in res.table.items():
        mark = "  <- the program's default" if s == 5 else ""
        extra = f"   {r['stranger_WRONG']:15d}" if res.strangers else ""
        lines.append(f"{s:10d}   {r['right']:5d}   {r['WRONG']:5d}   {r['maybe']:5d}   {r['asked']:5d}   "
                     f"{r['missed']:6d}{extra}{mark}")
    safe = [s for s, r in res.table.items() if r["WRONG"] == 0 and r["stranger_WRONG"] == 0]
    if res.tested:
        if safe:
            best = max(safe, key=lambda s: (res.table[s]["right"], res.table[s]["maybe"], -abs(s - 5)))
            lines += ["", f"Best strictness with no wrong 'sure': {best}"]
        else:
            lines += ["", "Every strictness made a wrong 'sure': check the folders (one person per folder?)."]
    lines += ["", f"Per person (strictness {strictness}):"]
    for name, r in sorted(res.per_person.items()):
        lines.append(f"  {name:24s} right {r['right']:3d} · WRONG {r['WRONG']:3d} · maybe {r['maybe']:3d} · "
                     f"asked {r['asked']:3d} · missed {r['missed']:3d}")
    if res.no_face:
        lines += ["", f"No face found in {len(res.no_face)} photo(s): " + ", ".join(res.no_face[:10])
                  + (" ..." if len(res.no_face) > 10 else "")]
    return "\n".join(lines)
