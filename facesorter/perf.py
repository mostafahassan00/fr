"""How hard the program works: speed levels, pause, battery saving."""
from __future__ import annotations

import os
import queue
import threading
from contextlib import contextmanager

import cv2

from .engine import FaceEngine

LEVELS = ("quiet", "balanced", "fast")
LABELS = {"quiet": "🐢 Quiet | هادي", "balanced": "⚖️ Balanced | متوازن", "fast": "🚀 Fast | سريع"}

try:
    import psutil
except ImportError:  # optional: priority and battery features need it
    psutil = None

CORES = os.cpu_count() or 2


def workers_for(level: str) -> int:
    if level == "quiet":
        return 1
    if level == "fast":
        return max(1, CORES)
    return max(1, CORES // 2)


def set_low_priority(low: bool) -> None:
    """Quiet mode: let every other program on the laptop go first."""
    if psutil is None:
        return
    try:
        p = psutil.Process()
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if low else psutil.NORMAL_PRIORITY_CLASS)
        else:
            if low:
                p.nice(10)  # cannot be raised again without admin rights; fine for this program
    except Exception:
        pass


def on_battery() -> bool:
    if psutil is None:
        return False
    try:
        b = psutil.sensors_battery()
        return b is not None and not b.power_plugged
    except Exception:
        return False


class Throttle:
    """Shared, changeable settings that the running job reads between batches."""

    def __init__(self, level: str = "balanced", battery_saver: bool = True):
        self.level = level if level in LEVELS else "balanced"
        self.battery_saver = battery_saver
        self.pause = threading.Event()
        self.stop = threading.Event()
        self._battery = False
        self._checked = 0.0

    def effective_level(self, now: float) -> str:
        if now - self._checked > 20:
            self._checked = now
            self._battery = on_battery()
        if self.battery_saver and self._battery:
            return "quiet"
        return self.level

    @property
    def on_battery(self) -> bool:
        return self._battery


class EnginePool:
    """Face engines shared by worker threads. The number available sets how many photos are
    processed at the same time, and can change while running."""

    def __init__(self, log=print):
        cv2.setNumThreads(1)  # we parallelise over photos instead
        self._log = log
        self._free: queue.Queue = queue.Queue()
        self._made = 0
        self._target = 1
        self._lock = threading.Lock()

    def resize(self, n: int) -> None:
        with self._lock:
            self._target = max(1, n)
            while self._made < self._target:
                self._free.put(FaceEngine(self._log))
                self._made += 1

    @contextmanager
    def engine(self):
        eng = self._free.get()
        try:
            yield eng
        finally:
            with self._lock:
                if self._made > self._target:
                    self._made -= 1  # shrink: drop this engine
                else:
                    self._free.put(eng)
