"""Report kernel cgroup total-memory peak, with sampled-current fallback."""

import threading
from pathlib import Path


class CgroupMemory:
    def __init__(self, root="/sys/fs/cgroup", interval=0.25):
        root = Path(root)
        candidates = [
            (root / "memory.peak", root / "memory.current", root / "memory.max"),
            (
                root / "memory/memory.max_usage_in_bytes",
                root / "memory/memory.usage_in_bytes",
                root / "memory/memory.limit_in_bytes",
            ),
        ]
        self.peak_path, self.current_path, self.limit_path = next(
            (paths for paths in candidates if paths[1].is_file()), candidates[0]
        )
        self.interval = interval
        self.sampled = None
        self.sample_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._sample_loop, daemon=True)

    @staticmethod
    def _read(path):
        try:
            value = path.read_text().strip()
            return int(value) if value.isdecimal() else None
        except OSError:
            return None

    def _record(self, current):
        if current is not None:
            with self.sample_lock:
                self.sampled = max(self.sampled or 0, current)

    def _sample_loop(self):
        while not self.stop_event.is_set():
            current = self._read(self.current_path)
            self._record(current)
            self.stop_event.wait(self.interval)

    def start(self):
        self.thread.start()

    def snapshot(self):
        peak = self._read(self.peak_path)
        current = self._read(self.current_path)
        self._record(current)
        with self.sample_lock:
            sampled = self.sampled
        return {
            "peak_bytes": peak if peak is not None else sampled,
            "peak_kind": "kernel"
            if peak is not None
            else ("sampled" if sampled is not None else "unavailable"),
            "peak_path": str(self.peak_path),
            "current_path": str(self.current_path),
            "current_bytes": current,
            "limit_path": str(self.limit_path),
            "limit_bytes": self._read(self.limit_path),
            "sample_interval_seconds": self.interval,
        }

    def stop(self):
        self.stop_event.set()
        self.thread.join()
        return self.snapshot()
