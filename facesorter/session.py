"""A visit to one computer: scan its photos, prepare the review, save what you confirmed.

    scan()          stage 1-2: list photos (skip ones seen before), find faces (cached by content)
    build_review()  stage 3:   who is who -> sure / maybe / unknown people seen before / new people
    save_review()   stage 4:   learn what you confirmed, copy photos into folders (by mode)
    identify()      "who is this?" for one photo, without changing anything

Modes:
    learn       review + learn, no copies
    learn_sort  review + learn + copy the photos into a folder per person
    sort        review + copy only; the library does not change
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from .engine import Filters, cluster, encode_jpg, file_date, find_photos, read_photo, similarity_for
from .i18n import person_label, tr
from .library import Library, cache_pack, cache_unpack, copy_file, device_name
from .perf import CORES, EnginePool, Throttle, set_low_priority, speed_label, workers_for

MODES = ("learn", "learn_sort", "sort")


def mode_label(mode: str) -> str:
    return {"learn": tr("Learn only", "تعلم بس"), "learn_sort": tr("Learn + sort", "تعلم + ترتيب"),
            "sort": tr("Sort only", "ترتيب بس")}[mode]



SURE_MARGIN = 0.14       # score >= threshold + this: "sure"
MAYBE_MARGIN = 0.02      # score >= threshold + this: "is this X?" (tuned on hard photos: v3.1)
JOIN_MARGIN = 0.04       # joins an unknown person seen before
BATCH = 24


def stage_label(stage: int) -> str:
    return {1: tr("Listing photos", "حصر الصور"), 2: tr("Finding faces", "البحث عن الوشوش"),
            3: tr("Who is who", "التعرف"), 4: tr("Saving", "الحفظ"), 5: tr("Copying", "النسخ")}.get(stage, "")



OTHERS, NO_FACES = "Others", "No_faces"

Log = Callable[[str], None]
Progress = Callable[[int, int, int], None]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===================================================================== scanning
@dataclass
class ScanSummary:
    files: int = 0
    known: int = 0
    skipped: int = 0
    new: int = 0
    unreadable: int = 0
    stopped: bool = False


def scan(lib: Library, folder: Path, filters: Filters | None = None, throttle: Throttle | None = None,
         progress: Progress | None = None, log: Log = print) -> int:
    """Scan a folder of this computer. Returns the session id (an unfinished session of the same
    folder is continued). Nothing is learned yet."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise RuntimeError(tr(f"Folder not found: {folder}", f"الفولدر مش موجود: {folder}"))
    filters = filters or Filters()
    throttle = throttle or Throttle()
    progress = progress or (lambda st, i, n: None)
    device = device_name()
    with lib.lock:
        row = lib.db.execute("SELECT id FROM sessions WHERE device=? AND folder=? AND status IN ('scanning','review')",
                             (device, str(folder))).fetchone()
        if row:
            sid = row[0]
            log(tr(f"Continuing the unfinished session {sid} for this folder.", f"بكمّل الجلسة {sid} اللي مخلصتش للفولدر ده."))
        else:
            sid = lib.db.execute("INSERT INTO sessions(started, device, folder, status) VALUES(?,?,?,?)",
                                 (_now(), device, str(folder), "scanning")).lastrowid
        lib.db.execute("UPDATE sessions SET status='scanning' WHERE id=?", (sid,))
        lib.db.commit()
        lib.session_id = sid
        s = ScanSummary()
        lib.log("run", f"Scanning {folder} on {device}")

        # ---- stage 1: list; files seen before (same path, size, date) are not opened again
        progress(1, 0, 1)
        files = find_photos(folder)
        s.files = len(files)
        todo = []
        for i, path in enumerate(files):
            if i % 200 == 0:
                progress(1, i, len(files))
            why = filters.skip_file(path)
            if why:
                s.skipped += 1
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            seen = lib.db.execute("SELECT s.size, s.mtime, s.sha1, c.status FROM seen s "
                                  "LEFT JOIN photo_cache c ON c.sha1=s.sha1 WHERE s.device=? AND s.path=?",
                                  (device, str(path))).fetchone()
            if seen and seen[0] == st.st_size and abs(seen[1] - st.st_mtime) < 1e-3 and seen[3] and \
                    not (seen[3] == "skipped" and not filters.min_side):
                lib.db.execute("INSERT OR REPLACE INTO session_photos(session_id, sha1, path) VALUES(?,?,?)",
                               (sid, seen[2], str(path)))
                s.known += 1
            else:
                todo.append((path, st.st_size, st.st_mtime))
        lib.db.commit()
        progress(1, len(files), len(files))
        log(tr(f"Stage 1: {s.files} photos, {len(todo)} to check, {s.known} checked before, {s.skipped} skipped.",
                f"المرحلة 1: {s.files} صورة، {len(todo)} هتتفحص، {s.known} اتفحصت قبل كده، {s.skipped} اتجاهلت."))

        # ---- stage 2: faces, in small saved batches, several at a time
        _find_faces(lib, sid, device, todo, filters, throttle, progress, log, s)
        lib.db.execute("UPDATE sessions SET status=? WHERE id=?", ("scanning" if s.stopped else "review", sid))
        lib.db.commit()
        lib.log("run", f"Scan: {s.files} photos, {s.new} new, {s.known} known, {s.skipped} skipped"
                       + (" (stopped)" if s.stopped else ""))
        lib.session_id = None
    return sid


def _find_faces(lib, sid, device, todo, filters, throttle, progress, log, s: ScanSummary) -> None:
    progress(2, 0, len(todo))
    if not todo:
        return
    pool = EnginePool(log)
    level = None

    def work(item):
        path, size, mtime = item
        with pool.engine() as eng:
            sha1, img, date = read_photo(path)
            if img is None:
                return item, sha1, date, None, None
            why = filters.skip_image(img)
            if why:
                return item, sha1, date, why, None
            return item, sha1, date, None, eng.faces_in(img)

    with ThreadPoolExecutor(max_workers=CORES) as ex:
        for start in range(0, len(todo), BATCH):
            while throttle.pause.is_set() and not throttle.stop.is_set():
                time.sleep(0.2)
            if throttle.stop.is_set():
                s.stopped = True
                log(tr("Stopped. Everything finished so far is saved; start again to continue.",
                       "وقفت. كل اللي خلص اتحفظ، ودوس ابدأ تاني عشان تكمّل."))
                break
            lvl = throttle.effective_level(time.monotonic())
            if lvl != level:
                level = lvl
                pool.resize(workers_for(lvl))
                set_low_priority(lvl == "quiet")
                note = tr(" (on battery)", " (على البطارية)") if throttle.on_battery and throttle.battery_saver else ""
                log(tr(f"Speed: {lvl}{note}, {workers_for(lvl)} photo(s) at a time.",
                        f"السرعة: {speed_label(lvl)}{note}، {workers_for(lvl)} صورة في نفس الوقت."))
            for (path, size, mtime), sha1, date, skip, faces in ex.map(work, todo[start:start + BATCH]):
                if sha1 is None:
                    s.unreadable += 1
                    continue
                cached = lib.db.execute("SELECT status FROM photo_cache WHERE sha1=?", (sha1,)).fetchone()
                if cached is None or (cached[0] == "skipped" and not skip):
                    if skip:
                        status, packed = "skipped", None
                    elif faces is None:
                        status, packed = "unreadable", None
                    else:
                        status = "faces" if faces else "no_faces"
                        packed = cache_pack([(f.box, f.score, f.embedding.tobytes(), encode_jpg(f.crop, 75),
                                              f.quality, f.good, f.why) for f in faces])
                    lib.db.execute("INSERT OR REPLACE INTO photo_cache(sha1, date, status, faces) VALUES(?,?,?,?)",
                                   (sha1, date or file_date(path), status, packed))
                    s.new += 1
                else:
                    s.known += 1
                if skip:
                    s.skipped += 1
                lib.db.execute("INSERT OR REPLACE INTO seen(device, path, size, mtime, sha1) VALUES(?,?,?,?,?)",
                               (device, str(path), size, mtime, sha1))
                lib.db.execute("INSERT OR REPLACE INTO session_photos(session_id, sha1, path) VALUES(?,?,?)",
                               (sid, sha1, str(path)))
            lib.db.commit()  # this batch is safe now
            progress(2, min(start + BATCH, len(todo)), len(todo))
    log(tr(f"Stage 2: {s.new} photos checked for faces.", f"المرحلة 2: {s.new} صورة اتفحصت."))


# ====================================================================== review
@dataclass
class SFace:
    i: int
    sha1: str
    path: str
    emb: np.ndarray
    crop: bytes
    quality: float
    good: bool
    why: str
    date: str | None


@dataclass
class Group:
    section: str                       # sure | maybe | seen_before | new
    person_id: int | None              # known person, or unknown person seen before
    name: str                          # proposed name ('' = no name)
    faces: list[int]                   # indices into Review.faces
    candidates: list[tuple[str, float]] = field(default_factory=list)
    accepted: dict[int, bool] = field(default_factory=dict)
    already: set[int] = field(default_factory=set)   # photo already taught this person
    pool: list[int] = field(default_factory=list)    # unknown faces from the library joining this group
    seen_count: int = 0                # for unknown people seen before
    devices: int = 0
    touched: bool = False              # you changed something in it

    def ok(self, i: int) -> bool:
        """Is face i accepted? Your choice if you made one, else: yes, except for "is this X?"."""
        return self.accepted.get(i, self.section != "maybe")

    def set(self, i: int, value: bool) -> None:
        self.accepted[i] = value
        self.touched = True


@dataclass
class Review:
    session_id: int
    folder: str
    device: str
    faces: list[SFace]
    groups: list[Group]
    loose: list[int]                   # faces nobody could be found for (stay unknown)
    photos: dict[str, str]             # sha1 -> path on this computer
    no_faces: list[str]
    threshold: float

    def counts(self) -> dict:
        c = {"sure": 0, "maybe": 0, "seen_before": 0, "new": 0}
        for g in self.groups:
            c[g.section] += 1
        return c


def build_review(lib: Library, sid: int, strictness: int = 5, min_photos: int = 2,
                 progress: Progress | None = None) -> Review:
    progress = progress or (lambda st, i, n: None)
    progress(3, 0, 1)
    base = similarity_for(strictness)
    with lib.lock:
        sess = lib.db.execute("SELECT folder, device FROM sessions WHERE id=?", (sid,)).fetchone()
        photos = dict(lib.db.execute("SELECT sha1, path FROM session_photos WHERE session_id=?", (sid,)).fetchall())
        faces: list[SFace] = []
        no_faces = []
        for sha1, path in photos.items():
            row = lib.db.execute("SELECT date, status, faces FROM photo_cache WHERE sha1=?", (sha1,)).fetchone()
            if not row:
                continue
            date, status, packed = row
            if status == "no_faces":
                no_faces.append(sha1)
            for box, score, emb, crop, q, good, why in cache_unpack(packed):
                faces.append(SFace(len(faces), sha1, path, np.frombuffer(emb, np.float32), crop, q, good, why, date))
        review = Review(sid, sess[0], sess[1], faces, [], [], photos, no_faces, base)
        if not faces:
            progress(3, 1, 1)
            return review
        E = np.array([f.emb for f in faces])

        # which photos already taught which person (e.g. the same photo on another computer)
        taught: dict[str, set[int]] = {}
        keys = list({"sha1:" + f.sha1 for f in faces})
        for k in range(0, len(keys), 500):
            chunk = keys[k:k + 500]
            for pk, pid in lib.db.execute(f"SELECT photo_key, person_id FROM prints WHERE photo_key IN "
                                          f"({','.join('?' * len(chunk))})", chunk):
                taught.setdefault(pk, set()).add(pid)

        # 1) known people
        groups: dict[tuple[str, int], Group] = {}
        rest = []
        for f, m in zip(faces, lib.match_known(E)):
            if m and m[0][2] >= base + MAYBE_MARGIN:
                pid, name, score = m[0]
                section = "sure" if score >= base + SURE_MARGIN else "maybe"
                g = groups.get((section, pid))
                if g is None:
                    g = groups[(section, pid)] = Group(section, pid, name, [])
                g.faces.append(f.i)
                if section == "maybe":
                    for cpid, cname, cs in m:
                        if cs >= base and cname not in [c[0] for c in g.candidates]:
                            g.candidates.append((cname, cs))
                if pid in taught.get("sha1:" + f.sha1, set()):
                    g.already.add(f.i)
            else:
                rest.append(f.i)

        # 2) unknown people seen before (on this or other computers)
        rest2 = []
        if rest:
            info = {p.id: p for p in lib.people("unknown")}
            for i, (pid, score) in zip(rest, lib.match_groups(E[rest], "unknown")):
                if pid is not None and score >= base + JOIN_MARGIN:
                    g = groups.get(("seen_before", pid))
                    if g is None:
                        p = info.get(pid)
                        g = groups[("seen_before", pid)] = Group("seen_before", pid, "", [],
                                                                 seen_count=p.seen_count if p else 0,
                                                                 devices=len(p.devices) if p else 0)
                    g.faces.append(i)
                else:
                    rest2.append(i)

        # 3) new people: group the rest together with unknown faces kept from other visits
        new_groups = []
        if rest2:
            pool = lib.loose_faces()
            pool_e = np.array([e for _, e, _ in pool], np.float32).reshape(-1, 128)
            allE = np.vstack([E[rest2], pool_e]) if len(pool_e) else E[rest2]
            labels = cluster(allE, base)
            members: dict[int, list[int]] = {}
            for k, lab in enumerate(labels):
                members.setdefault(int(lab), []).append(k)
            for ks in members.values():
                mine = [rest2[k] for k in ks if k < len(rest2)]
                theirs = [pool[k - len(rest2)] for k in ks if k >= len(rest2)]
                photos_n = len({faces[i].sha1 for i in mine} | {key for _, _, key in theirs})
                if mine and photos_n >= min_photos:
                    g = Group("new", None, "", mine, pool=[pid for pid, _, _ in theirs])
                    new_groups.append(g)
                else:
                    review.loose += mine

        order = {"maybe": 0, "sure": 1, "seen_before": 2}
        known = sorted(groups.values(), key=lambda g: (order[g.section], -g.seen_count, (g.name or "").lower()))
        review.groups = known + sorted(new_groups, key=lambda g: -len(g.faces))
    progress(3, 1, 1)
    return review


def save_review(lib: Library, review: Review, mode: str, dest: Path | None = None, include_others: bool = True,
                progress: Progress | None = None, log: Log = print) -> str:
    """Apply your review. Returns the session report."""
    if mode not in MODES:
        raise ValueError(mode)
    learn = mode in ("learn", "learn_sort")
    sort = mode in ("learn_sort", "sort")
    if sort and not dest:
        raise RuntimeError(tr("Choose where to put the sorted photos.", "اختار هتحط الصور المرتبة فين."))
    progress = progress or (lambda st, i, n: None)
    faces = review.faces
    sid = review.session_id
    with lib.lock:
        lib.session_id = sid
        backup = lib.backup(f"before_session{sid}") if learn else None
        progress(4, 0, len(review.groups))
        folders_of: dict[str, set[str]] = {}
        report = {"recognised": {}, "named": [], "unknown": 0, "learned": 0, "unsure_skipped": 0}
        unknown_n = 0

        def face_dict(i):
            f = faces[i]
            return {"emb": f.emb, "crop": f.crop, "quality": f.quality, "key": "sha1:" + f.sha1, "date": f.date,
                    "device": review.device}

        leftovers = list(review.loose)
        for gi, g in enumerate(review.groups):
            acc = [i for i in g.faces if g.ok(i)]
            leftovers += [i for i in g.faces if not g.ok(i)]
            name = g.name.strip()
            label = name
            if learn and acc:
                if name:
                    if g.section in ("seen_before",) and g.person_id is not None:
                        lib.rename(g.person_id, name)          # an unknown person gets a name
                        pid = lib.person_by_name(name)
                    else:
                        pid = lib.person_for_name(name)
                    manual = g.touched or g.section != "sure"
                    source = "manual" if manual else "auto"
                    if g.section in ("new", "seen_before"):
                        report["named"].append(name)
                    else:
                        report["recognised"][name] = report["recognised"].get(name, 0) + len(acc)
                else:
                    pid = g.person_id if g.section == "seen_before" else lib.new_person(None, "unknown")
                    source = "unknown"
                    report["unknown"] += 1
                good = [face_dict(i) for i in acc if faces[i].good and i not in g.already]
                report["unsure_skipped"] += sum(1 for i in acc if not faces[i].good)
                report["learned"] += lib.add_prints(pid, good, source, sid)
                if g.pool:  # unknown faces from earlier visits that belong to this person
                    q = ",".join("?" * len(g.pool))
                    lib.db.execute(f"UPDATE prints SET person_id=?, source=? WHERE id IN ({q})",
                                   [pid, "manual" if name else "unknown", *g.pool])
                lib.note_seen(pid, len({faces[i].sha1 for i in acc}), review.device)
                if not name:
                    label = f"Unknown_{pid:04d}"
            elif acc:
                if not name:
                    unknown_n += 1
                    label = (f"Unknown_{g.person_id:04d}" if g.person_id else f"New_person_{unknown_n:02d}")
                if name:
                    report["recognised"][name] = report["recognised"].get(name, 0) + len(acc)
            for i in acc:
                folders_of.setdefault(faces[i].sha1, set()).add(label)
            progress(4, gi + 1, len(review.groups))

        if learn:  # faces nobody was found for: keep them, they may be recognised later
            lib.add_prints(None, [face_dict(i) for i in leftovers if faces[i].good], "unknown", sid)
            lib.changed()

        copied = 0
        if sort:
            jobs = []
            with_faces = {f.sha1 for f in faces}
            for sha1, path in review.photos.items():
                fs = folders_of.get(sha1)
                if fs:
                    jobs += [(path, f) for f in fs]
                elif include_others and sha1 in with_faces:
                    jobs.append((path, OTHERS))
                elif include_others and sha1 in review.no_faces:
                    jobs.append((path, NO_FACES))
            for k, (path, folder) in enumerate(jobs):
                src = Path(path)
                if src.exists():
                    copy_file(src, Path(dest) / folder)
                    copied += 1
                if k % 20 == 0:
                    progress(5, k, len(jobs))
            progress(5, len(jobs), len(jobs))

        lines = [tr(f"Computer: {review.device}", f"الجهاز: {review.device}"),
                 tr(f"Folder: {review.folder}", f"الفولدر: {review.folder}"),
                 tr(f"Mode: {mode_label(mode)}", f"الوضع: {mode_label(mode)}"),
                 tr(f"Photos: {len(review.photos)} · faces: {len(faces)}", f"الصور: {len(review.photos)} · الوشوش: {len(faces)}")]
        if report["recognised"]:
            lines.append(tr("Recognised: ", "اتعرف على: ")
                         + ", ".join(f"{n} ({c})" for n, c in sorted(report["recognised"].items())))
        if report["named"]:
            lines.append(tr("New names: ", "أسامي جديدة: ") + ", ".join(report["named"]))
        if learn:
            lines.append(tr(f"Unknown people kept: {report['unknown']} · new face prints learned: {report['learned']}"
                            f" · low-quality faces not learned: {report['unsure_skipped']}",
                            f"مجهولين اتحفظوا: {report['unknown']} · بصمات جديدة اتعلمت: {report['learned']}"
                            f" · وشوش مش واضحة متعلمتش: {report['unsure_skipped']}"))
        else:
            lines.append(tr("The library was not changed (sort only).", "المكتبة متغيرتش (ترتيب بس)."))
        if sort:
            lines.append(tr(f"Copied {copied} photos to {dest}", f"اتنسخت {copied} صورة في {dest}"))
        text = "\n".join(lines)
        lib.db.execute("UPDATE sessions SET status='saved', finished=?, mode=?, report=?, backup=? WHERE id=?",
                       (_now(), mode, text, str(backup) if backup else None, sid))
        lib.db.commit()
        lib.log("history", f"Session {sid} saved. " + text.replace("\n", " | "))
        lib.session_id = None
        log(text)
        return text


def pending_session(lib: Library) -> tuple[int, str] | None:
    """A session that was scanned but not saved yet (to continue its review)."""
    row = lib.db.execute("SELECT id, folder FROM sessions WHERE status='review' AND device=? ORDER BY id DESC LIMIT 1",
                         (device_name(),)).fetchone()
    return (row[0], row[1]) if row else None


def discard_session(lib: Library, sid: int) -> None:
    lib.db.execute("UPDATE sessions SET status='discarded' WHERE id=?", (sid,))
    lib.db.commit()


# ==================================================================== identify
def identify(lib: Library, engine, path: Path) -> tuple[np.ndarray | None, list[dict]]:
    """'Who is this?' for one photo: (the photo as shown, one dict per face). Nothing in the library changes
    (use Library.save_face to keep a face)."""
    path = Path(path)
    sha1, img, date = read_photo(path)
    if img is None:
        raise RuntimeError(tr(f"Cannot open this photo: {path.name}", f"مقدرتش أفتح الصورة دي: {path.name}"))
    out = []
    faces = engine.faces_in(img)
    if not faces:
        return img, out
    base = similarity_for(5)
    for f, m in zip(faces, lib.match_known(np.array([x.embedding for x in faces]))):
        best = [(name, score) for _, name, score in m if score >= base]
        verdict = ("sure" if best and best[0][1] >= base + SURE_MARGIN else
                   "maybe" if best and best[0][1] >= base + MAYBE_MARGIN else "unknown")
        seen = None
        if verdict == "unknown":
            pid, sc = lib.match_groups(f.embedding[None, :], "unknown")[0]
            if pid is not None and sc >= base + JOIN_MARGIN:
                p = lib.person(pid)
                seen = tr(f"{p.label} (seen {p.seen_count} times on {len(p.devices)} computer(s))",
                          f"{person_label(p.label)} (اتشاف {p.seen_count} مرة على {len(p.devices)} جهاز)") if p else None
        out.append({"crop": f.crop, "box": f.box, "matches": best, "verdict": verdict, "good": f.good,
                    "seen_before": seen,
                    "face": {"emb": f.embedding, "crop": encode_jpg(f.crop), "quality": f.quality,
                             "key": "sha1:" + sha1, "date": date or file_date(path), "device": device_name()}})
    return img, out


# ------------------------------------------------------------------ find people
@dataclass
class Found:
    path: Path
    date: str | None
    sure: bool                 # every chosen person is surely in it (confirmed, or recognised "sure")
    people: dict[int, str]     # person id -> "confirmed" / "sure" / "maybe"


def find_people(lib: Library, pids: list[int], strictness: int = 5, device: str | None = None,
                progress: Progress = lambda *a: None) -> list[Found]:
    """Photos on this computer with ALL of these people in them, from the saved scan results
    (photos are not opened again). Newest first. Photos that were moved or deleted are left out."""
    device = device or device_name()
    pids = list(dict.fromkeys(pids))
    if not pids:
        return []
    base = similarity_for(strictness)
    with lib.lock:
        rows = lib.db.execute("SELECT s.path, s.sha1, c.date, c.faces FROM seen s JOIN photo_cache c ON c.sha1=s.sha1 "
                              "WHERE s.device=? AND c.status='faces'", (device,)).fetchall()
        q = ",".join("?" * len(pids))
        confirmed: dict[str, set[int]] = {}
        for key, pid in lib.db.execute(f"SELECT photo_key, person_id FROM prints WHERE person_id IN ({q}) "
                                       f"AND source IN ('manual','auto')", pids):
            if key and key.startswith("sha1:"):
                confirmed.setdefault(key[5:], set()).add(pid)
    faces_of: list[tuple[int, np.ndarray]] = []   # (row index, print)
    for k, (_, _, _, blob) in enumerate(rows):
        for _box, _score, emb, *_ in cache_unpack(blob):
            faces_of.append((k, np.frombuffer(emb, np.float32)))
        if k % 500 == 0:
            progress(1, k, len(rows))
    found_in: dict[int, dict[int, str]] = {}
    if faces_of:
        matches = lib.match_known(np.array([e for _, e in faces_of], np.float32), top=1)
        for (k, _), m in zip(faces_of, matches):
            if not m:
                continue
            pid, _name, score = m[0]
            if pid in pids and score >= base + MAYBE_MARGIN:
                level = "sure" if score >= base + SURE_MARGIN else "maybe"
                have = found_in.setdefault(k, {})
                if have.get(pid) != "sure":
                    have[pid] = level
    out = []
    for k, (path, sha1, date, _) in enumerate(rows):
        people = dict(found_in.get(k, {}))
        for pid in confirmed.get(sha1, ()):
            people[pid] = "confirmed"
        if all(pid in people for pid in pids) and Path(path).exists():
            out.append(Found(Path(path), date, all(people[x] != "maybe" for x in pids), people))
    progress(1, len(rows), len(rows))
    out.sort(key=lambda f: (f.date or "", str(f.path)), reverse=True)
    return out

