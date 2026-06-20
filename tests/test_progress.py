"""Tests for citypods/progress.py: the stall-diagnostics progress registry."""

from __future__ import annotations

import threading

from citypods.progress import ProgressRegistry, format_snapshot


class TestProgressRegistry:
    def test_no_active_work_by_default(self):
        reg = ProgressRegistry()
        assert reg.snapshot() == []
        assert reg.longest_elapsed() == 0.0

    def test_start_then_finish_clears_entry(self):
        reg = ProgressRegistry()
        previous = reg.start(source="dallas-tx", uid="ep1", phase="audio-encode")
        assert len(reg.snapshot()) == 1
        reg.finish(previous)
        assert reg.snapshot() == []

    def test_entry_fields_and_elapsed(self):
        reg = ProgressRegistry()
        reg.start(source="dallas-tx", uid="ep1", phase="audio-encode")
        (entry,) = reg.snapshot()
        assert entry.source == "dallas-tx"
        assert entry.uid == "ep1"
        assert entry.phase == "audio-encode"
        assert entry.elapsed() >= 0.0

    def test_nested_start_restores_previous_on_finish(self):
        """A thread that starts a second phase while already tracking one restores the first
        when the inner phase finishes, rather than clearing tracking entirely."""
        reg = ProgressRegistry()
        outer = reg.start(source="dallas-tx", uid="ep1", phase="timeline-plan")
        inner = reg.start(source="dallas-tx", uid="ep1", phase="audio-encode")
        reg.finish(inner)
        (entry,) = reg.snapshot()
        assert entry.phase == "timeline-plan"
        reg.finish(outer)
        assert reg.snapshot() == []

    def test_each_thread_tracked_independently(self):
        reg = ProgressRegistry()
        barrier = threading.Barrier(2)
        results: dict[str, int] = {}

        def worker(uid):
            reg.start(source="dallas-tx", uid=uid, phase="audio-encode")
            barrier.wait()
            results[uid] = len(reg.snapshot())

        threads = [threading.Thread(target=worker, args=(uid,)) for uid in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == {"a": 2, "b": 2}

    def test_track_context_manager_cleans_up_on_exception(self):
        reg = ProgressRegistry()
        try:
            with reg.track(source="dallas-tx", uid="ep1", phase="audio-encode"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert reg.snapshot() == []

    def test_longest_elapsed_with_multiple_entries(self):
        reg = ProgressRegistry()
        reg.start(source="s", uid="a", phase="p")
        reg.start(source="s", uid="b", phase="p")
        assert reg.longest_elapsed() >= 0.0


class TestFormatSnapshot:
    def test_empty_snapshot(self):
        assert format_snapshot([]) == "no tracked work active"

    def test_formats_active_entries(self):
        reg = ProgressRegistry()
        reg.start(source="dallas-tx", uid="ep1", phase="audio-encode")
        text = format_snapshot(reg.snapshot())
        assert "audio-encode" in text
        assert "dallas-tx" in text
        assert "ep1" in text

    def test_limits_shown_entries(self):
        # Each thread's entry lives at a distinct key (thread ident), so distinct threads are
        # needed to register more than one concurrent entry.
        reg = ProgressRegistry()
        barrier = threading.Barrier(10)

        def worker(i):
            reg.start(source="s", uid=f"ep{i}", phase="p")
            barrier.wait()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        text = format_snapshot(reg.snapshot(), limit=3)
        assert "+7 more" in text
