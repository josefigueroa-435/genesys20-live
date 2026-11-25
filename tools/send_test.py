"""Does our transmit wire reach the GENESYS? Usage: py g20_txtest.py COM12
Waits until the CD line is on (adapter screwed onto the GENESYS), then sends the read-only query MODEL
at deliberately wrong settings so the GENESYS should flash 'Parity error' / 'Framing error' on its display
(per its user guide). Also logs anything received."""
import sys
import time

import serial

port_name = sys.argv[1]


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def opened(baud, bits, parity):
    while True:
        try:
            return serial.Serial(port_name, baud, bytesize=bits, parity=parity, stopbits=1, timeout=0, write_timeout=1)
        except serial.SerialException:
            time.sleep(1)


s = opened(9600, 8, "N")
say("waiting for CD=1 (screw the adapter onto the GENESYS)")
t_end = time.time() + 300
while not s.cd and time.time() < t_end:
    time.sleep(0.2)
say(f"CD={int(s.cd)} - starting in 5 s, watch the GENESYS display")
s.close()
time.sleep(5)
plan = [("9600 8N1 (correct settings)", 9600, 8, "N"), ("9600 7E1 (wrong parity)", 9600, 7, "E"),
        ("2400 8N1 (wrong baud)", 2400, 8, "N"), ("19200 8N1 (wrong baud)", 19200, 8, "N")]
for rnd in range(3):
    for label, baud, bits, par in plan:
        p = opened(baud, bits, par)
        say(f"SEND MODEL x3 at {label}")
        for _ in range(3):
            p.write(b"MODEL\r\n")
            time.sleep(0.3)
        t = time.time() + 6
        while time.time() < t:
            n = p.in_waiting
            if n:
                data = p.read(n)
                say(f"RX {len(data)} bytes at {label}: hex={data.hex(' ')} text={data.decode('latin-1')!r}")
            time.sleep(0.05)
        p.close()
say("RESULT done")
