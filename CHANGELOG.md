# ToLiss EFB – Changelog

## Installation

1. Copy `PI_ToLissWebTablet.py` to `X-Plane 12/Resources/plugins/PythonPlugins/`.
2. Copy `index.html` to `X-Plane 12/Resources/plugins/PythonPlugins/ToLissWebTablet/`.
3. Restart X-Plane, or use XPPython3's "Reload scripts".
4. Check **Settings → About** shows the new version. The page is no longer cached by browsers, so a normal reload picks up updates.

On first start the plugin builds two indexes in the background and saves them in the plugin folder:
`apt_index_cache.json` (airports) and `navdata_cache.pkl` (waypoints and navaids). Later starts load them in well under a second.
They rebuild automatically when your scenery or navigation data (e.g. a Navigraph AIRAC update) changes, and can be deleted safely.

**Recommended ToLiss ISCS setting:** enable **"X-Plane pause acts as freeze motion"**. The approach trainer pauses the sim
as soon as the aircraft is positioned and finishes setting it up while paused, which needs the aircraft systems to keep running.

---

## 0.59

### New pages and features
- **Flight Recorder** (new page in the Flight group):
  - Records 62 channels up to 10 times a second to CSV in `PythonPlugins/ToLissWebTablet/fdr/`: position, altitudes
    (MSL, baro, radio), speeds (IAS, TAS, GS, Mach, V/S), attitude, track, angle of attack, g loads, sidestick and rudder,
    brakes, thrust levers, N1, EGT, fuel flow, reversers, fuel and weight, flaps/slats, gear, speedbrake, AP/FD/A/THR and
    APPR/LOC, FCU targets, wind, OAT, baro setting, master warning/caution and ground contact.
  - Event log: every ECAM E/WD message as it appears and clears (with its colour), master warning/caution, liftoff,
    touchdown, autopilot engage/disengage, gear and flap selections, and repositioning jumps.
  - Manual start/stop by default, or an automatic mode (start when engines run or airborne, stop 30 s after shutdown).
    Recording runs in the plugin, so it continues with the EFB closed, and pauses while the sim is paused.
  - Landing report for every landing: landing rate at contact, touchdown g, runway identified automatically, touchdown
    distance from the landing threshold, centreline offset, height and speed over the threshold, pitch, roll, crab, bounces,
    rollout distance and time, reversers, and stabilised-approach checks at 1,000 ft and 500 ft.
  - Flight review: map track with ECAM and touchdown markers, charts (altitude/IAS, V/S/pitch/roll, sidestick/g load) and
    a filterable event table. Downloads: full CSV, events CSV and KML for Google Earth.
- **Cockpit Displays** (new page in the Flight group, with its own Settings switch):
  - **Live data stream** from the plugin, 15 updates a second, only while the page is open.
  - **PFD:** attitude with pitch ladder, roll scale and sideslip index; speed tape with selected/managed speed, speed trend
    and VLS/VSW/VMAX bands where ToLiss provides them; Mach; altitude tape with selected altitude, QNH/STD and ground
    reference; radio height; V/S scale; heading tape with track and selected heading; flight director; ILS deviation
    with LS on; the FMA text from ToLiss; and a **sidestick position indicator** (always, on the ground only, or off).
  - **E/WD:** N1 and EGT dials, N2 and fuel flow (2 or 4 engines), fuel on board, flap/slat indicator with CONF,
    and the ECAM message and memo lines in their colours.
  - **SD (lower ECAM):** always shows the page selected on the ToLiss ECAM (including automatic page calls), drawn in
    the aircraft's layout: ENG, FUEL, WHEEL, F/CTL, CAB PRESS, CRUISE and STATUS, with the permanent TAT / SAT / UTC / GW
    strip. BLEED, ELEC, HYD, APU, COND and DOOR show their title and any ToLiss text for now. Values not available show as XX.
  - PFD laid out like the ToLiss PFD pop-out, with FMA text placed in its five columns (MDA/DH shown as BARO/RADIO).
  - Show any combination of PFD, E/WD and SD, and a full-screen mode.
- **Updates** (Settings → Updates): the EFB checks the official GitHub releases, shows when a newer version is available
  (with a dot on Settings in the sidebar) and its release notes, and installs it with one tap while parked. Downloads are
  verified against their published SHA-256 checksum, the previous version is backed up, and **Undo last update** restores
  it. Automatic checks can be switched off.
- **Custom checklists** (Settings → Airbus Checklist): upload your own checklist in the ToLiss `checklist.xml` format and choose
  between it and the ToLiss default. Uploads are checked with exactly the same parser the checklist page uses; a file that
  cannot be read is rejected with the reason (e.g. the line and column of an XML error) and nothing is saved.
  Checklists are stored in `PythonPlugins/ToLissWebTablet/checklists/`, your choice is remembered, and several can be kept.
- **Scratchpad drawing:** the same pens as the map (magenta, cyan, green, white), **Undo** and **Clear**; works with mouse,
  finger and Apple Pencil, and drawings are kept between sessions.
- **Settings reorganised** into Preflight, Flight, Sim control, Network and External Tools, matching the sidebar.
  New **Flight Recorder** switch: when off, its page is hidden and nothing is recorded (automatic recording included).
  "External Charting Links" is now "External tool links", and the About text has a clearer note on the free beta software.
- **Modern fonts:** Inter for the interface and JetBrains Mono for data (via Google Fonts), falling back to SF Pro / SF Mono
  on Apple devices and Segoe UI / Cascadia on Windows when offline, instead of Arial and Courier New. Buttons and inputs
  now use the same font. The MCDU keeps its own font.

### Approach trainer (Aircraft Repositioning → Approach)
- **Approach database** read from X-Plane's own procedure files (CIFP), using Navigraph data in *Custom Data* first, as ToLiss does:
  ILS, LOC, GLS, RNAV, RNP, VOR, NDB and others, with transitions and missed approaches.
- **Procedure plots** on the map: transitions (cyan), final (magenta), missed approach (green), holds, curved RF legs and DME arcs,
  fix names, roles (IAF, IF, FAF, MAP) and altitude constraints, plus a leg-by-leg table.
- **Three ways to start:** *On final* (2–20 nm on the real final course and published glide path), *Intercept* (join at 6–15 nm,
  from the left or right, 20°/30°/45°, below the path) and *Full procedure* (2–10 nm before the IF or a transition's first fix).
  *Centreline* offers a plain 3° approach to any runway without a published approach.
- **MCDU programming:** INIT FROM/TO, approach and VIA selection, insert, PERF APPR (QNH, temperature, magnetic wind from the
  VATSIM METAR, BARO minimum), DIR TO the first fix for full procedures (with a check that the procedure's legs follow it),
  and optional activation of the approach phase. Works from the screen text, so it tolerates layout differences and checks
  for MCDU error messages after each entry.
- **Aircraft setup:** optional engine start (ToLiss "Engines Running" state), flaps and gear for the start point, FCU speed,
  heading (converted to magnetic) and altitude, both FDs, A/THR (with a warning if the thrust levers are at IDLE), AP1, and APPR or LOC armed.
- Live progress list with a Stop button. Stops at the first problem and explains why; the sim is still paused if requested.
- **Pause** straight after positioning; **press LS** on both EFIS panels.
- **Go again** repeats the scenario without reprogramming the MCDU. **Favourites** save airport, approach and start settings.
- Navigation data is indexed in the background at start-up and cached on disk; approaches load as soon as an airport is selected.

### MCDU page
- Live mirror of MCDU 1 or 2 in Airbus colours, with small/large fonts and ToLiss symbols.
- Keyboard laid out like the real unit (as on MOZA/WinWing hardware), including BRT/DIM; computer keyboard works too.

### xPilot & ATC page
- xPilot connection status, callsign, SELCAL (with alert and reset) and network aircraft count.
- COM1/COM2 active and standby frequencies, the station tuned on each and receive indicators.
- **Nearby ATC** from VATSIM data, nearest first, with a radio-range check based on your altitude; tap for controller info or
  ATIS, and put a frequency in COM1 or COM2 standby.
- Buttons for xPilot's notifications, aircraft labels, TCAS control, default X-Plane ATIS and split audio (with tooltips).

### Repositioning
- Rewritten to use X-Plane's recommended method: correct position, ground height and orientation (no more floating,
  wrong heading or upside-down aircraft). No false stall warning after a ground move.
- Stands line up by aircraft type: the nose wheel is placed on the stop point using the loaded aircraft's own gear geometry.
- Moves to another airport wait for its scenery to load; in-air moves keep the aircraft's MCDU, IRS and engine state.
- Works correctly when the sim is paused.
- New map with base map, full airport detail and tappable stands and runways.
- Much faster airport search and stand loading, using a saved airport index and custom-scenery priority from `scenery_packs.ini`.

### Other
- Licensed under CC BY-NC 4.0 (free for personal, non-commercial use; commercial use needs a separate license).
  The license is shown in Settings → About and included as `LICENSE`.
- Removed leftover code from the old Final approach and Localiser intercept tabs and from earlier designs.
- Plugin no longer lets browsers cache the EFB page.
- Removed the MCDU text-entry box, the xPilot headset/speaker, text console and nearby-ATC-window buttons, and the
  xPilot help text in Settings.

## 0.57
- In-air repositioning keeps the aircraft state; holds position while distant scenery loads.
- Localiser intercept mode (later merged into the Approach tab).
- Pause option after in-air repositioning.

## 0.56
- **Dispatch & OFP:** Takeoff and Landing performance sheets from SimBrief's runway analysis (TLR), laid out like a printed
  performance sheet, with a runway picker and landing margins.
- **Map & Telemetry:** ground map from X-Plane scenery (pavement, taxi lines, hold-short lines, taxiway signs, stands,
  runways, airport boundary, buildings drawn above the pavement), heading-up and tall-map modes, freehand drawing saved per airport.
- Compact telemetry strip (GS, IAS/Mach, altitude, V/S, heading, wind, OAT/QNH, N1, AP/A/THR, COM1, fuel, weight, park brake,
  radio altitude, flaps, speedbrake, gear, transponder, fuel flow, TAT, nearest taxiway or stand, beacon/strobe/seatbelts).
- Aircraft icon uses true heading.
- Sidebar grouped into Preflight, Flight, Sim control and Network.
- VATSIM Radar added as an external link.
