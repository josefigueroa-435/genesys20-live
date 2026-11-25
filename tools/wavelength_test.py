"""One-off test: does this GENESYS 20 accept the GENESYS 2/5 command WAVELENGTH?
Usage: py tools\\wavelength_test.py [COMx] [restore_nm]

Sends, one at a time and waiting for OK/ERR after each:
  WAVELENGTH          (query: report the wavelength)
  DATA                (query: the reading, like SND)
  SND                 (reading before)
  WAVELENGTH 550      (set: the display should change to 550 nm)
  SND                 (reading after)
  WAVELENGTH <orig>   (put it back: the queried value, or restore_nm if the query is not supported)
Nothing else is ever sent. Close/Disconnect GENESYS 20 Live first.
"""
import sys
import time

import serial
from serial.tools import list_ports

TEST_NM = 550


def find_port():
    for p in list_ports.comports():
        if any(w in (p.description or "").lower() for w in ("prolific", "usb-to-serial", "ftdi", "ch340")):
            return p.device
    return None


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def ask(s, cmd, timeout=15.0):
    """Send one command; return the answer lines (echo, OK/ERR removed) and the final status."""
    s.reset_input_buffer()
    s.write(cmd.encode("ascii") + b"\r\n")
    buf, end = b"", time.time() + timeout
    while time.time() < end:
        n = s.in_waiting
        if n:
            buf += s.read(n)
            lines = [ln.strip() for ln in buf.replace(b"\r", b"\n").split(b"\n")]
            words = [ln.decode("latin-1") for ln in lines if ln]
            if words and words[-1] in ("OK", "ER", "ERR") and buf.endswith((b"\n", b"\r")):
                status = words[-1]
                answer = [w for w in words[:-1] if w.upper() != cmd.upper()]
                say(f"-> {cmd:16s} <- {answer}  [{status}]   raw={buf!r}")
                return answer, status
        time.sleep(0.03)
    say(f"-> {cmd:16s} <- (no OK/ERR within {timeout:.0f} s)   raw={buf!r}")
    return None, None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].upper().startswith("COM") else find_port()
    restore = next((float(a) for a in sys.argv[1:] if a.replace(".", "", 1).isdigit()), None)
    if not port:
        sys.exit("No USB-serial adapter found.")
    s, end = None, time.time() + 180
    say(f"waiting for {port} (click Disconnect in GENESYS 20 Live)")
    while s is None and time.time() < end:
        try:
            s = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0, write_timeout=2)
        except serial.SerialException:
            time.sleep(1)
    if s is None:
        sys.exit(f"{port} stayed busy for 3 minutes; nothing was sent.")
    say(f"opened {port} 9600 8N1")
    time.sleep(0.5)
    try:
        wl_answer, wl_status = ask(s, "WAVELENGTH")
        original = None
        if wl_status == "OK" and wl_answer:
            try:
                original = float(wl_answer[-1].split()[0])
            except ValueError:
                pass
        ask(s, "DATA")
        before, _ = ask(s, "SND")
        _, set_status = ask(s, f"WAVELENGTH {TEST_NM}", timeout=20)
        time.sleep(1.0)
        after, _ = ask(s, "SND")
        if set_status == "OK":
            ask(s, "WAVELENGTH")
        back = original if original is not None else restore
        if set_status == "OK" and back:
            ask(s, f"WAVELENGTH {back:g}", timeout=20)
        say(f"RESULT query={'yes ' + str(original) if original is not None else 'no'} "
            f"set={set_status} before={before} after={after} restored_to={back if set_status == 'OK' else '-'}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
