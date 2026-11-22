"""Tests of the library: everything that changes or protects what the program knows about people.

    python -m unittest discover -s tests -v

They use made-up faces (random 128-number prints), so they need no AI models and no photos.
tests/data/v3.1_library.db was made by Face Sorter v3.1 with made-up faces: it checks that a
library from an older version still opens and keeps everything.
"""
from __future__ import annotations

import os
import pickle
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facesorter import store  # noqa: E402
from facesorter.engine import encode_jpg  # noqa: E402
from facesorter.library import Library, LockedError, MultipleLibrariesError, cache_pack, cache_unpack  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
rng = np.random.default_rng(1)


def base() -> np.ndarray:
    return rng.normal(0, 1, 128).astype(np.float32)


def face(b: np.ndarray, key: str, noise: float = 0.15) -> dict:
    v = b + rng.normal(0, noise, 128).astype(np.float32)
    return {"emb": (v / np.linalg.norm(v)).astype(np.float32), "key": key, "quality": 0.9, "date": "2025-01-01",
            "device": "TEST-PC", "crop": encode_jpg(np.full((112, 112, 3), 120, np.uint8), 75)}


class LibraryCase(unittest.TestCase):
    """A fresh, empty library in a temporary folder for each test."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.lib = Library(self.dir / "Library")

    def tearDown(self) -> None:
        try:
            self.lib.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def person(self, name: str | None, n: int = 4, b: np.ndarray | None = None) -> int:
        b = base() if b is None else b
        pid = self.lib.new_person(name, "known" if name else "unknown")
        self.lib.add_prints(pid, [face(b, f"{name}-{pid}-{k}") for k in range(n)], "manual" if name else "unknown")
        self.lib.note_seen(pid, n, "TEST-PC")
        self.lib.changed()
        return pid


class LibraryTest(LibraryCase):
    # ------------------------------------------------------------ the file
    def test_new_library_has_a_dated_name(self) -> None:
        self.assertTrue(store.parse_name(self.lib.path), self.lib.path.name)
        self.assertEqual(self.lib.created, store.today())

    def test_two_library_files_in_one_folder_ask_which(self) -> None:
        shutil.copy2(self.lib.path, self.lib.folder / "FaceSorter_Library_created-2020-01-01_updated-2020-01-01.db")
        with self.assertRaises(MultipleLibrariesError):
            Library(self.lib.folder)

    def test_open_without_creating(self) -> None:
        with self.assertRaises(FileNotFoundError):
            Library(self.dir / "empty", create=False)

    # ------------------------------------------------------------ people
    def test_rename_and_join_by_name(self) -> None:
        a, b = self.person("Mona"), self.person(None)
        self.lib.rename(b, "Mona")  # an existing name: the two become one
        self.assertEqual([p.name for p in self.lib.people("known")], ["Mona"])
        self.assertEqual(self.lib.person(a).prints, 8)
        self.assertEqual(self.lib.people("unknown"), [])

    def test_naming_an_unknown_confirms_its_faces(self) -> None:
        u = self.person(None)
        self.lib.rename(u, "Ahmed")
        p = self.lib.person(u)
        self.assertEqual((p.kind, p.manual, p.auto), ("known", 4, 0))

    def test_merge_many_and_delete(self) -> None:
        a, b, c = self.person("A"), self.person("B"), self.person("C")
        self.lib.merge_many([a, b], "AB")
        self.assertEqual(sorted(self.lib.names()), ["AB", "C"])
        self.lib.delete_person(c)
        self.assertEqual(self.lib.names(), ["AB"])

    def test_isolate_goes_back_to_unknown_with_a_note(self) -> None:
        a = self.person("Sara")
        self.lib.isolate([a])
        p = self.lib.person(a)
        self.assertEqual((p.kind, p.name), ("unknown", None))
        self.assertIn("Sara", p.note)
        self.assertNotIn("Sara", self.lib.names())

    def test_recognition_uses_confirmed_faces(self) -> None:
        b = base()
        self.person("Omar", b=b)
        self.person("Other")
        best = self.lib.match_known(np.array([face(b, "x")["emb"]]))[0][0]
        self.assertEqual(best[1], "Omar")
        self.assertGreater(best[2], 0.7)

    def test_not_this_person_and_move_faces(self) -> None:
        a = self.person("Laila")
        ids = [r[0] for r in self.lib.prints_of(a)][:2]
        self.lib.remove_prints(ids)
        self.assertEqual(self.lib.person(a).prints, 2)
        self.lib.move_prints(ids, "Nour")
        self.assertEqual(self.lib.person(self.lib.person_by_name("Nour")).prints, 2)

    def test_export_people_to_a_new_library(self) -> None:
        a, _ = self.person("Hany"), self.person("Rana")
        path = self.lib.export_people([a], self.dir / "export")
        out = Library(path.parent)
        self.assertEqual(out.names(), ["Hany"])
        self.assertEqual(out.person(out.person_by_name("Hany")).prints, 4)
        out.close()

    def test_merge_another_library(self) -> None:
        self.person("Mona")
        other = Library(self.dir / "other")
        pid = other.new_person("Mona", "known")
        other.add_prints(pid, [face(base(), f"o{k}") for k in range(3)], "manual")
        pid = other.new_person("Ziad", "known")
        other.add_prints(pid, [face(base(), f"z{k}") for k in range(2)], "manual")
        other.changed()
        other.close()
        self.lib.merge_from(self.dir / "other")
        self.assertEqual(sorted(self.lib.names()), ["Mona", "Ziad"])
        self.assertEqual(self.lib.person(self.lib.person_by_name("Mona")).prints, 7)
        self.assertTrue(self.lib.backups(), "a backup is made before merging")

    # ------------------------------------------------------------ safety
    def test_backup_and_restore(self) -> None:
        self.person("Before")
        backup = self.lib.backup("test")
        self.person("After")
        self.lib.restore(backup)
        self.assertEqual(self.lib.names(), ["Before"])

    def test_undo_last_session(self) -> None:
        self.person("Kept")
        backup = self.lib.backup("before_session")
        self.lib.db.execute("INSERT INTO sessions(id, started, device, folder, mode, status, backup) "
                            "VALUES(1, 'now', 'TEST-PC', 'x', 'learn', 'saved', ?)", (str(backup),))
        self.person("Added by the session")
        self.lib.undo_last_session()
        self.assertEqual(self.lib.names(), ["Kept"])
        self.assertEqual(self.lib.sessions()[0][5], "undone")

    def test_password_recovery_key_change_and_remove(self) -> None:
        if not store.HAS_CIPHER:
            self.skipTest("SQLCipher not installed")
        self.person("Secret")
        self.lib.backup("plain")
        key = self.lib.set_password("كلمة سر")
        folder = self.lib.folder
        self.lib.close()
        self.assertTrue(store.is_encrypted(self.lib.path))
        for bad in (None, "wrong"):
            with self.assertRaises(LockedError):
                Library(folder, bad)
        for good in ("كلمة سر", key, key.lower()):
            lib = Library(folder, good)
            self.assertEqual(lib.names(), ["Secret"])
            self.assertTrue(store.is_encrypted(lib.backups()[0]), "backups are encrypted too")
            lib.close()
        self.lib = Library(folder, key)
        self.lib.set_password("new one")  # change: the recovery key stays the same
        self.lib.close()
        Library(folder, "new one").close()
        self.lib = Library(folder, key)
        self.lib.remove_password()
        self.lib.close()
        self.assertFalse(store.is_encrypted(self.lib.path))
        self.lib = Library(folder)
        self.assertEqual(self.lib.names(), ["Secret"])

    def test_copy_and_move(self) -> None:
        self.person("Traveller")
        copy = self.lib.copy_to(self.dir / "copies")
        self.assertEqual(Library(copy).names(), ["Traveller"])
        self.lib.backup("x")
        self.lib.move_to(self.dir / "usb" / "Library")
        self.assertEqual(self.lib.folder, (self.dir / "usb" / "Library").resolve())
        self.assertTrue(self.lib.backups())
        self.assertFalse((self.dir / "Library").exists())
        self.assertEqual(self.lib.names(), ["Traveller"])

    def test_clean_keeps_people(self) -> None:
        self.person("Stays")
        self.lib.db.execute("INSERT INTO photo_cache(sha1, date, status, faces) VALUES('s', NULL, 'no_faces', NULL)")
        self.lib.db.commit()
        self.lib.clean()
        self.assertEqual(self.lib.size_info()["cache_photos"], 0)
        self.assertEqual(self.lib.names(), ["Stays"])

    # ------------------------------------------------------------ scan results stay plain data
    def test_scan_results_round_trip(self) -> None:
        emb = face(base(), "k")["emb"].tobytes()
        faces = [((1, 2, 3, 4), 0.98, emb, b"jpg", 0.7, True, ""), ((5, 6, 7, 8), 0.5, emb, b"", 0.2, False, "small")]
        self.assertEqual(cache_unpack(cache_pack(faces)), faces)

    def test_a_library_file_cannot_run_code(self) -> None:
        marker = self.dir / "hacked.txt"

        class Evil:
            def __reduce__(self):
                return (os.system, (f'echo hacked > "{marker}"',))
        self.assertEqual(cache_unpack(pickle.dumps([Evil()])), [])
        self.assertFalse(marker.exists(), "code from the library file was run")

    def test_old_pickled_scan_results_still_read(self) -> None:
        old = pickle.dumps([((np.int32(1), 2, 3, 4), np.float32(0.9), b"e", b"c", 0.5, True, "")], protocol=4)
        (box, score, emb, crop, q, good, why), = cache_unpack(old)
        self.assertEqual((tuple(int(v) for v in box), emb, crop, good), ((1, 2, 3, 4), b"e", b"c", True))
        self.assertAlmostEqual(float(score), 0.9, places=5)


class FindTest(LibraryCase):
    """'Find': photos on this computer with all the chosen people in them, from saved scan results."""

    def photo(self, name: str, people: list[np.ndarray], date: str, exists: bool = True) -> str:
        path = self.dir / name
        if exists:
            path.write_bytes(b"x")
        sha1 = name
        faces = [((0, 0, 50, 50), 0.99, face(b, "q", noise=0.05)["emb"].tobytes(), b"", 0.9, True, "") for b in people]
        self.lib.db.execute("INSERT INTO photo_cache(sha1, date, status, faces) VALUES(?,?,?,?)",
                            (sha1, date, "faces" if faces else "no_faces", cache_pack(faces) if faces else None))
        self.lib.db.execute("INSERT INTO seen(device, path, size, mtime, sha1) VALUES(?,?,?,?,?)",
                            ("TEST-PC", str(path), 1, 1.0, sha1))
        return sha1

    def test_find_people_together(self) -> None:
        from facesorter.session import find_people
        a_base, b_base, c_base = base(), base(), base()
        a, b = self.person("A", b=a_base), self.person("B", b=b_base)
        self.photo("both.jpg", [a_base, b_base], "2024-01-01")
        self.photo("only_a.jpg", [a_base, c_base], "2024-02-01")
        self.photo("gone.jpg", [a_base, b_base], "2024-03-01", exists=False)
        self.photo("nobody.jpg", [c_base], "2024-04-01")
        self.lib.db.commit()
        self.assertEqual([f.path.name for f in find_people(self.lib, [a], device="TEST-PC")],
                         ["only_a.jpg", "both.jpg"])  # newest first, the deleted photo is left out
        together = find_people(self.lib, [a, b], device="TEST-PC")
        self.assertEqual([f.path.name for f in together], ["both.jpg"])
        self.assertTrue(together[0].sure)
        self.assertEqual(find_people(self.lib, [a], device="OTHER-PC"), [])

    def test_a_confirmed_face_counts_even_if_unclear(self) -> None:
        from facesorter.session import find_people
        a = self.person("A")
        sha1 = self.photo("blurry.jpg", [base()], "2024-05-05")   # looks like nobody known...
        self.lib.add_prints(a, [face(base(), "sha1:" + sha1)], "manual")   # ...but you confirmed it was A
        self.lib.changed()
        found = find_people(self.lib, [a], device="TEST-PC")
        self.assertEqual([(f.path.name, f.people[a]) for f in found], [("blurry.jpg", "confirmed")])


class OldLibraryTest(unittest.TestCase):
    """A library made by v3.1 opens with this version and keeps everything."""

    def test_v31_library(self) -> None:
        d = Path(tempfile.mkdtemp())
        try:
            folder = d / "Library"
            folder.mkdir()
            shutil.copy2(DATA / "v3.1_library.db",
                         folder / "FaceSorter_Library_created-2026-11-22_updated-2026-11-22.db")
            lib = Library(folder)
            self.assertEqual(sorted(lib.names()), ["Ahmed", "Mona"])
            self.assertEqual(len(lib.people("unknown")), 1)
            self.assertEqual(lib.person(lib.person_by_name("Mona")).prints, 4)
            self.assertEqual(lib.created, "2026-11-22")
            row = lib.db.execute("SELECT faces FROM photo_cache WHERE sha1='abc'").fetchone()
            (box, *_), = cache_unpack(row[0])
            self.assertEqual(tuple(int(v) for v in box), (10, 20, 30, 40))
            lib.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
