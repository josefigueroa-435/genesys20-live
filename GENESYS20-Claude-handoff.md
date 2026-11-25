# Build a Windows reader for the Thermo Scientific GENESYS 20

Give Claude this file and `genesys20_reader.py`. The Python file is original host-side code, not firmware extracted from the spectrophotometer. The initial goal is to capture a reading when the operator presses Print, not poll the device or control its lamp, calibration, or wavelength.

## First capture on the laptop

1. Save the Python file in a folder and open PowerShell there. With Python 3 installed, run:

   ```powershell
   py -m pip install pyserial==3.5
   py genesys20_reader.py --port COM11 --baud 9600 --log capture.jsonl
   ```

2. In **Utility → Printer Setup**, choose **Serial**. Return to the measurement display, note exactly what it shows, then press **Print**. Thermo documents the output selection, optional wavelength and units, decimal separator, and configurable serial settings in the [GENESYS 20 user guide, pp. 6–7](https://www.wpi.edu/sites/default/files/2025-07/269-223100---Rev-A---GENESYS-20-User-Guide.pdf).

3. Look for JSON output, not just the “Opened” status. Press Ctrl+C to stop. `capture.jsonl` contains timestamps, display-oriented text and the exact bytes as hex. No instrument commands are sent. The log appends across runs; use a fresh filename when changing settings.

4. **If nothing arrives:** COM11 opened successfully on this laptop, but the earlier 9600-baud listen timed out. Neither the instrument's serial settings nor its output format has been confirmed. Match the panel settings with `--baud`, `--bits`, `--parity`, `--stop`, and `--flow` (`none`, `rtscts`, or `xonxoff`). Close any other serial reader before retrying. If settings match and the port is still silent, check the cable: the [GENESYS 20 service manual](https://www.isu.edu/media/libraries/college-of-science-and-engineering/biology/biolab/equipment/G20SpecManual.pdf) specifies a straight-through PC connection.

## Prompt for Claude

> Build a local Windows desktop app around the attached Python reader. Let me choose a COM port and serial settings, connect/disconnect, see incoming raw text, and save a timestamped session. Keep port I/O off the UI thread and release the port on disconnect. Distinguish “port open, waiting for Print” from “bytes received” and “verified measurement.” Preserve raw bytes even when decoding fails. Do not send commands to the instrument. Start with the raw capture viewer; ask me for a real capture and its matching front-panel reading before implementing measurement parsing or CSV fields. A chunk may contain partial lines or several lines; handle CR, LF and CRLF across chunk boundaries. Never infer missing units or wavelength. Label any demonstration data as simulated.

---

## Test status recorded in the Perplexity session (copied 2026-11-25)

Source: Perplexity session "Thermo Scientific GENESYS Visibility", https://www.perplexity.ai/computer/tasks/8c9e73cf-cfb1-4703-bcf0-f218e112cbdc

- **Other files:** none attached.
- **Port and baud:** Windows identified a Prolific USB-to-Serial adapter on `COM11`, and the port opened. A passive listen at `9600` baud received no bytes. `9600`, 8 data bits, no parity, 1 stop bit and no flow control are reader defaults, not verified instrument settings.
- **Protocol and commands:** the reader passively listens for RS-232 output when Printer Setup is set to **Serial** and the operator presses **Print**. It sends no commands. The session did not verify an automatic read command or the instrument's actual output format.
- **What passed:** the reader passed a simulated serial test. The instrument's "Insufficient Energy" startup error cleared after the cuvette was removed.
- **Still unconfirmed:** whether Printer Setup was set to **Serial**, whether the instrument can transmit to the laptop, and whether any received bytes match the front-panel reading. The stated goal is hands-free live readings, but the first milestone remains one real **Print** capture matched to the display.
