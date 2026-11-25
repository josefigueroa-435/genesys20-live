"""Ask the GENESYS 20 the read-only queries, then listen for Print. Usage:
    py tools\\query_and_listen.py COM12 [listen_seconds]
Sends MODEL, REV and SND (x3), printing every reply line, then logs everything that arrives while you
press Print. Only the read-only queries MODEL, REV and SND are ever sent."""
import sys
import time

import serial

port_name = sys.argv[1]
listen_s = float(sys.argv[2]) if len(sys.argv) > 2 else 120


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def drain(s, seconds):
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        n = s.in_waiting
        if n:
            data = s.read(n)
            buf += data
            say(f"RX hex={data.hex(' ')} text={data.decode('latin-1')!r}")
        time.sleep(0.03)
    return buf


s = serial.Serial(port_name, 9600, bytesize=8, parity="N", stopbits=1, timeout=0, write_timeout=1)
say(f"opened {port_name} 9600 8N1")
drain(s, 0.5)
for cmd in ("MODEL", "REV", "SND", "SND", "SND"):
    say(f"SEND {cmd}")
    s.write(cmd.encode() + b"\r\n")
    drain(s, 2.0)
say(f"listening {listen_s:.0f} s - press Print on the GENESYS (measurement screen, then Utility > RS232)")
drain(s, listen_s)
s.close()
say("RESULT done")
