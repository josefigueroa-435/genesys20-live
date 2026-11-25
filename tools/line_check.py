"""GENESYS 20 serial line check (Windows). Usage: py -I g20_linecheck.py COM11 9600 LISTEN_SECONDS

Waits until the port is free, then:
1. reports the modem input lines (CTS, DSR, RI, CD) with DTR/RTS on and off;
2. sends the read-only query MODEL once (watch the GENESYS display for Parity/Framing error);
3. listens, printing every byte and any receive errors (framing/parity/break/overrun), which would
   mean a signal is arriving but at different settings.
Nothing but MODEL is ever sent."""
import ctypes
import sys
import time
from ctypes import wintypes

import serial

port_name, baud, listen_s = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
CE = {0x1: "RXOVER", 0x2: "OVERRUN", 0x4: "PARITY", 0x8: "FRAME", 0x10: "BREAK"}


class COMSTAT(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD), ("cbInQue", wintypes.DWORD), ("cbOutQue", wintypes.DWORD)]


k32 = ctypes.windll.kernel32


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def lines(s):
    return f"CTS={int(s.cts)} DSR={int(s.dsr)} RI={int(s.ri)} CD={int(s.cd)}"


def comm_state(s):
    err, st = wintypes.DWORD(), COMSTAT()
    k32.ClearCommError(wintypes.HANDLE(s._port_handle), ctypes.byref(err), ctypes.byref(st))
    return [n for b, n in CE.items() if err.value & b], st.cbInQue


deadline = time.time() + 600
s = None
say(f"waiting for {port_name} to be free (close or Disconnect GENESYS 20 Live)...")
while time.time() < deadline:
    try:
        s = serial.Serial(port_name, baud, bytesize=8, parity="N", stopbits=1, timeout=0, rtscts=False, xonxoff=False)
        break
    except serial.SerialException:
        time.sleep(1)
if s is None:
    say("RESULT port never became free")
    sys.exit(2)
say(f"opened {port_name} at {baud} 8N1")
time.sleep(0.3)
say(f"lines with DTR=1 RTS=1: {lines(s)}")
s.dtr = False
s.rts = False
time.sleep(0.3)
say(f"lines with DTR=0 RTS=0: {lines(s)}")
s.dtr = True
s.rts = True
time.sleep(0.3)
say(f"lines with DTR=1 RTS=1: {lines(s)}")
comm_state(s)  # clear anything stale

total = 0
errs_seen = {}
last_lines = lines(s)
say("sending MODEL (read-only query) once - watch the GENESYS display")
s.write(b"MODEL\r\n")
end = time.time() + listen_s
say(f"listening {listen_s:.0f} s - press Print on the GENESYS a few times now")
while time.time() < end:
    errs, n = comm_state(s)
    for e in errs:
        errs_seen[e] = errs_seen.get(e, 0) + 1
        say(f"RX ERROR {e}")
    if n:
        data = s.read(n)
        total += len(data)
        say(f"RX {len(data)} bytes: hex={data.hex(' ')} text={data.decode('latin-1')!r}")
    now_lines = lines(s)
    if now_lines != last_lines:
        say(f"lines changed: {now_lines}")
        last_lines = now_lines
    time.sleep(0.05)
s.close()
say(f"RESULT bytes={total} errors={errs_seen or 'none'} final_lines={last_lines}")
