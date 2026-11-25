"""The serial link: one worker thread owns the port, splits what arrives into lines, and (only when
asked) sends the read-only queries SND, MODEL and REV.

How the real GENESYS 20 answers (firmware 2.10, captured 2026-11-25 at 9600 8N1, CR+LF):
    -> SND\\r\\n
    <- SND\\r\\n        echo of the query
    <- 0.0003\\r\\n     the answer (absorbance, 4 decimals)
    <- OK\\r\\n         end of the answer
MODEL answers "GENESYS 20", REV answers "GENESYS 20 Revision = 2.10". The echo is skipped, answer
lines are tagged with their query, and OK closes the query. A query that gets an answer but no OK
is closed after ANSWER_IDLE_S of quiet, so firmware that leaves out the OK still works.

The GENESYS echoes every character the moment it arrives, even in the middle of an answer (two
queries sent back to back came out as "GENESRYS 20" / "OKE"). So queries wait in an outbox and only
one is ever in flight: the next is written after the previous one's OK (or timeout).

Events for the GUI arrive on `Link.events` as tuples:
    ("in", t, bytes)                    raw chunk received
    ("out", t, bytes)                   raw query sent
    ("line", t, bytes, source, cmd)     a complete line; source is "print" or "reply" (cmd = the query)
    ("timeout", t, cmd)                 a query got no answer in time
    ("refused", t, cmd)                 the instrument answered ER/ERR (command not supported)
    ("error", t, message)               the port failed; the link has closed itself
    ("closed", t)
t is time.time().
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque

from genesys import READ_ONLY_COMMANDS, TERMINATORS, LineAssembler, SerialSettings

REPLY_TIMEOUT_S = 3.0      # no answer at all within this -> timeout
ANSWER_IDLE_S = 0.8        # answered but no OK: close the query after this much quiet
IDLE_FLUSH_S = 0.4


class _Query:
    __slots__ = ("cmd", "t_sent", "answered", "t_last")

    def __init__(self, cmd: str, t_sent: float):
        self.cmd = cmd
        self.t_sent = t_sent
        self.answered = False
        self.t_last = t_sent


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
        self._pending: deque = deque()    # the query in flight (at most one)
        self._outbox: deque = deque()     # queries waiting their turn
        self._wl_re = None                # set-wavelength command shape, only if the user configured one
        self._wl_parts = None
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
        self._outbox.clear()
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

    def allow_wavelength_command(self, template: str | None) -> None:
        """Permit exactly one extra command shape: a set-wavelength command typed in by the user from
        Thermo's RS232 command set, e.g. "XYZ {wl}". Only whole-number wavelengths 325-1100 can be
        substituted; anything else is still refused. None switches it off."""
        self._wl_re = None
        self._wl_parts = None
        if not template:
            return
        if template.count("{wl}") != 1 or len(template) > 24 or not all(32 <= ord(c) < 127 for c in template):
            raise ValueError("the command must be plain text with {wl} exactly once (max 24 characters)")
        pre, post = template.split("{wl}")
        self._wl_parts = (pre, post)
        self._wl_re = re.compile(re.escape(pre) + r"(\d{3,4})" + re.escape(post))

    def wavelength_command(self, wl: float) -> str:
        if not getattr(self, "_wl_parts", None):
            raise ValueError("no set-wavelength command has been configured")
        wl = int(round(wl))
        if not 325 <= wl <= 1100:
            raise ValueError("wavelength outside 325-1100 nm")
        pre, post = self._wl_parts
        return f"{pre}{wl}{post}"

    def _allowed(self, cmd: str) -> bool:
        if cmd in READ_ONLY_COMMANDS:
            return True
        rx = getattr(self, "_wl_re", None)
        m = rx.fullmatch(cmd) if rx else None
        return bool(m) and 325 <= int(m.group(1)) <= 1100

    def send(self, cmd: str) -> None:
        """Queue a read-only query (or the configured set-wavelength command); anything else is
        refused. The worker thread writes it as soon as no other query is in flight."""
        if not self._allowed(cmd):
            raise ValueError(f"{cmd!r} is not one of the read-only queries {READ_ONLY_COMMANDS}")
        if self._port is None:
            raise RuntimeError("not connected")
        with self._lock:
            self._outbox.append(cmd)

    def _write_next(self, port) -> None:
        """Worker thread: write the next queued query if nothing is in flight."""
        with self._lock:
            if self._pending or not self._outbox:
                return
            cmd = self._outbox.popleft()
            data = cmd.encode("ascii") + TERMINATORS[self.settings.terminator]
            port.write(data)
            self._pending.append(_Query(cmd, time.monotonic()))
        self.events.put(("out", time.time(), data))

    def _loop(self) -> None:
        asm = LineAssembler()
        last_rx = time.monotonic()
        while not self._stop.is_set():
            port = self._port
            if port is None:
                break
            try:
                self._write_next(port)
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
                    waiting = any(q.cmd == "SND" for q in self._pending) or "SND" in self._outbox
                if not waiting:
                    try:
                        self.send("SND")
                    except Exception as exc:
                        self.events.put(("error", time.time(), f"could not send SND: {exc}"))

    def _emit_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        now = time.monotonic()
        with self._lock:
            q = self._pending[0] if self._pending else None
            if q is not None:
                word = text.decode("latin-1").upper()
                if word == q.cmd and not q.answered:
                    q.t_last = now                 # the instrument echoing our query
                    return
                if word == "OK":
                    self._pending.popleft()        # end of this query's answer
                    if not q.answered:
                        self.events.put(("timeout", time.time(), q.cmd))
                    return
                if word in ("ER", "ERR"):          # refused (GENESYS 20 says "ER", GENESYS 2/5 "ERR")
                    self._pending.popleft()
                    self.events.put(("refused", time.time(), q.cmd))
                    return
                q.answered = True
                q.t_last = now
        self.events.put(("line", time.time(), line, "reply" if q else "print", q.cmd if q else None))

    def _expire(self, now: float) -> None:
        with self._lock:
            while self._pending:
                q = self._pending[0]
                if q.answered and now - q.t_last > ANSWER_IDLE_S:
                    self._pending.popleft()        # answered, no OK came: close it quietly
                elif not q.answered and now - q.t_sent > REPLY_TIMEOUT_S:
                    self._pending.popleft()
                    self.events.put(("timeout", time.time(), q.cmd))
                else:
                    break
