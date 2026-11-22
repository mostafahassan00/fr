"""The library file on disk: dated name, optional password (encryption), copies.

    Library/
        FaceSorter_Library_created-2026-11-22_updated-2026-12-05.db   the library (SQLite / SQLCipher)
        FaceSorter_Library_created-2026-11-22_updated-2026-12-05.key  only when it has a password
        backups/

Encryption: the whole .db file is encrypted by SQLCipher with a random master
key. The .key file holds that master key twice, each locked with a key derived
(scrypt) from a secret: your password, and a recovery key you keep somewhere
safe. Changing the password only re-locks the master key. Without the password
AND without the recovery key the library cannot be opened by anyone.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import date
from pathlib import Path

from .i18n import tr

try:  # SQLCipher: same API as sqlite3, plus encryption
    import sqlcipher3 as sqlite
    HAS_CIPHER = True
except ImportError:  # pragma: no cover - the portable build always has it
    import sqlite3 as sqlite
    HAS_CIPHER = False

PREFIX = "FaceSorter_Library_created-"
NAME_RE = re.compile(r"FaceSorter_Library_created-(\d{4}-\d{2}-\d{2})_updated-(\d{4}-\d{2}-\d{2})(?:_\w+)?\.db$")
LEGACY = "library.db"
DatabaseError = sqlite.DatabaseError


class LockedError(Exception):
    """The library has a password and it was not given (or was wrong)."""


class MultipleLibrariesError(Exception):
    def __init__(self, files: list[Path]):
        super().__init__(f"{len(files)} library files in one folder")
        self.files = files


def today() -> str:
    return date.today().isoformat()


def dated_name(created: str, updated: str) -> str:
    return f"{PREFIX}{created}_updated-{updated}.db"


def parse_name(p: Path) -> tuple[str, str] | None:
    m = NAME_RE.search(p.name)
    return (m.group(1), m.group(2)) if m else None


def find_libraries(folder: Path) -> list[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        return []
    files = sorted(folder.glob(f"{PREFIX}*.db"))
    legacy = folder / LEGACY
    return files + ([legacy] if legacy.exists() else [])


def key_path(db: Path) -> Path:
    return db.with_suffix(".key")


def is_encrypted(db: Path) -> bool:
    try:
        with open(db, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return len(head) == 16 and head != b"SQLite format 3\x00"


# ------------------------------------------------------------ key wrapping
def _derive(secret: str, salt: bytes) -> bytes:
    norm = secret.strip()
    return hashlib.scrypt(norm.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)


def _wrap(master: bytes, secret: str) -> dict:
    salt = secrets.token_bytes(16)
    k = _derive(secret, salt)
    data = bytes(a ^ b for a, b in zip(master, k[:32]))
    tag = hmac.new(k[32:], salt + data, hashlib.sha256).digest()
    return {"salt": salt.hex(), "data": data.hex(), "tag": tag.hex()}


def _unwrap(entry: dict, secret: str) -> bytes | None:
    salt, data, tag = bytes.fromhex(entry["salt"]), bytes.fromhex(entry["data"]), bytes.fromhex(entry["tag"])
    k = _derive(secret, salt)
    if not hmac.compare_digest(hmac.new(k[32:], salt + data, hashlib.sha256).digest(), tag):
        return None
    return bytes(a ^ b for a, b in zip(data, k[:32]))


def new_recovery_key() -> str:
    raw = base64.b32encode(secrets.token_bytes(15)).decode().rstrip("=")
    return "-".join(raw[i:i + 6] for i in range(0, len(raw), 6))


def write_keyfile(db: Path, master: bytes, password: str, recovery: str) -> None:
    payload = {"version": 1, "password": _wrap(master, password), "recovery": _wrap(master, recovery.upper())}
    tmp = key_path(db).with_suffix(".key.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, key_path(db))


def rewrap_password(db: Path, master: bytes, password: str) -> None:
    payload = json.loads(key_path(db).read_text(encoding="utf-8"))
    payload["password"] = _wrap(master, password)
    tmp = key_path(db).with_suffix(".key.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, key_path(db))


def unlock(db: Path, secret: str) -> bytes:
    """The master key from the password or the recovery key. Raises LockedError."""
    kp = key_path(db)
    if not kp.exists():
        raise LockedError(tr("The key file (.key) next to the library is missing.", "ملف المفتاح (.key) اللي جنب المكتبة مش موجود."))
    payload = json.loads(kp.read_text(encoding="utf-8"))
    for slot, s in (("password", secret), ("recovery", secret.upper())):
        master = _unwrap(payload[slot], s)
        if master is not None:
            return master
    raise LockedError(tr("Wrong password.", "الباسورد غلط."))


# ---------------------------------------------------------------- connect
def _key_sql(master: bytes | None) -> str:
    return f"x'{master.hex()}'" if master else ""


def connect(db: Path, master: bytes | None = None):
    conn = sqlite.connect(str(db), check_same_thread=False)
    if master:
        if not HAS_CIPHER:
            raise LockedError(tr("This copy of the program cannot open password-protected libraries.",
                                 "النسخة دي من البرنامج مش بتقدر تفتح مكتبات عليها باسورد."))
        conn.execute(f"PRAGMA key = \"{_key_sql(master)}\"")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except DatabaseError as e:
        conn.close()
        raise LockedError(tr("Wrong password or damaged library file.", "الباسورد غلط أو ملف المكتبة بايظ.")) from e
    conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def export_to(conn, dest: Path, master: bytes | None) -> None:
    """Write a full copy of the open library to `dest`, encrypted with `master` (None = no password)."""
    dest = Path(dest)
    if dest.exists():
        dest.unlink()
    conn.commit()
    if HAS_CIPHER:
        conn.execute("ATTACH DATABASE ? AS export_db KEY ?", (str(dest), _key_sql(master)))
        try:
            conn.execute("SELECT sqlcipher_export('export_db')")
        finally:
            conn.execute("DETACH DATABASE export_db")
    else:
        out = sqlite.connect(str(dest))
        try:
            conn.backup(out)
        finally:
            out.close()
