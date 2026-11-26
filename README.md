# GENESYS 20 Live

Live readings from a Thermo Scientific GENESYS 20 over its RS-232 port, with graphs that use what you
know about the solution: Beer-Lambert concentrations, kinetics fits (zero, first and second order),
calibration curves with LOD/LOQ, and point-by-point absorption spectra.

Start **GENESYS 20 Live.bat**. **GENESYS 20 Live (demo).bat** runs a simulated instrument (a fading
dye) so you can try every tab without hardware; everything it shows is labelled SIMULATED.

## Connecting

1. Cable: the GENESYS is a DCE device, so use a **straight-through** DE-9 cable (here through the
   Prolific USB-to-serial adapter, usually COM11).
2. On the GENESYS: **Utility > RS232**. Note baud, data bits, parity, handshake and terminator. In the
   app's **Connection** card, click **Edit** next to Serial settings and set the same (the manual's
   values are 9600, 8, none, 1 stop). The port list finds USB serial adapters by itself, every 2 s,
   and hides Bluetooth serial links.
3. **Utility > Printer setup > Serial**. Optionally Print WL = Yes and Print Units = Yes, so each printout
   carries its wavelength and unit.
4. Click **Connect**. First test, no commands sent: on the GENESYS go to Utility > RS232 and press
   **Print**. Its settings should appear in the **Raw data** tab.
5. Measure something and press **Print**. Compare the reading the app shows with the display and click
   **Yes, it matches** in the Raw data tab. Until then, readings are marked "format not checked".

## Hands-free readings

Tick **Ask for a reading every N s**. The app then sends `SND`, a read-only query that returns the
absorbance to 4 decimals. `SND`, `MODEL` and `REV` aren't in Thermo's user guide, but they were
checked on this GENESYS 20 (model 4001/4, firmware "Revision = 2.10") on 2026-11-25: each query is
echoed, answered, then closed with `OK`, and SND answered about once a second without a miss. The
GENESYS echoes characters as they arrive, so the app sends one query at a time. If the GENESYS
doesn't answer three times, polling turns itself off and Print keeps working.

The app refuses to send anything except these three queries (`genesys.READ_ONLY_COMMANDS`), so it can
never change the instrument's wavelength, blank or settings. The GENESYS 20 has no command to report or
set its wavelength, so type the wavelength you set into the Sample box.

## What you know about the solution (Sample box)

- **Wavelength, path length b, molar absorptivity ε** (and molar mass for mg/L): concentrations
  follow from c = A / (ε b).
- **Or a calibration curve**: in the Calibration tab, read each standard and add it. The fit gives slope,
  R², LOD (3.3 s/slope), LOQ (10 s/slope) and ε = slope / b. Choose "My calibration curve" to convert
  readings with it.
- **Kinetics**: say whether the reactant (colour fades) or the product (colour grows) absorbs. Give
  A∞ (final absorbance) if it isn't zero, or ε and c₀ when the product absorbs. The Kinetics tab plots
  [R], ln[R] and 1/[R] against time, fits all three, and reports k, R², half-life and the initial rate.
  Use **New run** at the moment you mix, and **Mark event** to label additions.
- **Spectrum**: step the wavelength by hand (re-blank at each), type it in, and add the reading.
  λmax is refined with a parabola through the top three points.

## Presets and saved calibration curves

The preset list at the top of the Sample card fills in wavelength, ε and molar mass:

- **Your presets** (★) are saved with **Save...** into the data folder.
- **Reference presets** are published values, each with its source shown under the list:
  - NADH 340 nm, 6,220.
  - p-nitrophenolate 405 nm, 18,300.
  - Ferroin 508 nm, 11,100.
  - Crystal violet 590 nm, 87,000. Literature disagrees on this one, so calibrate it.
  - Six FD&C food dyes, using the official JECFA/FDA absorptivities. mg/L comes straight from the
    published number.
- **Method presets** (Bradford 595 nm, BCA 562 nm, permanganate 525 nm) only set the wavelength and
  switch to "My calibration curve": those methods need standards.
- The Calibration tab saves and reloads named curves ("Saved curves").

## Wavelength scans (Scan tab)

Set the range, step and number of scans (for example 3 scans every 5 min to watch a spectrum change).

- **Blank pass first** reads the blank at every wavelength and subtracts it from each scan
  (single-beam correction). The app pauses for you to swap in the sample.
- **Automatic** (default): the GENESYS moves itself.
  - The app sends `GTO <nm>`, the go-to-wavelength command from Thermo's VISIONlite GENESYS 20 driver,
    which Thermo said to use. It waits for the instrument's OK, waits **Settle** seconds (1.5 s
    works), then reads with `SND`.
  - When a scan ends, the GENESYS returns to the wavelength in the Sample box.
  - Verified on the real instrument (2026-11-25): 480-600 nm in 20 nm steps with a blank pass took
    41 s, and reading the blank twice gave a corrected spectrum within ±0.0007 A of zero.
- **Flagged readings:** when a reading is outside the GENESYS 20's photometric range (−0.1 to 2.5 A),
  the instrument answers the number and then `ER`. The value is kept, drawn as a hollow point, and
  listed in the CSV's `outside_range` column. Blanks often fall below −0.1 A away from the zeroing
  wavelength, and those values are still needed for the subtraction.
- **Guided:**
  - The big prompt says which wavelength to set.
  - Set it with the nm keys and press **Print** (Printer setup = Serial, Print WL = Yes).
  - The printed wavelength is checked against the target.
- The app only ever sends `MODEL`, `REV`, `SND` and `GTO <325-1100>`. The other commands in
  Thermo's driver (ZER, TRN, CCM, ECH) are not used, and the GENESYS 2/5 commands (`WAVELENGTH`,
  `DATA`, `SCAN`) are refused by the GENESYS 20 with `ER`.
- Results: blank-corrected overlay (older scans lighter), λmax per scan, **Export CSV**.

## Saving and sharing runs (Runs tab)

- **Save run...** stores a folder in `<data folder>/runs` with:
  - `report.html`: one page with every chart, to email or print.
  - `readings.csv` and `scan.csv`.
  - The chart images.
  - `run.json`.
- **Runs** tab:
  - **Load into app** reopens a run.
  - **Share (zip)** packs it.
  - **Copy summary** puts a text summary on the clipboard.
  - **Delete** moves it to `runs/_deleted`.
- Point the data folder at a OneDrive or shared folder (**Change...**) and everyone sees the same runs,
  presets and curves.

## Updates

Installed copies check GitHub at start-up. When a newer release exists, a bar offers **Update now**:
- Only changed files are downloaded, each one checked against its SHA-256.
- The old files go to `.update_backup`.
- Settings, logs and the data folder are never touched.

Click the version in the header to check by hand. The development copy (the folder with `.git`) never
updates itself; `py tools/publish.py X.Y.Z "notes"` publishes a release from it.

## Troubleshooting the cable (tools folder)

Run these with the app disconnected (they need the port). Replace COM12 with your port.

- `py tools\loopback_check.py COM12 9600 300` sends `MODEL` once a second for 300 s and logs echoes,
  receive errors and signal-line changes. Bridge pins 2 and 3 of the 9-pin end that normally plugs
  into the GENESYS: an echo means the adapter (and cable, if bridged at its far end) can carry data.
  A 2-3 loopback echoes whether or not the wiring is swapped.
- `py tools\send_test.py COM12` waits until CD is on (adapter on the GENESYS), then sends `MODEL` at
  correct and deliberately wrong settings. If our wire reaches the GENESYS, its display flashes
  "Parity error" / "Framing error" (user guide p. 12).
- `py tools\line_check.py COM12 9600 120` shows the signal lines and listens while you press Print.

What was found on 2026-11-25 with the DTECH USB adapter (female DB9, Prolific PL2303 `067B:2303`):

- Plugged straight onto the GENESYS: no data in either direction, and no Parity/Framing error on the
  GENESYS display, although the CD line follows the GENESYS (on when attached).
- Holes 2-3 bridged (with solid contact): clean echoes, so the adapter itself works.
- Holes 2-7 bridged: CTS blinked in time with each transmitted `MODEL` and there were no receive errors,
  so **the adapter transmits on pin 2**, i.e. it is wired as DCE. The GENESYS is also DCE (it
  transmits on pin 2, service manual p. 4-12), so transmit meets transmit.
- **Fix (confirmed working):** a **DB9 null-modem adapter, male-to-female**, between the DTECH and
  the GENESYS (a missing pin 9, Ring Indicator, doesn't matter). Chain: USB, DTECH, null-modem, GENESYS.
  Alternative: a standard USB-RS232 adapter with a male DB9 plus a female-female gender changer
  (Thermo's own cable is a straight-through female-female, part 336042).
- The adapter once failed to start (Code 10) and came back on re-plugging. The COM number changes when
  you use a different USB socket (COM11 vs COM12), so press the refresh button next to Port.

## Files

- `logs/raw-*.jsonl`: every byte in and out, with time stamps (written as it arrives).
- `logs/readings-*.csv`: every parsed reading (time, source, A, %T, concentration, raw text).
- **Save CSV...** saves the current run plus its event marks.
- Settings live in `settings.json` (the demo uses `settings.demo.json`).

## Code

`genesys.py` serial settings, line splitting, parser, raw log, simulator ·
`link.py` worker thread that owns the port · `analysis.py` Beer-Lambert, calibration, kinetics,
λmax · `app.py` the window · `genesys20_reader.py` the original command-line listener.
Tests: `py -m unittest test_core test_app` (the second opens a window briefly).
