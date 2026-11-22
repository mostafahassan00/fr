"""Finding faces in photos and turning each face into a 128-number "print".

Also: face quality, the date a photo was taken (EXIF), which files to skip,
and grouping prints (average-linkage clustering, no extra libraries).
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .i18n import tr

APP_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = APP_DIR / "models"
MODELS = {
    # OpenCV Zoo models (Apache-2.0 / MIT), mirrored on Hugging Face.
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
    ),
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
HEIF_EXTS = {".heic", ".heif", ".hif"}   # iPhone photos (read with pi-heif, when it is installed)
try:
    import pi_heif
    IMAGE_EXTS |= HEIF_EXTS
except ImportError:  # pragma: no cover - the portable build has it
    pi_heif = None
MAX_SIDE = 1920        # big photos are shrunk to this before looking for faces
MIN_FACE = 36          # faces smaller than this (pixels) are too small to recognise at all
DETECT_SCORE = 0.80    # how sure the detector must be that something is a face
BIG_FILE = 2_500_000   # JPEGs larger than this are decoded at half size (much faster)

# quality: a face is "good enough to learn from" when all of these hold
GOOD_SIZE = 60         # pixels (shortest side of the face box, in the resized photo)
GOOD_SHARPNESS = 60.0  # variance of the Laplacian of the 112x112 face
GOOD_YAW = 0.35        # how far the nose is from the middle of the eyes (0 = straight on)
GOOD_SCORE = 0.85      # detector confidence

Log = Callable[[str], None]


def similarity_for(strictness: int) -> float:
    """Strictness 1..10 -> minimum similarity for two faces to be the same person."""
    return 0.26 + 0.02 * max(1, min(10, int(strictness)))   # 1 -> 0.28, 5 -> 0.36, 10 -> 0.46


@dataclass
class DetectedFace:
    box: tuple[int, int, int, int]  # x, y, w, h in the (resized) photo
    score: float
    embedding: np.ndarray            # normalised float32 (128,)
    crop: np.ndarray                 # aligned face picture (112x112 BGR)
    quality: float                   # 0..1
    good: bool                       # good enough to learn from
    why: str = ""                    # why not good ("small", "blurry", "side", "unsure")


def ensure_models(log: Log = print) -> dict[str, Path]:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, (name, url) in MODELS.items():
        path = MODEL_DIR / name
        if not path.exists() or path.stat().st_size < 100_000:
            log(tr(f"Downloading AI model {name} (first run only)...", f"بنزّل نموذج {name} (أول مرة بس)..."))
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            if tmp.stat().st_size < 100_000:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(tr(f"Download of {name} failed. Check your internet connection.",
                                      f"تنزيل {name} فشل. اتأكد من النت."))
            tmp.replace(path)
        paths[key] = path
    return paths


def face_quality(det: np.ndarray, crop: np.ndarray) -> tuple[float, bool, str]:
    """Quality of one detected face: size, sharpness, how frontal it is, detector confidence."""
    w, h = float(det[2]), float(det[3])
    size = min(w, h)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    # landmarks: right eye (4,5), left eye (6,7), nose (8,9)
    eye_mid = (det[4] + det[6]) / 2.0
    eye_dist = abs(det[6] - det[4]) + 1e-6
    yaw = abs(det[8] - eye_mid) / eye_dist
    score = float(det[-1])
    why = []
    if size < GOOD_SIZE:
        why.append("small")
    if sharp < GOOD_SHARPNESS:
        why.append("blurry")
    if yaw > GOOD_YAW:
        why.append("side")
    if score < GOOD_SCORE:
        why.append("unsure")
    q = (min(1.0, size / (2 * GOOD_SIZE)) * 0.35 + min(1.0, sharp / (3 * GOOD_SHARPNESS)) * 0.35
         + max(0.0, 1.0 - yaw / (2 * GOOD_YAW)) * 0.2 + score * 0.1)
    return round(float(q), 3), not why, ",".join(why)


class FaceEngine:
    def __init__(self, log: Log = print):
        paths = ensure_models(log)
        self.detector = cv2.FaceDetectorYN.create(str(paths["detector"]), "", (320, 320), DETECT_SCORE, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(paths["recognizer"]), "")

    def faces_in(self, img: np.ndarray) -> list[DetectedFace]:
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, dets = self.detector.detect(img)
        faces: list[DetectedFace] = []
        if dets is None:
            return faces
        for det in dets:
            x, y, fw, fh = (int(v) for v in det[:4])
            if min(fw, fh) < MIN_FACE:
                continue
            crop = self.recognizer.alignCrop(img, det)
            emb = self.recognizer.feature(crop).flatten().astype(np.float32)
            emb /= np.linalg.norm(emb) + 1e-9
            q, good, why = face_quality(det, crop)
            faces.append(DetectedFace((x, y, fw, fh), float(det[-1]), emb, crop, q, good, why))
        return faces


# --------------------------------------------------------------------- files
def read_photo(path: Path) -> tuple[str | None, np.ndarray | None, str | None]:
    """Returns (sha1 of the file, decoded image, date taken "YYYY-MM-DD" or None).

    Works with Arabic paths on Windows. The sha1 identifies the photo even if
    it is copied or renamed. Big JPEGs are decoded directly at half size.
    """
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None, None, None
    raw = data.tobytes()
    sha1 = hashlib.sha1(raw).hexdigest()
    if path.suffix.lower() in HEIF_EXTS or raw[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        img, date = read_heif(raw)
        return sha1, shrink(img) if img is not None else None, date
    date = exif_date(raw)
    img = None
    if len(data) > BIG_FILE and path.suffix.lower() in (".jpg", ".jpeg"):
        img = cv2.imdecode(data, cv2.IMREAD_REDUCED_COLOR_2)
        if img is not None and max(img.shape[:2]) < 1400:
            img = None  # small picture after all: decode at full size
    if img is None:
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return sha1, None, date
    return sha1, shrink(img), date


def shrink(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def read_heif(raw: bytes) -> tuple[np.ndarray | None, str | None]:
    """An iPhone photo (HEIC / HEIF): the picture (turned the right way up) and the date it was taken."""
    if pi_heif is None:
        return None, None
    try:
        import io
        heif = pi_heif.open_heif(io.BytesIO(raw), convert_hdr_to_8bit=True)
        img = np.asarray(heif)
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR if img.shape[2] == 4 else cv2.COLOR_RGB2BGR)
        exif = heif.info.get("exif") or b""
        start = exif.find(b"Exif\x00\x00")
        date = _tiff_date(exif[start + 6:]) if start >= 0 else None
        return img, date
    except Exception:
        return None, None


def exif_date(raw: bytes) -> str | None:
    """DateTimeOriginal (or DateTime) from a JPEG's EXIF, as YYYY-MM-DD. No extra libraries."""
    try:
        if raw[:2] != b"\xff\xd8":
            return None
        i = 2
        while i + 4 < len(raw) and i < 200_000:
            if raw[i] != 0xFF:
                return None
            marker, size = raw[i + 1], struct.unpack(">H", raw[i + 2:i + 4])[0]
            if marker == 0xE1 and raw[i + 4:i + 10] == b"Exif\x00\x00":
                return _tiff_date(raw[i + 10:i + 2 + size])
            if marker in (0xDA, 0xD9):
                return None
            i += 2 + size
    except (struct.error, IndexError, ValueError):
        return None
    return None


def _tiff_date(t: bytes) -> str | None:
    end = "<" if t[:2] == b"II" else ">"

    def u16(o):
        return struct.unpack(end + "H", t[o:o + 2])[0]

    def u32(o):
        return struct.unpack(end + "I", t[o:o + 4])[0]

    def read_ifd(off):
        tags = {}
        for k in range(u16(off)):
            e = off + 2 + 12 * k
            tag, typ, count = u16(e), u16(e + 2), u32(e + 4)
            if typ == 2:  # ASCII
                vo = u32(e + 8) if count > 4 else e + 8
                tags[tag] = t[vo:vo + count].split(b"\x00")[0].decode("ascii", "ignore")
            elif typ == 4:
                tags[tag] = u32(e + 8)
        return tags

    ifd0 = read_ifd(u32(4))
    exif = read_ifd(ifd0[0x8769]) if 0x8769 in ifd0 else {}
    value = exif.get(0x9003) or exif.get(0x9004) or ifd0.get(0x0132)
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})", value or "")
    if not m or m.group(1) in ("0000",):
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def file_date(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return None


def write_jpg(path: Path, img: np.ndarray, quality: int = 90) -> None:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        buf.tofile(str(path))


def encode_jpg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def decode_jpg(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


# ------------------------------------------------------------- which files
SCREENSHOT = re.compile(r"screenshot|screen[ _-]?shot|screen[ _-]?capture|لقطة[ _]?شاشة|snapshot_?\d", re.I)
STICKER_DIR = re.compile(r"sticker", re.I)


@dataclass
class Filters:
    skip_screenshots: bool = True
    skip_stickers: bool = True
    min_side: int = 300                  # skip pictures smaller than this (0 = keep all)
    exclude: tuple[str, ...] = ()        # folder names to skip, e.g. ("Downloads", "Memes")

    def skip_file(self, path: Path) -> str | None:
        """Why this file should be skipped before opening it (None = keep)."""
        parts = [p.lower() for p in path.parts[:-1]]
        for ex in self.exclude:
            if ex.strip() and ex.strip().lower() in parts:
                return "excluded folder"
        if self.skip_screenshots and (SCREENSHOT.search(path.name) or any(SCREENSHOT.search(p) for p in parts)):
            return "screenshot"
        if self.skip_stickers and (any(STICKER_DIR.search(p) for p in parts)
                                   or (path.suffix.lower() == ".webp" and "whatsapp" in " ".join(parts))):
            return "sticker"
        return None

    def skip_image(self, img: np.ndarray) -> str | None:
        if self.min_side and max(img.shape[:2]) < self.min_side:
            return "too small"
        return None


def find_photos(folder: Path) -> list[Path]:
    photos = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("$recycle.bin", "system volume information")]
        for f in files:
            if Path(f).suffix.lower() in IMAGE_EXTS and not f.startswith("."):
                photos.append(Path(root) / f)
    return sorted(photos)


# ----------------------------------------------------------------- grouping
def top_k_mean(sims: np.ndarray, k: int = 3) -> np.ndarray:
    """For a (faces x prints) similarity matrix: mean of each row's k best values."""
    if sims.shape[1] == 0:
        return np.zeros(sims.shape[0], np.float32)
    k = min(k, sims.shape[1])
    part = np.partition(sims, sims.shape[1] - k, axis=1)[:, -k:]
    return part.mean(axis=1)


def cluster(embeddings: np.ndarray, min_similarity: float) -> np.ndarray:
    """Average-linkage grouping of face prints (nearest-neighbour chain, O(n²)).

    Two groups join when the average similarity between their faces is at
    least `min_similarity`. Returns one group label per face.
    """
    n = len(embeddings)
    if n <= 1:
        return np.zeros(n, dtype=int)
    x = embeddings.astype(np.float32)
    d = 1.0 - x @ x.T                       # cosine distances
    np.fill_diagonal(d, np.inf)
    size = np.ones(n)
    active = np.ones(n, bool)
    parent = list(range(n))
    limit = 1.0 - min_similarity

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    chain: list[int] = []
    remaining = n
    while remaining > 1:
        if not chain:
            chain.append(int(np.flatnonzero(active)[0]))
        a = chain[-1]
        b = int(np.argmin(d[a]))
        dist = d[a, b]
        if len(chain) > 1 and d[a, chain[-2]] <= dist:
            b, dist = chain[-2], d[a, chain[-2]]
        if len(chain) > 1 and b == chain[-2]:
            chain.pop()
            chain.pop()
            # merge b into a (Lance-Williams update for average linkage)
            na, nb = size[a], size[b]
            new = (na * d[a] + nb * d[b]) / (na + nb)
            new[a] = np.inf
            d[a, :] = new
            d[:, a] = new
            d[b, :] = np.inf
            d[:, b] = np.inf
            size[a] = na + nb
            active[b] = False
            remaining -= 1
            if dist <= limit:
                parent[find(b)] = find(a)
        else:
            chain.append(b)
    roots = [find(i) for i in range(n)]
    _, labels = np.unique(roots, return_inverse=True)
    return labels


def face_sheet(thumbs: list[np.ndarray], size: int = 96, cols: int = 8) -> np.ndarray:
    thumbs = thumbs[: cols * 3]
    rows = max(1, (len(thumbs) + cols - 1) // cols)
    sheet = np.full((rows * size, max(1, min(cols, len(thumbs))) * size, 3), 30, np.uint8)
    for i, t in enumerate(thumbs):
        r, c = divmod(i, cols)
        sheet[r * size:(r + 1) * size, c * size:(c + 1) * size] = cv2.resize(t, (size, size))
    return sheet
