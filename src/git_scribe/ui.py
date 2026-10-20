"""Small terminal helpers (stderr only, so stdout stays machine-readable)."""
from __future__ import annotations

import sys
import threading


def say(message: str) -> None:
    print(f"git-scribe: {message}", file=sys.stderr, flush=True)


class Spinner:
    """Shows a spinner on stderr while the model works. No-op when stderr isn't a TTY."""

    def __init__(self, text: str):
        self.text = text
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if sys.stderr.isatty():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        frames, i = "|/-\\", 0
        while not self._stop.wait(0.1):
            sys.stderr.write(f"\r{frames[i % 4]} {self.text}")
            sys.stderr.flush()
            i += 1

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
