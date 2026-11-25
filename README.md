# GENESYS 20 Live

Live readings from a Thermo Scientific GENESYS 20 over its RS-232 port, with graphs that use what you
know about the solution: Beer-Lambert concentrations, kinetics fits (zero, first and second order),
calibration curves with LOD/LOQ, and point-by-point absorption spectra.

Start **GENESYS 20 Live.bat**. **GENESYS 20 Live (demo).bat** runs a simulated instrument (a fading
dye) so you can try every tab without hardware; everything it shows is labelled SIMULATED.

## Connecting

1. Cable: the GENESYS is a DCE device, so use a **straight-through** DE-9 cable (here through the
   Prolific USB-to-serial adapter, usually COM11).
2. On the GENESYS: **Utility > RS232**. Note baud, data bits, parity, handshake and terminator, and set
   the same in the app's top bar (the manual's values are 9600, 8, none, 1 stop).
3. **Utility > Printer setup > Serial**. Optionally Print WL = Yes and Print Units = Yes, so each printout
   carries its wavelength and unit.
4. Click **Connect**. First test, no commands sent: on the GENESYS go to Utility > RS232 and press
   **Print**. Its settings should appear in the **Raw data** tab.
5. Measure something and press **Print**. Compare the reading the app shows with the display and click
   **Yes, it matches** in the Raw data tab. Until then, readings are marked "format not checked".

## Hands-free readings

Tick **Ask for a reading every N s**. The app then sends `SND`, a read-only query that returns the
absorbance. `SND`, `MODEL` and `REV` come from a third-party description of the GENESYS 20 (firmware
1.11), not from Thermo's user guide, so they may not work on every unit. If the GENESYS doesn't answer
three times, polling turns itself off and Print keeps working.

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
