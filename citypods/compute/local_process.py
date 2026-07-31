"""Killable persistent subprocess backend for local ASR inference.

Native faster-whisper/stable-ts calls cannot be interrupted safely from a Python thread. This
backend keeps model caches inside one spawned worker process, sends serializable episode jobs over a
pipe, and lets the parent terminate/restart that process when an item exceeds its wall-clock limit.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading
import traceback
from multiprocessing.connection import Connection
from types import ModuleType

from citypods.compute.base import InferenceJob, JobResult


class InferenceProcessTerminated(RuntimeError):
    """The local inference worker exited or was terminated before returning a result."""


class LocalInferenceWorkerError(RuntimeError):
    """An exception raised inside the killable local inference subprocess, re-raised in the
    parent. Keeps the worker's original exception type/message as attributes (not just folded
    into the message string) so callers can classify the failure, e.g. media-decode quarantine
    (``external_worker._is_deterministic_media_decode_error``)."""

    def __init__(self, name: str, message: str, traceback_text: str) -> None:
        super().__init__(f"local inference worker {name}: {message}\n{traceback_text}")
        self.worker_exception_name = name
        self.worker_exception_message = message


def _run_job(asr, job: InferenceJob) -> JobResult:
    inp = job.inputs
    model_name = inp["model"]
    if not isinstance(model_name, str):
        raise TypeError("process-local inference requires a serializable model name")
    if job.task == "transcribe":
        model = asr.load_model(model_name, inp["compute_type"], inp["cpu_threads"])
        output = asr.transcribe(
            inp["audio_path"],
            model,
            inp["language"],
            inp["compute_type"],
            inp["beam_size"],
            inp["initial_prompt"],
            inp["cpu_threads"],
        )
    elif job.task == "align":
        output = asr.align(
            inp["audio_path"],
            inp["text"],
            model_name,
            inp["language"],
            inp["cpu_threads"],
        )
    else:
        raise ValueError(f"process-local backend does not implement task {job.task!r}")
    return JobResult(task=job.task, recipe_hash=job.recipe_hash, output=output)


def _worker_main(conn: Connection, asr_override: ModuleType | object | None) -> None:
    if asr_override is None:
        from citypods import asr
    else:
        asr = asr_override
    try:
        while True:
            message = conn.recv()
            if message is None:
                return
            try:
                conn.send(("ok", _run_job(asr, message)))
            except KeyboardInterrupt:
                # SIGINT is the cooperative first step of parent teardown. Do not turn it into a
                # user-facing inference error; exiting through the outer ``finally`` lets native
                # resources unregister before terminate/kill escalation is needed.
                return
            except BaseException as exc:  # noqa: BLE001 - serialize worker failures to parent
                conn.send(
                    (
                        "error",
                        {
                            "module": type(exc).__module__,
                            "name": type(exc).__name__,
                            "message": str(exc),
                            "traceback": traceback.format_exc(),
                        },
                    )
                )
    except (EOFError, BrokenPipeError, OSError, KeyboardInterrupt):
        return
    finally:
        conn.close()


class ProcessLocalBackend:
    """Synchronous local backend whose native inference runs in a killable child process."""

    name = "local"
    isolates_inference = True

    def __init__(
        self,
        *,
        start_method: str = "spawn",
        asr: ModuleType | object | None = None,
    ) -> None:
        self._ctx = multiprocessing.get_context(start_method)
        self._asr_override = asr
        self._call_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process: multiprocessing.Process | None = None
        self._conn: Connection | None = None

    def _ensure_worker(self) -> Connection:
        with self._process_lock:
            if self._process is not None and self._process.is_alive() and self._conn is not None:
                return self._conn
            self._reset_worker_locked()
            parent, child = self._ctx.Pipe()
            process = self._ctx.Process(
                target=_worker_main,
                args=(child, self._asr_override),
                name="citypods-local-asr",
                daemon=True,
            )
            process.start()
            child.close()
            self._process = process
            self._conn = parent
            return parent

    def _reset_worker_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        if self._process is not None:
            if self._process.is_alive():
                self._interrupt_process(self._process)
                self._process.join(timeout=1)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=5)
                if self._process.is_alive():
                    self._process.kill()
                    self._process.join(timeout=5)
            else:
                self._process.join(timeout=0.1)
            try:
                self._process.close()
            except ValueError:
                # ``multiprocessing.Process.close()`` raises if the process is still running.
                pass
        self._conn = None
        self._process = None

    @staticmethod
    def _interrupt_process(process: multiprocessing.Process) -> None:
        """Give Python/native inference a chance to unwind before SIGTERM/SIGKILL.

        The spawned ASR process can own named semaphores created by its native dependencies. A
        hard terminate skips their normal unregister path and leaves the shared multiprocessing
        resource tracker to report a leak at runner shutdown. SIGINT is catchable as
        ``KeyboardInterrupt`` and the worker's ``finally`` block closes its IPC endpoint, so use
        it as the first shutdown signal; the existing terminate/kill escalation remains the
        bounded backstop for genuinely stuck native code.
        """
        pid = getattr(process, "pid", None)
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass

    def run_inference(self, job: InferenceJob) -> JobResult:
        with self._call_lock:
            conn = self._ensure_worker()
            try:
                conn.send(job)
                status, payload = conn.recv()
            except (EOFError, BrokenPipeError, OSError) as exc:
                with self._process_lock:
                    self._reset_worker_locked()
                raise InferenceProcessTerminated(
                    "local inference worker exited before returning a result"
                ) from exc
            if status == "ok":
                return payload
            if status != "error" or not isinstance(payload, dict):
                raise RuntimeError(f"invalid response from local inference worker: {status!r}")
            if payload.get("name") == "AlignmentQualityError":
                from citypods import asr

                raise asr.AlignmentQualityError(payload.get("message", "alignment failed"))
            raise LocalInferenceWorkerError(
                payload.get("name", "error"),
                payload.get("message", ""),
                payload.get("traceback", ""),
            )

    def terminate_active(self) -> bool:
        """Terminate the current worker, waking any blocked ``run_inference`` caller."""
        with self._process_lock:
            process = self._process
            if process is None or not process.is_alive():
                self._reset_worker_locked()
                return False
            self._interrupt_process(process)
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            self._reset_worker_locked()
            return True

    def close(self) -> None:
        with self._process_lock:
            process = self._process
            conn = self._conn
            if process is None:
                self._reset_worker_locked()
                return
            if process.is_alive() and conn is not None:
                try:
                    conn.send(None)
                except (BrokenPipeError, OSError):
                    pass
                process.join(timeout=5)
            if process.is_alive():
                self._interrupt_process(process)
                process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            self._reset_worker_locked()
