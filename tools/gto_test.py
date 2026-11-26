"""Test the GENESYS 20 go-to-wavelength command used by Thermo's VISIONlite GENESYS 20 driver.
Usage: py tools\\gto_test.py [test_nm] [restore_nm]

Sends one at a time, waiting for OK/ER after each:  SND, GTO <test>, SND, GTO <restore>, SND.
If the plain whole-number form is refused (ER), tries "<n>.0" once. Nothing else is sent.
"""
import sys
import time

import serial
from serial.tools import list_ports


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def ask(s, cmd, timeout=20.0):
    s.reset_input_buffer()
    t0 = time.time()
    s.write(cmd.encode("ascii") + b"\r\n")
    buf = b""
    while time.time() - t0 < timeout:
        n = s.in_waiting
        if n:
            buf += s.read(n)
            words = [w.strip().decode("latin-1") for w in buf.replace(b"\r", b"\n").split(b"\n") if w.strip()]
            if words and (words[-1] == "OK" or words[-1].startswith("ER")) and buf.endswith((b"\n", b"\r")):
                ans = [w for w in words[:-1] if w.upper() != cmd.upper()]
                say(f"-> {cmd:10s} <- {ans} [{words[-1]}] after {time.time() - t0:.1f} s   raw={buf!r}")
                return ans, words[-1]
        time.sleep(0.03)
    say(f"-> {cmd:10s} <- no OK/ER in {timeout:.0f} s   raw={buf!r}")
    return None, None


def main():
    nums = [float(a) for a in sys.argv[1:]]
    test_nm, restore_nm = (nums + [550, 530])[:2]
    port = next((p.device for p in list_ports.comports()
                 if any(w in (p.description or "").lower() for w in ("prolific", "usb-to-serial", "ftdi"))), None)
    if not port:
        sys.exit("No USB-serial adapter found.")
    s, end = None, time.time() + 180
    say(f"waiting for {port} (Disconnect GENESYS 20 Live if it's connected)")
    while s is None and time.time() < end:
        try:
            s = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0, write_timeout=2)
        except serial.SerialException:
            time.sleep(1)
    if s is None:
        sys.exit("Port stayed busy; nothing was sent.")
    time.sleep(0.5)
    try:
        before, _ = ask(s, "SND")
        fmt = "{:.0f}"
        _, st = ask(s, f"GTO {test_nm:.0f}")
        if st and st.startswith("ER"):
            fmt = "{:.1f}"
            _, st = ask(s, f"GTO {test_nm:.1f}")
        moved, _ = ask(s, "SND")
        if st == "OK":
            ask(s, "GTO " + fmt.format(restore_nm))
            back, _ = ask(s, "SND")
        else:
            back = None
        say(f"RESULT gto={'OK, format ' + fmt if st == 'OK' else st} A_before={before} A_at_{test_nm:.0f}={moved} "
            f"A_back_at_{restore_nm:.0f}={back}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
