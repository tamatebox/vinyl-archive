"""Audio source: a subprocess (arecord by default) writing raw PCM to stdout.

The subprocess model keeps failure detection trivial — device loss or any
capture error surfaces as EOF on the pipe, and the supervisor restarts us.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

ALSA_CARDS = Path("/proc/asound/cards")


class SubprocessSource:
    def __init__(self, command: list[str]):
        self.command = command
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        log.info("starting capture: %s", " ".join(self.command))
        self._proc = subprocess.Popen(
            self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name="capture-stderr").start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                log.warning("capture: %s", text)

    def read_exact(self, nbytes: int) -> bytes:
        """Read exactly nbytes; a shorter result means EOF (process died)."""
        chunks = []
        remaining = nbytes
        while remaining > 0:
            chunk = self._proc.stdout.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None

    @property
    def returncode(self) -> int | None:
        return self._proc.poll() if self._proc else None


def alsa_device_present(device: str) -> bool:
    """Check /proc/asound/cards for the card named in an ``hw:CARD=x`` device.

    Returns True when the check does not apply (non-Linux, unparseable
    device string) so that arecord itself becomes the arbiter.
    """
    if not ALSA_CARDS.exists():
        return True
    m = re.search(r"CARD=([^,]+)", device)
    if not m:
        return True
    name = m.group(1)
    try:
        text = ALSA_CARDS.read_text()
    except OSError:
        return True
    return re.search(rf"\[{re.escape(name)}\s*\]", text) is not None
