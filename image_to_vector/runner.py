"""Runs traces in a child process.

vtracer holds the GIL for the whole trace, so a worker thread would freeze
the UI. A process also lets a stale trace be killed outright when the user
moves a slider mid-trace, instead of waiting for it to finish.

The worker is given file contents, never a path: macOS grants access to
files in protected folders per process (see macos.py), so the worker may be
refused a file the window process can read.
"""
from __future__ import annotations

import multiprocessing as mp

from PySide6.QtCore import QObject, QTimer, Signal

from .tracer import TraceSettings, trace_to_pipe


class TraceRunner(QObject):
    finished = Signal(str)  # SVG text
    failed = Signal(str)
    busyChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        # spawn on every platform: fork after Qt has started threads is unsafe.
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._conn = None
        self._poll = QTimer(self)
        self._poll.setInterval(30)
        self._poll.timeout.connect(self._check)

    def is_busy(self) -> bool:
        return self._proc is not None

    def start(self, data: bytes, settings: TraceSettings) -> None:
        """Trace an image file's contents; a trace still running is killed and never reported."""
        self._stop()
        recv_conn, send_conn = self._ctx.Pipe(duplex=False)
        self._proc = self._ctx.Process(target=trace_to_pipe, args=(send_conn, data, settings), daemon=True)
        self._proc.start()
        # Drop our copy of the send end so a crashed child reads as EOF.
        send_conn.close()
        self._conn = recv_conn
        self._poll.start()
        self.busyChanged.emit(True)

    def cancel(self) -> None:
        if self._stop():
            self.busyChanged.emit(False)

    def _stop(self) -> bool:
        if self._proc is None:
            return False
        self._poll.stop()
        self._proc.terminate()
        self._proc.join()
        self._conn.close()
        self._proc = self._conn = None
        return True

    def _check(self) -> None:
        if not self._conn.poll():
            return
        try:
            status, payload = self._conn.recv()
        except EOFError:
            self._proc.join()
            status, payload = "error", f"trace process died (exit code {self._proc.exitcode})"
        self._stop()
        self.busyChanged.emit(False)
        (self.finished if status == "ok" else self.failed).emit(payload)
