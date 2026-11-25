"""The serial link: one worker thread owns the port, splits what arrives into lines, and (only when
asked) sends the read-only queries SND, MODEL and REV.

Events for the GUI arrive on `Link.events` as tuples:
    ("in", t, bytes)                    raw chunk received
    ("out", t, bytes)                   raw query sent
    ("line", t, bytes, source, cmd)     a complete line; source is "print" or "reply" (cmd = the query)
    ("timeout", t, cmd)                 a query got no reply in time
    ("error", t, message)               the port failed; the link has closed itself
    ("closed", t)
t is time.time().
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque

from genesys import READ_ONLY_COMMANDS, TERMINATORS, LineAssembler, SerialSettings

REPLY_TIMEOUT_S = 2.5
IDLE_FLUSH_S = 0.4


def open_serial(s: SerialSettings):
    import serial  # imported here so the rest works (and tests run) without pyserial

    return serial.Serial(
        s.port, s.baud, bytesize=s.bits, parity=s.parity, stopbits=s.stop, timeout=0.05,
        write_timeout=1.0, rtscts=s.flow == "rtscts", xonxoff=s.flow == "xonxoff")


class Link:
    def __init__(self):
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.settings: SerialSettings | None = None
        self.poll_interval = 0.0          # seconds between SND queries; 0 = off
        self._port = None
        self._lock = threading.Lock()     # guards writes and _pending
        self._pending: deque = deque()    # (cmd, t_sent)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_poll = 0.0

    @property
    def is_open(self) -> bool:
        return self._port is not None

    def open(self, settings: SerialSettings, port=None) -> None:
        """Open the port (or adopt `port`, e.g. the simulator) and start the reader thread."""
        if self.is_open:
            self.close()
        self.settings = settings
        self._port = port if port is not None else open_serial(settings)
        self._pending.clear()
        self._stop.clear()
        self._next_poll = time.monotonic() + 0.5
        self._thread = threading.Thread(target=self._loop, name="genesys-link", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        th, self._thread = self._thread, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=2)
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
            self.events.put(("closed", time.time()))

    def send(self, cmd: str) -> None:
        """Send one of the read-only queries. Anything else is refused."""
        if cmd not in READ_ONLY_COMMANDS:
            raise ValueError(f"{cmd!r} is not one of the read-only queries {READ_ONLY_COMMANDS}")
        port = self._port
        if port is None:
            raise RuntimeError("not connected")
        data = cmd.encode("ascii") + TERMINATORS[self.settings.terminator]
        with self._lock:
            port.write(data)
            self._pending.append((cmd, time.monotonic()))
        self.events.put(("out", time.time(), data))

    def _loop(self) -> None:
        asm = LineAssembler()
        last_rx = time.monotonic()
        while not self._stop.is_set():
            port = self._port
            if port is None:
                break
            try:
                data = port.read(256)
            except Exception as exc:  # unplugged, driver error...
                self.events.put(("error", time.time(), f"{type(exc).__name__}: {exc}"))
                self._port = None
                try:
                    port.close()
                except Exception:
                    pass
                self.events.put(("closed", time.time()))
                return
            now = time.monotonic()
            if data:
                last_rx = now
                self.events.put(("in", time.time(), data))
                for line in asm.feed(data):
                    self._emit_line(line)
            elif asm.pending and now - last_rx > IDLE_FLUSH_S:
                self._emit_line(asm.flush())
            self._expire(now)
            if self.poll_interval > 0 and now >= self._next_poll:
                self._next_poll = now + self.poll_interval
                with self._lock:
                    waiting = any(c == "SND" for c, _ in self._pending)
                if not waiting:
                    try:
                        self.send("SND")
                    except Exception as exc:
                        self.events.put(("error", time.time(), f"could not send SND: {exc}"))

    def _emit_line(self, line: bytes) -> None:
        if not line.strip():
            return
        with self._lock:
            cmd = self._pending.popleft()[0] if self._pending else None
        self.events.put(("line", time.time(), line, "reply" if cmd else "print", cmd))

    def _expire(self, now: float) -> None:
        with self._lock:
            while self._pending and now - self._pending[0][1] > REPLY_TIMEOUT_S:
                cmd, _ = self._pending.popleft()
                self.events.put(("timeout", time.time(), cmd))
