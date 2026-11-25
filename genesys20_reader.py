"""GENESYS 20 serial print reader. Independent code, not Thermo firmware.

Windows: py -m pip install pyserial==3.5
Then:    py genesys20_reader.py --port COM11 --baud 9600 --log capture.jsonl
Select Serial under Printer Setup and press Print on the instrument.
Stop with Ctrl+C. stdout is JSON Lines; status and errors go to stderr.
"""
import argparse
import json
import sys
from contextlib import ExitStack
from datetime import datetime, timezone


def make_event(data, port, baud):
    """A chunk is not necessarily a complete printout or even a full line."""
    return {"received_utc": datetime.now(timezone.utc).isoformat(),
            "port": port, "baud": baud, "hex": data.hex(),
            "text": data.decode("ascii", errors="backslashreplace")}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM11")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--bits", type=int, choices=(7, 8), default=8)
    ap.add_argument("--parity", choices=("N", "E", "O"), default="N")
    ap.add_argument("--stop", type=int, choices=(1, 2), default=1)
    ap.add_argument("--flow", choices=("none", "rtscts", "xonxoff"), default="none")
    ap.add_argument("--log", help="Append JSON Lines to this file, flushing each chunk")
    args = ap.parse_args()
    try:
        import serial
    except ImportError:
        sys.exit("Install pySerial first: py -m pip install pyserial==3.5")
    try:
        with ExitStack() as stack:
            port = stack.enter_context(serial.Serial(
                args.port, args.baud, bytesize=args.bits, parity=args.parity,
                stopbits=args.stop, timeout=0.25, rtscts=args.flow == "rtscts",
                xonxoff=args.flow == "xonxoff"))
            log = stack.enter_context(open(args.log, "a", encoding="utf-8")) if args.log else None
            print(f"Opened {args.port}. Waiting for bytes; press Print. Ctrl+C to stop.",
                  file=sys.stderr, flush=True)
            while True:
                data = port.read(1024)
                if not data:
                    continue
                line = json.dumps(make_event(data, args.port, args.baud))
                if log:
                    print(line, file=log, flush=True)
                print(line, flush=True)
    except KeyboardInterrupt:
        return 0
    except (OSError, serial.SerialException, ValueError) as exc:
        print(f"Capture stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
