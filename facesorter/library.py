"""The library: a folder that holds what the program knows about people.

    Library/
        FaceSorter_Library_created-<date>_updated-<date>.db
                          people, their face prints and small face pictures, unknown faces,
                          sessions (one per computer visit), the log. The "updated" date in the
                          name changes by itself whenever the library changes.
        ....key           only with a password (see store.py)
        backups/          automatic copies (before each saved session)

The library never depends on your photo files: it keeps a 128-number print
and a small 112x112 picture of every face it learned from, so it can be moved
on its own (e.g. on a USB stick) and keeps growing on every computer.

A person ("profile") has:
    prints     faces you confirmed (manual) or it learned by itself (auto)
    looks      groups of similar prints (with glasses, beard, younger...)
    stats      how often seen, on which computers, last time
Unknown people are kept too (kind='unknown'), so someone seen on several
computers can be named later.
"""
from __future__ import annotations

import json
import os
import pickle
import shutil
import socket
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from .engine import APP_DIR, cluster, decode_jpg, top_k_mean
from . import store
from .store import LockedError, MultipleLibrariesError  # noqa: F401  (re-exported for the window)

_ROOT = APP_DIR.parent if APP_DIR.name == "app" else APP_DIR   # portable build: FaceSorter/app/...
LIBRARY_DIR = Path(os.environ.get("FACE_SORTER_LIBRARY", _ROOT.parent / "Library"))

MAX_MANUAL = 500         # prints kept per person that you confirmed
KEEP_FIRST = 30          # the first manual prints are never dropped
MAX_AUTO = 200           # prints kept per person that were learned automatically
MAX_UNKNOWN = 150        # prints kept per unknown person
MAX_LOOSE = 5000         # unknown faces not (yet) in any group
REDUNDANT = 0.93         # a new print this similar to an existing one adds nothing
LOOK_SIM = 0.55          # prints this similar are the same "look"
KEEP_BACKUPS = 10

Log = Callable[[str], None]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS people(
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT NOT NULL CHECK(kind IN ('known', 'unknown')),
    created TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 0, devices TEXT DEFAULT '', note TEXT);
CREATE TABLE IF NOT EXISTS prints(
    id INTEGER PRIMARY KEY, person_id INTEGER REFERENCES people(id),
    emb BLOB NOT NULL, crop BLOB, quality REAL,
    source TEXT NOT NULL CHECK(source IN ('manual', 'auto', 'unknown')),
    photo_key TEXT, photo_date TEXT, device TEXT, session_id INTEGER, added TEXT);
CREATE INDEX IF NOT EXISTS prints_person ON prints(person_id);
CREATE INDEX IF NOT EXISTS prints_key ON prints(photo_key);
CREATE TABLE IF NOT EXISTS seen(
    device TEXT, path TEXT, size INTEGER, mtime REAL, sha1 TEXT, PRIMARY KEY(device, path));
CREATE TABLE IF NOT EXISTS photo_cache(
    sha1 TEXT PRIMARY KEY, date TEXT, status TEXT NOT NULL, faces BLOB);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY, started TEXT, finished TEXT, device TEXT, folder TEXT, mode TEXT,
    status TEXT, report TEXT, backup TEXT);
CREATE TABLE IF NOT EXISTS session_photos(
    session_id INTEGER, sha1 TEXT, path TEXT, PRIMARY KEY(session_id, sha1));
CREATE TABLE IF NOT EXISTS log(
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, session_id INTEGER, text TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def device_name() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname() or "computer"


@dataclass
class Person:
    id: int
    name: str | None
    kind: str
    manual: int
    auto: int
    seen_count: int
    last_seen: str | None
    devices: list[str]
    level: str = ""          # weak / medium / strong (how well the program knows them)
    looks: int = 0
    note: str = ""           # e.g. "isolated from Ahmed"

    @property
    def label(self) -> str:
        return self.name or f"Unknown_{self.id:04d}"

    @property
    def prints(self) -> int:
        return self.manual + self.auto


class Library:
    """Open (or create) the library in `folder`.

    Raises store.MultipleLibrariesError if the folder has several library files and `file` is not
    given, and store.LockedError if it has a password and `password` is missing or wrong (the
    recovery key works as a password too).
    """

    def __init__(self, folder: Path = LIBRARY_DIR, password: str | None = None, file: Path | None = None,
                 create: bool = True):
        self.folder = Path(folder).resolve()
        files = [Path(file)] if file else store.find_libraries(self.folder)
        if len(files) > 1:
            raise MultipleLibrariesError(files)
        if not files:
            if not create:
                raise FileNotFoundError(f"No library in {self.folder}")
            self.folder.mkdir(parents=True, exist_ok=True)
            files = [self.folder / store.dated_name(store.today(), store.today())]
        self.path = files[0].resolve()
        self.folder = self.path.parent
        self.master: bytes | None = None
        if self.path.exists() and store.is_encrypted(self.path):
            if not password:
                raise LockedError("This library has a password.")
            self.master = store.unlock(self.path, password)
        self.db = store.connect(self.path, self.master)
        self.db.executescript(SCHEMA)
        self._upgrade()
        self.lock = threading.RLock()
        self._known = None          # cached prints of known people for matching
        self.session_id: int | None = None
        self._dated_name()

    def _upgrade(self) -> None:
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(people)")}
        if "note" not in cols:
            self.db.execute("ALTER TABLE people ADD COLUMN note TEXT")
        if not self.db.execute("SELECT 1 FROM meta WHERE key='created'").fetchone():
            created = (store.parse_name(self.path) or (store.today(),))[0]
            self.db.execute("INSERT INTO meta(key, value) VALUES('created', ?)", (created,))
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('model', 'sface')")
        self.db.commit()

    # ---------------------------------------------------------------- file name
    @property
    def created(self) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key='created'").fetchone()
        return row[0] if row else store.today()

    @property
    def updated(self) -> str:
        parsed = store.parse_name(self.path)
        return parsed[1] if parsed else self.created

    @property
    def encrypted(self) -> bool:
        return self.master is not None

    def _dated_name(self, updated: str | None = None) -> None:
        """Give the file its dated name (created / last update). Called when the library changes."""
        want = store.dated_name(self.created, updated or self.updated)
        if self.path.name == want:
            return
        target = self.folder / want
        if target.exists():
            return
        self.db.commit()
        self.db.close()
        try:
            os.replace(self.path, target)
            if store.key_path(self.path).exists():
                os.replace(store.key_path(self.path), store.key_path(target))
            self.path = target
        except OSError:
            pass  # e.g. the file is busy: keep the old name and try next time
        self.db = store.connect(self.path, self.master)

    def touch(self) -> None:
        """The library changed today: put today's date in its name (once a day)."""
        if self.updated != store.today():
            with self.lock:
                self._dated_name(store.today())

    # ------------------------------------------------------------------ log
    def log(self, kind: str, text: str, session_id: int | None = None) -> None:
        self.db.execute("INSERT INTO log(at, kind, session_id, text) VALUES(?,?,?,?)",
                        (_now(), kind, session_id if session_id is not None else self.session_id, text))
        self.db.commit()

    def log_entries(self, kinds: tuple[str, ...] = ("history", "memory", "run"), limit: int = 3000):
        q = ",".join("?" * len(kinds))
        return self.db.execute(f"SELECT at, kind, text FROM log WHERE kind IN ({q}) ORDER BY id DESC LIMIT ?",
                               (*kinds, limit)).fetchall()

    def export_log(self, dest: Path) -> None:
        rows = self.db.execute("SELECT at, kind, text FROM log ORDER BY id").fetchall()
        Path(dest).write_text("\n".join(f"{a}  [{k}]  {t}" for a, k, t in rows), encoding="utf-8")

    def changed(self) -> None:
        self.db.commit()
        self._known = None
        self.touch()

    # -------------------------------------------------------------- backups
    def backup(self, reason: str) -> Path:
        """A full copy in backups/, locked with the same password as the library."""
        folder = self.folder / "backups"
        folder.mkdir(exist_ok=True)
        dst = folder / f"library_{datetime.now():%Y%m%d_%H%M%S}_{reason}.db"
        store.export_to(self.db, dst, self.master)
        for old in sorted(folder.glob("library_*.db"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        return dst

    def backups(self) -> list[Path]:
        return sorted((self.folder / "backups").glob("library_*.db"), reverse=True)

    def restore(self, backup: Path) -> None:
        with self.lock:
            self.db.commit()
            self.db.close()
            shutil.copy2(backup, self.path)
            self.db = store.connect(self.path, self.master)
            self._known = None
            self.log("history", f"Library restored from backup {Path(backup).name}")

    # ------------------------------------------------------------- password
    def set_password(self, password: str) -> str:
        """Lock the library with a password. Returns the recovery key (show it once!)."""
        with self.lock:
            if not store.HAS_CIPHER:
                raise RuntimeError("This copy of the program cannot encrypt libraries.")
            if self.master:
                store.rewrap_password(self.path, self.master, password)
                self.log("history", "Password changed")
                return ""
            master = os.urandom(32)
            recovery = store.new_recovery_key()
            tmp = self.path.with_suffix(".encrypting")
            store.export_to(self.db, tmp, master)
            store.write_keyfile(self.path, master, password, recovery)
            self._reencrypt_backups(None, master)
            self.db.close()
            os.replace(tmp, self.path)
            self.master = master
            self.db = store.connect(self.path, self.master)
            self.log("history", "Password set: the library is now encrypted")
            return recovery

    def remove_password(self) -> None:
        with self.lock:
            if not self.master:
                return
            tmp = self.path.with_suffix(".decrypting")
            store.export_to(self.db, tmp, None)
            self._reencrypt_backups(self.master, None)
            self.db.close()
            os.replace(tmp, self.path)
            store.key_path(self.path).unlink(missing_ok=True)
            self.master = None
            self.db = store.connect(self.path, None)
            self.log("history", "Password removed: the library is no longer encrypted")

    def _reencrypt_backups(self, old: bytes | None, new: bytes | None) -> None:
        for b in self.backups():
            try:
                conn = store.connect(b, old)
                tmp = b.with_suffix(".tmp")
                store.export_to(conn, tmp, new)
                conn.close()
                os.replace(tmp, b)
            except (LockedError, OSError, store.DatabaseError):
                b.unlink(missing_ok=True)  # a backup we cannot convert would be unreadable later

    # -------------------------------------------------------------- people
    def people(self, kind: str | None = "known") -> list[Person]:
        sql = """SELECT p.id, p.name, p.kind,
                   SUM(CASE WHEN r.source IN ('manual','unknown') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.source='auto' THEN 1 ELSE 0 END),
                   p.seen_count, p.last_seen, p.devices, p.note
                 FROM people p LEFT JOIN prints r ON r.person_id=p.id"""
        args: tuple = ()
        if kind:
            sql += " WHERE p.kind=?"
            args = (kind,)
        sql += " GROUP BY p.id"
        out = []
        for pid, name, k, m, a, seen, last, devs, note in self.db.execute(sql, args).fetchall():
            p = Person(pid, name, k, m or 0, a or 0, seen or 0, last, [d for d in (devs or "").split("|") if d],
                       note=note or "")
            p.looks = self.count_looks(pid)
            n = p.prints
            p.level = "weak" if n < 5 else ("strong" if n >= 25 and p.looks >= 3 else "medium")
            out.append(p)
        if kind == "unknown":
            out.sort(key=lambda p: (-p.seen_count, -len(p.devices), p.id))
        else:
            out.sort(key=lambda p: ((p.name or "").lower(), p.id))
        return out

    def person(self, pid: int) -> Person | None:
        for p in self.people(None):
            if p.id == pid:
                return p
        return None

    def names(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT name FROM people WHERE kind='known' AND name IS NOT NULL ORDER BY name COLLATE NOCASE")]

    def person_by_name(self, name: str) -> int | None:
        row = self.db.execute("SELECT id FROM people WHERE name=?", (name.strip(),)).fetchone()
        return row[0] if row else None

    def stats(self) -> dict:
        def q(sql: str) -> int:
            return self.db.execute(sql).fetchone()[0] or 0
        return {
            "known": q("SELECT COUNT(*) FROM people WHERE kind='known'"),
            "unknown": q("SELECT COUNT(*) FROM people WHERE kind='unknown'"),
            "prints": q("SELECT COUNT(*) FROM prints WHERE source IN ('manual','auto')"),
            "loose": q("SELECT COUNT(*) FROM prints WHERE person_id IS NULL"),
            "sessions": q("SELECT COUNT(*) FROM sessions WHERE status='saved'"),
            "devices": q("SELECT COUNT(DISTINCT device) FROM sessions WHERE status='saved'"),
            "size_mb": round(self.path.stat().st_size / 1e6, 1) if self.path.exists() else 0,
        }

    def prints_of(self, pid: int) -> list[tuple]:
        """(print id, crop jpg, date, source, quality, device) of a person, oldest first (timeline)."""
        return self.db.execute(
            "SELECT id, crop, photo_date, source, quality, device FROM prints WHERE person_id=? "
            "ORDER BY COALESCE(photo_date, '9999'), id", (pid,)).fetchall()

    def thumb(self, pid: int) -> np.ndarray | None:
        row = self.db.execute("SELECT crop FROM prints WHERE person_id=? AND crop IS NOT NULL "
                              "ORDER BY (source='manual') DESC, quality DESC LIMIT 1", (pid,)).fetchone()
        return decode_jpg(row[0]) if row else None

    def _embs(self, pid: int) -> np.ndarray:
        rows = self.db.execute("SELECT emb FROM prints WHERE person_id=?", (pid,)).fetchall()
        return np.array([np.frombuffer(r[0], np.float32) for r in rows], np.float32).reshape(-1, 128)

    def count_looks(self, pid: int) -> int:
        e = self._embs(pid)
        if len(e) < 2:
            return len(e)
        labels = cluster(e[-300:], LOOK_SIM)
        counts = np.bincount(labels)
        return int((counts >= 2).sum()) or 1

    def looks(self, pid: int) -> list[list[int]]:
        """Print ids grouped into this person's different looks (biggest first)."""
        rows = self.db.execute("SELECT id, emb FROM prints WHERE person_id=?", (pid,)).fetchall()
        if not rows:
            return []
        e = np.array([np.frombuffer(r[1], np.float32) for r in rows])
        labels = cluster(e, LOOK_SIM)
        groups: dict[int, list[int]] = {}
        for r, lab in zip(rows, labels):
            groups.setdefault(int(lab), []).append(r[0])
        return sorted(groups.values(), key=len, reverse=True)

    # ------------------------------------------------------------- matching
    def _known_matrix(self):
        if self._known is None:
            rows = self.db.execute("""SELECT r.person_id, r.emb, r.source FROM prints r
                                      JOIN people p ON p.id=r.person_id WHERE p.kind='known'""").fetchall()
            pids = np.array([r[0] for r in rows], int)
            embs = np.array([np.frombuffer(r[1], np.float32) for r in rows], np.float32).reshape(-1, 128)
            manual = np.array([r[2] == "manual" for r in rows], bool)
            names = dict(self.db.execute("SELECT id, name FROM people WHERE kind='known'").fetchall())
            self._known = (pids, embs, manual, names)
        return self._known

    def match_known(self, embs: np.ndarray, top: int = 3) -> list[list[tuple[int, str, float]]]:
        """For each face: the `top` best known people [(person id, name, score)], best first.

        The score is the mean of the face's 3 best similarities to the person, but never more than
        0.05 above its best similarity to a print you confirmed yourself (so things the program
        learned by itself can never pull a face towards someone).
        """
        pids, prints, manual, names = self._known_matrix()
        out: list[list[tuple[int, str, float]]] = [[] for _ in range(len(embs))]
        if not len(prints) or not len(embs):
            return out
        sims = embs @ prints.T
        scores = []
        for pid in np.unique(pids):
            cols = pids == pid
            mcols = cols & manual
            score = top_k_mean(sims[:, cols])
            if mcols.any():
                score = np.minimum(score, sims[:, mcols].max(axis=1) + 0.05)
            scores.append((pid, score))
        mat = np.stack([s for _, s in scores], axis=1)
        order = np.argsort(-mat, axis=1)[:, :top]
        for i in range(len(embs)):
            out[i] = [(int(scores[j][0]), names.get(int(scores[j][0])), float(mat[i, j])) for j in order[i]]
        return out

    def match_groups(self, embs: np.ndarray, kind: str = "unknown") -> list[tuple[int | None, float]]:
        """Best existing group of `kind` for each face (top-3 mean similarity)."""
        rows = self.db.execute("""SELECT r.person_id, r.emb FROM prints r JOIN people p ON p.id=r.person_id
                                  WHERE p.kind=?""", (kind,)).fetchall()
        out: list[tuple[int | None, float]] = [(None, 0.0)] * len(embs)
        if not rows or not len(embs):
            return out
        pids = np.array([r[0] for r in rows])
        pe = np.array([np.frombuffer(r[1], np.float32) for r in rows])
        sims = embs @ pe.T
        best_p = np.full(len(embs), -1)
        best_s = np.zeros(len(embs))
        for p in np.unique(pids):
            s = top_k_mean(sims[:, pids == p])
            better = s > best_s
            best_p[better] = p
            best_s[better] = s[better]
        return [(int(p) if p >= 0 else None, float(s)) for p, s in zip(best_p, best_s)]

    def loose_faces(self, limit: int = 3000) -> list[tuple[int, np.ndarray, str]]:
        rows = self.db.execute("SELECT id, emb, photo_key FROM prints WHERE person_id IS NULL "
                               "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [(r[0], np.frombuffer(r[1], np.float32), r[2]) for r in rows]

    # --------------------------------------------------------------- learning
    def new_person(self, name: str | None, kind: str) -> int:
        pid = self.db.execute("INSERT INTO people(name, kind, created) VALUES(?,?,?)",
                              (name, kind, _now())).lastrowid
        return pid

    def person_for_name(self, name: str) -> int:
        pid = self.person_by_name(name)
        if pid is None:
            pid = self.new_person(name.strip(), "known")
            self.log("memory", f"New person '{name.strip()}'")
        return pid

    def add_prints(self, pid: int | None, faces: list[dict], source: str, session_id: int | None = None) -> int:
        """Learn faces for a person (pid None = loose unknown faces). Each face dict has
        emb, crop, quality, key, date, device. Faces that add nothing new are skipped."""
        if not faces:
            return 0
        have = self._embs(pid) if pid is not None else np.zeros((0, 128), np.float32)
        keys = {r[0] for r in self.db.execute("SELECT photo_key FROM prints WHERE person_id IS ?", (pid,))}
        added = 0
        for f in faces:
            if f["key"] in keys:
                continue  # this photo already taught this person
            emb = np.asarray(f["emb"], np.float32)
            if len(have) >= 10 and source != "unknown" and float((have @ emb).max()) >= REDUNDANT:
                continue  # nothing new: almost the same as a print we have
            self.db.execute("INSERT INTO prints(person_id, emb, crop, quality, source, photo_key, photo_date, device, "
                            "session_id, added) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (pid, emb.tobytes(), f.get("crop"), f.get("quality"), source, f["key"], f.get("date"),
                             f.get("device"), session_id, _now()))
            have = np.vstack([have, emb[None, :]])
            keys.add(f["key"])
            added += 1
        self._trim(pid)
        self._known = None
        return added

    def _trim(self, pid: int | None) -> None:
        """Keep each person's prints varied and within limits: drop the most redundant ones."""
        if pid is None:
            ids = [r[0] for r in self.db.execute("SELECT id FROM prints WHERE person_id IS NULL ORDER BY id")]
            if len(ids) > MAX_LOOSE:
                self.db.executemany("DELETE FROM prints WHERE id=?", [(i,) for i in ids[:len(ids) - MAX_LOOSE]])
            return
        for source, limit, keep_first in (("manual", MAX_MANUAL, KEEP_FIRST), ("auto", MAX_AUTO, 0),
                                          ("unknown", MAX_UNKNOWN, 0)):
            rows = self.db.execute("SELECT id, emb FROM prints WHERE person_id=? AND source=? ORDER BY id",
                                   (pid, source)).fetchall()
            extra = len(rows) - limit
            if extra <= 0:
                continue
            e = np.array([np.frombuffer(r[1], np.float32) for r in rows])
            sims = e @ e.T
            np.fill_diagonal(sims, -1)
            redundancy = sims.max(axis=1)
            redundancy[:keep_first] = -2  # never drop the first ones you taught
            drop = np.argsort(-redundancy)[:extra]
            self.db.executemany("DELETE FROM prints WHERE id=?", [(rows[i][0],) for i in drop])

    def note_seen(self, pid: int, photos: int, device: str) -> None:
        row = self.db.execute("SELECT devices FROM people WHERE id=?", (pid,)).fetchone()
        devs = [d for d in (row[0] or "").split("|") if d] if row else []
        if device not in devs:
            devs.append(device)
        self.db.execute("UPDATE people SET seen_count=seen_count+?, last_seen=?, devices=? WHERE id=?",
                        (photos, _now()[:10], "|".join(devs), pid))

    # ------------------------------------------------------------- changes
    def rename(self, pid: int, new_name: str) -> str:
        """Name / rename a person. If the name already exists, the two are merged."""
        with self.lock:
            new_name = new_name.strip()
            row = self.db.execute("SELECT name, kind FROM people WHERE id=?", (pid,)).fetchone()
            if not row or not new_name or new_name == row[0]:
                return ""
            label = row[0] or f"Unknown_{pid:04d}"
            target = self.person_by_name(new_name)
            if target and target != pid:
                msg = self.merge(pid, target)
            else:
                self.db.execute("UPDATE people SET name=?, kind='known', note=NULL WHERE id=?", (new_name, pid))
                if row[1] == "unknown":  # you named it: its faces are now confirmed
                    self.db.execute("UPDATE prints SET source='manual' WHERE person_id=?", (pid,))
                msg = f"'{label}' is now '{new_name}'"
            self.log("memory", msg)
            self.changed()
            return msg

    def merge(self, pid: int, into: int) -> str:
        with self.lock:
            a = self.db.execute("SELECT name, kind, seen_count, devices FROM people WHERE id=?", (pid,)).fetchone()
            b = self.db.execute("SELECT name, devices FROM people WHERE id=?", (into,)).fetchone()
            if not a or not b or pid == into:
                return ""
            src = "manual" if a[1] == "unknown" else None
            if src:
                self.db.execute("UPDATE prints SET source='manual' WHERE person_id=?", (pid,))
            # the same photo should teach a person only once
            self.db.execute("DELETE FROM prints WHERE person_id=? AND photo_key IN "
                            "(SELECT photo_key FROM prints WHERE person_id=?)", (pid, into))
            self.db.execute("UPDATE prints SET person_id=? WHERE person_id=?", (into, pid))
            devs = sorted({d for d in ((a[3] or "") + "|" + (b[1] or "")).split("|") if d})
            self.db.execute("UPDATE people SET seen_count=seen_count+?, devices=? WHERE id=?",
                            (a[2] or 0, "|".join(devs), into))
            self.db.execute("DELETE FROM people WHERE id=?", (pid,))
            self._trim(into)
            msg = f"'{a[0] or f'Unknown_{pid:04d}'}' merged into '{b[0] or f'Unknown_{into:04d}'}'"
            self.log("memory", msg)
            self.changed()
            return msg

    def delete_person(self, pid: int) -> str:
        with self.lock:
            row = self.db.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()
            if not row:
                return ""
            n = self.db.execute("DELETE FROM prints WHERE person_id=?", (pid,)).rowcount
            self.db.execute("DELETE FROM people WHERE id=?", (pid,))
            msg = f"Deleted '{row[0] or f'Unknown_{pid:04d}'}' ({n} faces)"
            self.log("memory", msg)
            self.changed()
            return msg

    def remove_prints(self, print_ids: list[int]) -> str:
        """'Not this person': these faces leave the person and become unknown faces."""
        with self.lock:
            if not print_ids:
                return ""
            q = ",".join("?" * len(print_ids))
            row = self.db.execute(f"SELECT p.name FROM prints r JOIN people p ON p.id=r.person_id "
                                  f"WHERE r.id IN ({q}) LIMIT 1", print_ids).fetchone()
            n = self.db.execute(f"UPDATE prints SET person_id=NULL, source='unknown' WHERE id IN ({q})",
                                print_ids).rowcount
            msg = f"{n} face(s) removed from '{row[0] if row else '?'}'"
            self.log("memory", msg)
            self.changed()
            return msg

    def move_prints(self, print_ids: list[int], name: str) -> str:
        """'These faces are <name>'."""
        with self.lock:
            if not print_ids or not name.strip():
                return ""
            target = self.person_for_name(name)
            q = ",".join("?" * len(print_ids))
            n = self.db.execute(f"UPDATE prints SET person_id=?, source='manual' WHERE id IN ({q})",
                                [target, *print_ids]).rowcount
            self._trim(target)
            msg = f"{n} face(s) moved to '{name.strip()}'"
            self.log("memory", msg)
            self.changed()
            return msg

    def reset_auto(self, pid: int | None = None) -> int:
        with self.lock:
            if pid:
                n = self.db.execute("DELETE FROM prints WHERE source='auto' AND person_id=?", (pid,)).rowcount
            else:
                n = self.db.execute("DELETE FROM prints WHERE source='auto'").rowcount
            self.log("memory", f"Reset automatic learning ({n} faces)")
            self.changed()
            return n

    # ------------------------------------------------------------- insights
    def duplicates(self, threshold: float = 0.50) -> list[tuple[int, int, float]]:
        """Pairs of known people that look like the same person: (id a, id b, score).
        Pairs you said are different people are not shown again."""
        people = [(p.id, self._embs(p.id)) for p in self.people("known")]
        people = [(pid, e) for pid, e in people if len(e)]
        ignored = self._not_duplicates()
        out = []
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                a, ea = people[i]
                b, eb = people[j]
                if (min(a, b), max(a, b)) in ignored:
                    continue
                s = float(np.median(top_k_mean(ea @ eb.T)))
                if s >= threshold:
                    out.append((a, b, s))
        return sorted(out, key=lambda t: -t[2])

    def _not_duplicates(self) -> set[tuple[int, int]]:
        row = self.db.execute("SELECT value FROM meta WHERE key='not_duplicates'").fetchone()
        return {tuple(x) for x in json.loads(row[0])} if row else set()

    def not_duplicate(self, a: int, b: int) -> None:
        pairs = self._not_duplicates() | {(min(a, b), max(a, b))}
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('not_duplicates', ?)",
                        (json.dumps(sorted(pairs)),))
        self.db.commit()

    def frequent_unknowns(self, min_seen: int = 5) -> list[Person]:
        return [p for p in self.people("unknown") if p.seen_count >= min_seen or len(p.devices) >= 2]

    # ------------------------------------------------------------- sessions
    def sessions(self) -> list[tuple]:
        return self.db.execute("SELECT id, started, device, folder, mode, status, report FROM sessions "
                               "ORDER BY id DESC").fetchall()

    def last_saved_session(self) -> tuple | None:
        return self.db.execute("SELECT id, backup, report FROM sessions WHERE status='saved' "
                               "ORDER BY id DESC LIMIT 1").fetchone()

    def undo_last_session(self) -> str:
        """Put the library back exactly as it was before the last saved session."""
        with self.lock:
            row = self.last_saved_session()
            if not row or not row[1] or not Path(row[1]).exists():
                return "Nothing to undo."
            sid, backup, _ = row
            sessions_after = self.db.execute("SELECT id, started, finished, device, folder, mode, report "
                                             "FROM sessions WHERE id>=?", (sid,)).fetchall()
            self.restore(Path(backup))
            # keep the record that it happened
            for s_id, started, finished, device, folder, mode, report in sessions_after:
                self.db.execute("INSERT OR REPLACE INTO sessions(id, started, finished, device, folder, mode, status, "
                                "report) VALUES(?,?,?,?,?,?,?,?)",
                                (s_id, started, finished, device, folder, mode, "undone", report))
            self.db.commit()
            msg = f"Session {sid} was undone; the library is back as it was before it."
            self.log("history", msg)
            return msg

    def save_face(self, name: str, face: dict) -> str:
        """Keep one face (from "Who is this?") as a confirmed face of `name`."""
        with self.lock:
            name = name.strip()
            if not name:
                return ""
            pid = self.person_for_name(name)
            n = self.add_prints(pid, [face], "manual")
            self.note_seen(pid, 1, face.get("device") or device_name())
            msg = f"Face saved for '{name}'" if n else f"'{name}' already has this face"
            self.log("memory", msg)
            self.changed()
            return msg

    # ---------------------------------------------------- managing people
    def isolate(self, pids: list[int]) -> str:
        """Take people out of the known list: their faces go back to the unknown people, to be named again.
        The old name is no longer used for recognition."""
        with self.lock:
            done = []
            for pid in pids:
                row = self.db.execute("SELECT name, kind FROM people WHERE id=?", (pid,)).fetchone()
                if not row or row[1] != "known":
                    continue
                self.db.execute("UPDATE people SET name=NULL, kind='unknown', note=? WHERE id=?",
                                (f"isolated from {row[0]}", pid))
                self.db.execute("UPDATE prints SET source='unknown' WHERE person_id=?", (pid,))
                done.append(row[0])
            if not done:
                return ""
            msg = "Isolated (back to unknown, to be named again): " + ", ".join(done)
            self.log("memory", msg)
            self.changed()
            return msg

    def merge_many(self, pids: list[int], name: str) -> str:
        """Join several people into one, called `name`."""
        with self.lock:
            name = name.strip()
            if len(pids) < 2 or not name:
                return ""
            target = self.person_by_name(name)
            if target is None:
                target = pids[0]
                self.rename(target, name)
            for pid in pids:
                if pid != target:
                    self.merge(pid, target)
            msg = f"{len(pids)} people joined as '{name}'"
            self.log("memory", msg)
            self.changed()
            return msg

    def export_people(self, pids: list[int], dest_folder: Path) -> Path:
        """A new, separate library (no password) holding only these people."""
        with self.lock:
            dest_folder = Path(dest_folder)
            dest_folder.mkdir(parents=True, exist_ok=True)
            if store.find_libraries(dest_folder):
                raise RuntimeError(f"There is already a library in {dest_folder}")
            out = Library(dest_folder)
            for pid in pids:
                row = self.db.execute("SELECT name, kind, created, last_seen, seen_count, devices, note FROM people "
                                      "WHERE id=?", (pid,)).fetchone()
                if not row:
                    continue
                new_id = out.db.execute("INSERT INTO people(name, kind, created, last_seen, seen_count, devices, "
                                        "note) VALUES(?,?,?,?,?,?,?)", row).lastrowid
                rows = self.db.execute("SELECT emb, crop, quality, source, photo_key, photo_date, device, added "
                                       "FROM prints WHERE person_id=?", (pid,)).fetchall()
                out.db.executemany("INSERT INTO prints(person_id, emb, crop, quality, source, photo_key, photo_date, "
                                   "device, added) VALUES(?,?,?,?,?,?,?,?,?)", [(new_id, *r) for r in rows])
            out.log("history", f"Created by exporting {len(pids)} people from {self.path.name}")
            out.db.commit()
            path = out.path
            out.close()
            self.log("history", f"Exported {len(pids)} people to {path}")
            return path

    def merge_from(self, other: Path, password: str | None = None) -> str:
        """Bring another library (folder or file) into this one. The other library is not changed."""
        with self.lock:
            other = Path(other)
            o = Library(other.parent, password, file=other) if other.is_file() else Library(other, password,
                                                                                          create=False)
            if o.path == self.path:
                o.close()
                raise RuntimeError("That is this library.")
            self.backup("before_merge")
            try:
                people = added = 0
                for pid, name, kind, created, last, seen, devs, note in o.db.execute(
                        "SELECT id, name, kind, created, last_seen, seen_count, devices, note FROM people").fetchall():
                    target = self.person_by_name(name) if name else None
                    if target is None:
                        target = self.db.execute("INSERT INTO people(name, kind, created, last_seen, seen_count, "
                                                 "devices, note) VALUES(?,?,?,?,?,?,?)",
                                                 (name, kind, created, last, seen, devs, note)).lastrowid
                        people += 1
                    else:
                        for d in (devs or "").split("|"):
                            if d:
                                self.note_seen(target, 0, d)
                    keys = {r[0] for r in self.db.execute("SELECT photo_key FROM prints WHERE person_id=?",
                                                          (target,))}
                    for row in o.db.execute("SELECT emb, crop, quality, source, photo_key, photo_date, device, added "
                                            "FROM prints WHERE person_id=?", (pid,)).fetchall():
                        if row[4] in keys:
                            continue
                        self.db.execute("INSERT INTO prints(person_id, emb, crop, quality, source, photo_key, "
                                        "photo_date, device, added) VALUES(?,?,?,?,?,?,?,?,?)", (target, *row))
                        added += 1
                    self._trim(target)
                for row in o.db.execute("SELECT emb, crop, quality, source, photo_key, photo_date, device, added "
                                        "FROM prints WHERE person_id IS NULL").fetchall():
                    self.db.execute("INSERT INTO prints(person_id, emb, crop, quality, source, photo_key, photo_date, "
                                    "device, added) VALUES(NULL,?,?,?,?,?,?,?,?)", row)
                self._trim(None)
                self.db.executemany("INSERT OR IGNORE INTO photo_cache(sha1, date, status, faces) VALUES(?,?,?,?)",
                                    o.db.execute("SELECT sha1, date, status, faces FROM photo_cache").fetchall())
                self.db.executemany("INSERT OR IGNORE INTO seen(device, path, size, mtime, sha1) VALUES(?,?,?,?,?)",
                                    o.db.execute("SELECT device, path, size, mtime, sha1 FROM seen").fetchall())
                msg = f"Merged {o.path.name}: {people} new people, {added} faces added"
            finally:
                o.close()
            self.log("history", msg)
            self.changed()
            return msg

    def copy_to(self, dest_parent: Path) -> Path:
        """Save a dated copy in another place: <dest_parent>/FaceSorter_Library_copy_<date>_<time>/.
        The copy has the same password. This library stays where it is."""
        with self.lock:
            folder = Path(dest_parent) / f"FaceSorter_Library_copy_{datetime.now():%Y-%m-%d_%H%M}"
            folder.mkdir(parents=True, exist_ok=False)
            dst = folder / self.path.name
            store.export_to(self.db, dst, self.master)
            if self.master:
                shutil.copy2(store.key_path(self.path), store.key_path(dst))
            self.log("history", f"Copy saved to {folder}")
            return folder

    def move_to(self, dest_folder: Path) -> Path:
        """Move the library (with its backups) to `dest_folder`; it becomes this library's place."""
        with self.lock:
            dest_folder = Path(dest_folder).resolve()
            if dest_folder == self.folder:
                return self.folder
            dest_folder.mkdir(parents=True, exist_ok=True)
            if store.find_libraries(dest_folder):
                raise RuntimeError(f"There is already a library in {dest_folder}")
            dst = dest_folder / self.path.name
            store.export_to(self.db, dst, self.master)
            if self.master:
                shutil.copy2(store.key_path(self.path), store.key_path(dst))
            if (self.folder / "backups").is_dir():
                shutil.copytree(self.folder / "backups", dest_folder / "backups", dirs_exist_ok=True)
            check = store.connect(dst, self.master)  # make sure the new copy opens before removing the old one
            check.close()
            old_path, old_folder = self.path, self.folder
            self.db.close()
            old_path.unlink()
            store.key_path(old_path).unlink(missing_ok=True)
            shutil.rmtree(old_folder / "backups", ignore_errors=True)
            try:
                old_folder.rmdir()
            except OSError:
                pass
            self.path, self.folder = dst, dest_folder
            self.db = store.connect(self.path, self.master)
            self.log("history", f"Library moved from {old_folder} to {dest_folder}")
            return dest_folder

    def size_info(self) -> dict:
        def q(sql: str) -> int:
            return self.db.execute(sql).fetchone()[0] or 0
        mb = 1024 * 1024
        backups = sum(b.stat().st_size for b in self.backups())
        return {
            "file": round(self.path.stat().st_size / mb, 1),
            "people": round(q("SELECT SUM(LENGTH(emb)+LENGTH(COALESCE(crop,''))) FROM prints r JOIN people p "
                              "ON p.id=r.person_id WHERE p.kind='known'") / mb, 1),
            "unknown": round(q("SELECT SUM(LENGTH(emb)+LENGTH(COALESCE(crop,''))) FROM prints r LEFT JOIN people p "
                               "ON p.id=r.person_id WHERE p.kind IS NULL OR p.kind='unknown'") / mb, 1),
            "cache": round(q("SELECT SUM(LENGTH(COALESCE(faces,''))) FROM photo_cache") / mb, 1),
            "cache_photos": q("SELECT COUNT(*) FROM photo_cache"),
            "backups": round(backups / mb, 1),
        }

    def clean(self) -> str:
        """Delete the remembered scan results of photos (keeps every person and face). Photos met again
        are simply checked again."""
        with self.lock:
            before = self.path.stat().st_size
            pending = "SELECT sha1 FROM session_photos WHERE session_id IN " \
                      "(SELECT id FROM sessions WHERE status IN ('scanning','review'))"
            n = self.db.execute(f"DELETE FROM photo_cache WHERE sha1 NOT IN ({pending})").rowcount
            self.db.execute("DELETE FROM seen WHERE sha1 NOT IN (SELECT sha1 FROM photo_cache)")
            self.db.execute("DELETE FROM session_photos WHERE session_id NOT IN "
                            "(SELECT id FROM sessions WHERE status IN ('scanning','review'))")
            self.db.commit()
            self.db.execute("VACUUM")
            freed = max(0, before - self.path.stat().st_size) / (1024 * 1024)
            msg = f"Cleaned: forgot the scan results of {n} photos, freed {freed:.1f} MB"
            self.log("history", msg)
            self.changed()
            return msg

    def close(self) -> None:
        self.db.commit()
        self.db.close()


def cache_pack(faces) -> bytes:
    return pickle.dumps(faces, protocol=4)


def cache_unpack(blob: bytes | None) -> list:
    return pickle.loads(blob) if blob else []


def copy_file(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / src.name
    if dst.exists():
        i = 2
        while (folder / f"{src.stem}_{i}{src.suffix}").exists():
            i += 1
        dst = folder / f"{src.stem}_{i}{src.suffix}"
    shutil.copy2(src, dst)
    return dst
