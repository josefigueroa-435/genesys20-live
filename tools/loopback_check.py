"""Guided cable test for the GENESYS 20 link. Usage: py g20_guided.py COM11 9600 SECONDS
Sends the read-only query MODEL once a second and logs, with time stamps, every received byte,
receive errors and modem-line changes. In a loopback (pins 2-3 bridged) the MODEL text echoes back."""
import ctypes
import sys
import time
from ctypes import wintypes

import serial

port_name, baud, run_s = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
CE = {0x1: "RXOVER", 0x2: "OVERRUN", 0x4: "PARITY", 0x8: "FRAME", 0x10: "BREAK"}


class COMSTAT(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD), ("cbInQue", wintypes.DWORD), ("cbOutQue", wintypes.DWORD)]


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def lines(s):
    return f"CTS={int(s.cts)} DSR={int(s.dsr)} RI={int(s.ri)} CD={int(s.cd)}"


def comm_state(s):
    err, st = wintypes.DWORD(), COMSTAT()
    ctypes.windll.kernel32.ClearCommError(wintypes.HANDLE(s._port_handle), ctypes.byref(err), ctypes.byref(st))
    return [n for b, n in CE.items() if err.value & b], st.cbInQue


def open_port(deadline):
    while time.time() < deadline:
        try:
            p = serial.Serial(port_name, baud, bytesize=8, parity="N", stopbits=1, timeout=0, write_timeout=0.5)
            say(f"opened {port_name} {baud} 8N1, DTR=1 RTS=1; {lines(p)}")
            comm_state(p)
            return p
        except serial.SerialException:
            time.sleep(1)
    return None


end = time.time() + run_s
s = open_port(end)
if s is None:
    say("RESULT port never became free")
    sys.exit(2)
next_send = 0.0
next_beat = time.time() + 10
last = lines(s)
echoes = 0
while time.time() < end:
    now = time.time()
    try:
        if now >= next_send:
            next_send = now + 1.0
            s.write(b"MODEL\r\n")
        errs, n = comm_state(s)
        cur = lines(s)
    except (serial.SerialException, OSError) as exc:
        say(f"PORT LOST ({type(exc).__name__}) - USB adapter unplugged? waiting for it to come back")
        try:
            s.close()
        except Exception:
            pass
        s = open_port(end)
        if s is None:
            break
        last = lines(s)
        continue
    for e in errs:
        say(f"RX ERROR {e}")
    if n:
        try:
            data = s.read(n)
        except (serial.SerialException, OSError):
            continue
        if b"MODEL" in data:
            echoes += 1
            if echoes in (1, 5) or echoes % 20 == 0:
                say(f"ECHO of our MODEL ({echoes} so far): {data!r}")
        else:
            say(f"RX {len(data)} bytes: hex={data.hex(' ')} text={data.decode('latin-1')!r}")
    if cur != last:
        say(f"lines changed: {cur}")
        last = cur
    if now >= next_beat:
        say(f"... {cur}, echoes so far {echoes}")
        next_beat = now + 10
    time.sleep(0.05)
if s is not None:
    s.close()
say(f"RESULT echoes={echoes} final_lines={last}")
