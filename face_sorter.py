#!/usr/bin/env python3
"""
Face Sorter v3 - a portable library of people's faces that grows on every computer.

    python face_sorter.py                       opens the window

Command line (mostly for automation and tests):
    python face_sorter.py scan PHOTOS [--mode learn|learn_sort|sort] [--dest DIR]
                                      scan a folder, accept what is SURE, keep the rest unknown
    python face_sorter.py people                known people (and --unknown)
    python face_sorter.py name LABEL NAME       name / rename a person (e.g. Unknown_0007 "Ahmed")
    python face_sorter.py who PHOTO             who is in this photo?
    python face_sorter.py duplicates            people that may be the same person
    python face_sorter.py sessions              visits and their reports
    python face_sorter.py undo                  undo the last saved session
    python face_sorter.py log                   history
    python face_sorter.py accuracy FOLDER       how accurate is recognition on your photos
                                      (one folder per person; see facesorter/accuracy.py)

Every command takes --library DIR (default: the Library folder next to the program folder).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles cannot print Arabic otherwise
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) == 1:
        try:
            from facesorter.gui import main as gui_main
            gui_main()
        except SystemExit:
            raise
        except Exception:  # the window could not even start: say so instead of closing silently
            import traceback
            details = traceback.format_exc()
            log = Path(__file__).resolve().parent
            log = (log.parent if log.name == "app" else log) / "errors.txt"
            try:
                with open(log, "a", encoding="utf-8") as f:
                    f.write("\n===== start-up =====\n" + details)
            except OSError:
                pass
            try:
                import tkinter as tk
                from tkinter import messagebox
                r = tk.Tk()
                r.withdraw()
                messagebox.showerror("Face Sorter", f"Face Sorter could not start.\nDetails were saved in:\n{log}\n\n"
                                                    "البرنامج مقدرش يفتح، والتفاصيل اتحفظت في errors.txt")
            except Exception:
                print(details)
        return

    from facesorter.engine import FaceEngine, Filters
    from facesorter.library import LIBRARY_DIR, Library
    from facesorter.perf import Throttle
    from facesorter.session import MODES, build_review, identify, save_review, scan

    ap = argparse.ArgumentParser(description="A portable library of people's faces.")
    ap.add_argument("--library", type=Path, default=LIBRARY_DIR, help=f"library folder (default {LIBRARY_DIR})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan photos and save what is sure")
    s.add_argument("photos", type=Path)
    s.add_argument("--mode", choices=MODES, default="learn")
    s.add_argument("--dest", type=Path, help="where to put sorted photos (sort modes)")
    s.add_argument("--strictness", type=int, default=5)
    s.add_argument("--min-photos", type=int, default=2)
    s.add_argument("--keep-small", action="store_true", help="do not skip pictures smaller than 300px")
    s.add_argument("--speed", choices=["quiet", "balanced", "fast"], default="balanced")
    p = sub.add_parser("people", help="list people")
    p.add_argument("--unknown", action="store_true")
    n = sub.add_parser("name", help="name a person")
    n.add_argument("label", help="current name or Unknown_NNNN")
    n.add_argument("name")
    w = sub.add_parser("who", help="who is in this photo?")
    w.add_argument("photo", type=Path)
    for cmd in ("duplicates", "sessions", "undo", "log"):
        sub.add_parser(cmd)
    a = sub.add_parser("accuracy", help="measure recognition on a folder with one folder per person")
    a.add_argument("folder", type=Path)
    a.add_argument("--learn", type=int, default=0, help="photos to learn from per person (default: half)")
    a.add_argument("--strictness", type=int, default=5, help="strictness for the per-person lines")
    args = ap.parse_args()

    if args.cmd == "accuracy":  # uses its own throw-away library, never yours
        from facesorter import accuracy
        try:
            print(accuracy.report(accuracy.measure(args.folder, args.learn, args.strictness), args.strictness))
        except RuntimeError as err:
            sys.exit(f"Error: {err}")
        return

    lib = Library(args.library)
    try:
        if args.cmd == "scan":
            def bar(stage: int, i: int, total: int) -> None:
                if stage == 2 and total and (i % 48 == 0 or i == total):
                    print(f"\r  {i}/{total} photos", end="" if i < total else "\n", flush=True)
            flt = Filters(min_side=0 if args.keep_small else 300)
            sid = scan(lib, args.photos, flt, Throttle(args.speed, False), bar)
            review = build_review(lib, sid, args.strictness, args.min_photos)
            c = review.counts()
            print(f"Review: {c['sure']} sure, {c['maybe']} to confirm (kept unknown), "
                  f"{c['seen_before']} seen before, {c['new']} new")
            print(save_review(lib, review, args.mode, args.dest, log=lambda m: None))
        elif args.cmd == "people":
            for x in lib.people("unknown" if args.unknown else "known"):
                print(f"{x.label:24s} faces {x.prints:4d}  looks {x.looks:2d}  {x.level:6s}  seen {x.seen_count:4d}"
                      f"  on {', '.join(x.devices)}")
            print(lib.stats())
        elif args.cmd == "name":
            match = [x for x in lib.people(None) if x.label == args.label]
            if not match:
                sys.exit(f"No person called {args.label}")
            print(lib.rename(match[0].id, args.name) or "Nothing changed")
        elif args.cmd == "who":
            for r in identify(lib, FaceEngine(), args.photo)[1]:
                if r["matches"]:
                    print(f"{r['verdict']:8s} " + ", ".join(f"{nm} ({sc:.2f})" for nm, sc in r["matches"]))
                else:
                    print("unknown  " + (r["seen_before"] or ""))
        elif args.cmd == "duplicates":
            for a, b, sc in lib.duplicates():
                print(f"{lib.person(a).label}  ~  {lib.person(b).label}   {sc:.2f}")
        elif args.cmd == "sessions":
            for sid, started, device, folder, mode, status, report in lib.sessions():
                print(f"#{sid} {started} {device} {status} {folder}\n   " + (report or "").replace("\n", "\n   "))
        elif args.cmd == "undo":
            print(lib.undo_last_session())
        elif args.cmd == "log":
            for at, kind, text in reversed(lib.log_entries(("history", "memory"))):
                print(f"{at}  [{kind}]  {text}")
    except RuntimeError as err:
        sys.exit(f"Error: {err}")
    finally:
        lib.close()


if __name__ == "__main__":
    main()
