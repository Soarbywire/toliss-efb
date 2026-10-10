# ToLiss EFB – Changelog

## Installation

1. Copy `PI_ToLissWebTablet.py` to `X-Plane 12/Resources/plugins/PythonPlugins/`.
2. Copy the `ToLissWebTablet` folder to the same `PythonPlugins` folder (replace the files; your settings and data in it are kept).
3. Restart X-Plane, or use XPPython3's "Reload scripts".
4. Check **Settings → About** shows the new version. The page is no longer cached by browsers, so a normal reload picks up updates.

On first start the plugin builds two indexes in the background and saves them in the plugin folder:
`apt_index_cache.json` (airports) and `navdata_cache.pkl` (waypoints and navaids). Later starts load them in well under a second.
They rebuild automatically when your scenery or navigation data (e.g. a Navigraph AIRAC update) changes, and can be deleted safely.

**Recommended ToLiss ISCS setting:** enable **"X-Plane pause acts as freeze motion"**. The approach trainer pauses the sim
as soon as the aircraft is positioned and finishes setting it up while paused, which needs the aircraft systems to keep running.

---

## 0.67.4

### Airport frequencies (new)
- **xPilot & ATC → Airport frequencies:** the real-world radio stations of an airport (ATIS, Delivery, Ground, Tower,
  Departure, Approach, CTAF), read from your scenery (custom scenery first, as on the map), with tabs for the
  **current / nearest** airport, your **departure** and your **destination**. COM1 / COM2 buttons set the standby
  frequency, and a green dot shows when a VATSIM controller is online on that frequency.
- **Map & Telemetry → Frequencies** opens the same list for the airport you are at.

### QNH in the airport's own unit
- QNH is now shown the way it is used at each airport: inches of mercury where the METAR gives `A2992` (US, Canada,
  Japan and others), hectopascals where it gives `Q1013`; without a METAR, by country.
- Applies to the Ops Centre **takeoff data** (departure airport) and **landing data** (destination airport, now with a
  QNH line), the **Takeoff / Landing performance sheets** (the airport's unit first, the other in small print), and the
  **telemetry strip** (departure airport until airborne, then the destination; its label shows the unit).

### Secure connections
- If the computer's own security certificates reject a server (common with an out-of-date certificate store in the
  Python inside XPPython3, e.g. on a Mac), the EFB retries with an up-to-date set (certifi if installed, else a copy of
  Mozilla's certificates included with the EFB). This applies to Hoppie, SimBrief, VATSIM, METARs and updates.
- If it still fails, the message now explains what to check (the computer's date and time, its certificates, or
  antivirus HTTPS scanning) instead of the raw "CERTIFICATE_VERIFY_FAILED" error.

---

## 0.67.3

### Ops Centre
- **Load my latest SimBrief flight plan automatically** (a tickbox in the Ops Centre setup and in Settings, off by
  default). When ticked, the plugin loads your latest SimBrief plan when X-Plane starts (or after Reload scripts),
  retrying for a while if SimBrief can't be reached, and the EFB shows it on Dispatch & OFP when it opens. A plan you
  regenerate mid-session is still loaded with Fetch Latest OFP.
- **No repeated messages after a reload:** the dispatched flight's progress is saved, so loading the same plan again
  (automatically or by hand) carries on where it left off.
- **The Flight box folds:** tap its header to fold it to one line (the current phase, how many messages are sent and
  what comes next); the choice is remembered on each device.
- **The list of messages is always shown:** with no flight plan loaded, the phases and their messages appear greyed
  out, with a note on how to start, instead of disappearing.

---

## 0.67.2

### Ops Centre: DCDU (new)
Ops Centre messages on an A320-family DCDU (as on the A320, A321neo and A321XLR), in the same bezel and key colours
as the EFB's MCDU and FCU.
- **Where:** Ops Centre → **Message display: List / DCDU**, or the **DCDU** button on the MCDU page to show it next to
  the MCDU.
- **The screen:** time and sender, the message status (OPEN, SENDING, ROGER SENT, CLOSED...), the text in cyan while
  open and green once answered, long messages in pages, and the MSG count. Your own messages to the ops centre show as SENT.
- **Response keys beside the screen**, to suit each message: the final load sheet ACCEPT / REJECT (a rejection brings a
  revised load sheet), the stand assignment ROGER / REQ CHANGE (the ops centre assigns another stand), questions ROGER /
  STBY / UNABLE, and everything else ROGER. Answered messages can be CLOSED; RECALL brings closed ones back.
- **Keys:** MSG−/MSG+, PGE−/PGE+, PRINT (onto a cockpit-printer strip under the unit) and BRT/DIM.
- A **MSG** light and a chime for a new message. Responses go to the ops centre over Hoppie as the crew and are kept in
  the Ops Centre log, so every device shows the same state. ToLiss's own DCDU and CPDLC are not used.

---

## 0.67.1

### Fixes
- **Dispatch & OFP:** the box labelled "Callsign" showed the flight number. It now shows both: **Flight** (e.g. QF431)
  and **Callsign**, the ATC callsign from your SimBrief OFP (e.g. QFA43T).
- **Runway distances in feet or metres:** a new **Runway distances: FT / M** setting, on the Takeoff and Landing
  performance sheets and in the Ops Centre setup (one setting, shared by both and every device). It applies to the
  runway lengths, the actual and factored landing distances and the margin on the sheets, and to the Ops Centre's
  **landing data** message (LDA and required distances). Elevations stay in feet. The default is feet.

---

## 0.67

### A modular plugin
The plugin is now a package: `PI_ToLissWebTablet.py` is a small loader and the EFB itself lives in the
`ToLissWebTablet` folder, split into sections (core, features, services, sim and web) so each part can be worked on
separately. Everything goes through one main-thread bridge: requests from the page never touch X-Plane directly.

- **Updating from 0.66 or earlier:** the old updater only copies two files, so after **Install** and **Reload
  scripts** the EFB finishes the update itself: it downloads 0.67 from GitHub, checks the checksum and adds the new
  files (settings, learned stands, checklists, recordings and caches are kept). The EFB's address shows the progress;
  when it says so, choose **Reload scripts** once more. If it can't download, it explains how to copy the files by hand.
- **Updates now replace the whole package** and keep your data (including learned stands, `stand_rules.json`).
  A full copy of the previous version is kept in `PythonPlugins/.tolissefb-backups` for **Undo last update**.
  Updates wait until the flight recorder has stopped.
- **Reload scripts** now always runs the newly installed code (XPPython3 otherwise keeps old modules loaded).

### Pushback: towing model
- The push follows a **towing model**: the tug steers the nose wheel and the aircraft pivots about its main gear
  (turning radius = wheelbase ÷ tan(steering angle)), steering in and out smoothly. The wheelbase comes from the loaded
  aircraft's gear positions, with type figures as a fallback.
- **Nose wheel steering:** Gentle (45°), Normal (60°) or Tight (80°), within Airbus's 90° towing limit. Small turns
  automatically use less steering.
- **The map shows the plugin's own plan**, so the preview is exactly the path the push will take: a dashed line for
  the **path of the aircraft's nose**, from the nose now to the nose of the outline where the aircraft ends up. The
  line is drawn beneath both aircraft shapes, so it never crosses them (on a short straight push it is hidden
  entirely and the outline alone shows the result).
- The aircraft outlines are drawn to the loaded type's length and span (an A321 is longer than an A320).
- **Settings are kept by the plugin** (in `config.json`), so every device and browser gets the same pushback
  settings and they survive restarts and updates. Push back / pull forward is now remembered too.
- **Reset settings to defaults** button (set apart below the push controls): 20 m straight back, tail left 90°, 0 m final, 3 kt, Normal steering, push back.

### IOS: Replay (new)
X-Plane's own replay, on the EFB: Replay mode on/off, Start, Fast reverse, Reverse, Slow reverse, Pause, Slow forward,
Play, Fast forward and End, plus **Exit to Real-Time** (leaves replay and returns to the live flight). A playback
button enters replay when needed. A **REPLAY** badge shows at the top of every page while X-Plane is in replay. Any
command your X-Plane version lacks is greyed out. Replay is not available during a pushback or slew.

### IOS: TCAS Traffic (new)
Inject intruders into TCAS to practise traffic and resolution advisories (X-Plane 12.4.1 or later).
- **Scenarios:** head-on, crossing from the left or right, overtaking from behind, climbing from below, descending
  from above. Set the time to the closest point of approach, the horizontal miss distance, the intruder's height
  above or below you at that point, and optionally its speed. Up to 8 intruders at once for multi-threat encounters.
- Each intruder flies a straight path planned from your present track, speed and climb or descent, reports its
  altitude (Mode C) with its own Mode S address, and is removed a minute after it passes.
- The page lists each intruder's range, relative altitude, clock position, time to closest approach and TA/RA status.
- Only one plugin can supply traffic: the EFB won't take it from xPilot or LiveTraffic (it says who has it), and
  gives it back at once if another plugin asks (e.g. xPilot connecting). Intruders show on TCAS and the ND, not out of
  the window. TCAS gives no RAs on the ground or below about 1,000 ft above the ground; the page warns.

---

## 0.66

### IOS (Instructor Operating Station): new
The Sim Control group is now the IOS, named and ordered like a full flight simulator's instructor station:
Aircraft State, Slew, Reposition, Weather & Time, Situations and Malfunctions (the last two need ToLiss Pro).

- **Aircraft State:** live altitude, height above ground, IAS/TAS/GS, heading, V/S, pitch, bank and fuel. In flight,
  set altitude, airspeed (indicated, converted to true airspeed for the altitude, plus the wind), heading, vertical
  speed, pitch and bank with immediate effect: step buttons apply at once; typed values apply with Set, or together
  with "Set entered values". Works while paused (the EFB briefly unpauses, applies, pauses again and checks). On the
  ground a notice explains that these are in-flight changes.
- **Freezes:** Freeze (pauses X-Plane; with "X-Plane pause acts as freeze motion" on in the ToLiss ISCS, systems keep
  running), altitude freeze and fuel freeze. **Sim rate** x1/x2/x4/x8.
- **Slew:** move the aircraft freely with a hold-to-move pad or the keyboard (forward/back, sideways, turn,
  climb/descend) at a crawl to very fast; tap the map to place it. On the ground it follows the terrain; in the air it
  keeps its altitude (never below 50 ft). Ending slew hands back control at a chosen airspeed (or stopped on the
  ground), and pauses again if it was paused.
- **Reposition: quick positions:** lined up, 5 or 10 nm final, left/right downwind, overhead. The nearest airport is
  filled in automatically (and with a Nearest button).
- **Approach trainer:** optional "Clear the flight plan first" (erases a temporary plan, then NEW DEST at the first
  waypoint) before programming the MCDU.
- **Quick weather (Weather & Time):** wind relative to the aircraft's heading (headwind, crosswinds, gusts, tailwind,
  calm), turbulence (range read from your X-Plane's DataRefs.txt, applied up to the aircraft's altitude), visibility
  and cloud (low visibility, CAT I, CAT II, CAT IIIA), and Clear sky (X-Plane's Clear/CAVOK). Applied immediately
  (also while paused) and read back, so the message says what X-Plane took.
- **Armed wind shear:** hits as you descend through about 1,000 ft (a headwind gust, then a 25 kt tailwind with
  turbulence), then the previous wind returns. The button shows orange when armed and red while it hits.
- **Quick malfunctions (ToLiss Pro):** engine failure at V1 (from your SimBrief takeoff data), engine fire, engine
  failure after takeoff or now, green hydraulic loss, generator, bleed and decompression, each found in ToLiss's own
  failure list and armed with the right trigger.

### Built-in pushback: new (replaces Better Pushback)
- Plan the push with sliders or typed values: straight back, a turn with the tail left or right (0-180°), a final
  straight and the towing speed; presets for the common pushes; or pull forward.
- A map of the airport shows the plan before you start: a line from the tail to the nose at the end, and the
  aircraft at true size where it will end up. Map or satellite view, with the airport layout on top (or hidden).
- Works like a real tug: it starts when you release the parking brake and stops if you set it; Pause/Resume and Stop.
  The aircraft accelerates gently, follows the path and stops smoothly, keeping its exact height above the ground.
- After the push (or Stop) the aircraft is held in place until you set the parking brake, so idle thrust can't move
  it. Available on the ground only. Counts as OOOI OUT for the Ops Centre.

### Ops Centre
- **Stand assignment:** stands named for freight, maintenance, general aviation and the like are never assigned;
  the stand you park on is learned for your airline; **Change stand** assigns another automatically or one you choose
  (with a map preview), can exclude an unsuitable stand and save a choice as a rule; the assigned stand is marked on
  the moving map. Rules are kept in stand_rules.json.
- **Block-in summary** is sent when the parking brake is set on stand (engines may still run), and messages are held
  while the aircraft's ATSU is offline and sent when it is back.

### Displays and maps
- **FCU display** (Cockpit Displays): a display-only FCU with both EFIS baro windows, in Airbus colours.
- **MCDU** laid out and coloured like the real unit: blue-grey bezel, function keys with BRT/DIM, AIRPORT and arrow
  keys, round number keys, letter keys, and the annunciator lights in place (shown unlit).
- **Satellite view** on the Pushback, Reposition and Map & Telemetry maps (Esri World Imagery), with the airport
  layout's lines on top and a Layout switch.
- The SD is removed from Cockpit Displays for now.

### Also
- **Settings:** a "support" link to Ko-fi.
- Versions now continue as 0.66, 0.66.1 and so on.

## 0.65

- **Change stand (Ops Centre):** the Stand assignment row has a **Change stand** button (**Assign stand** before one is
  set), which opens a window with two choices:
  - **Assign another automatically:** a different suitable stand, by the usual preference; among equally good stands
    the pick is random, so trying again gives another.
  - **Choose a stand:** the destination's stands from your scenery (gate or remote, size code, airlines, how often
    your airline has used each, your rules), with search and **Show all stands** to also list stands not normally
    offered, with the reason (e.g. "name suggests freight").
  - Options: **Don't assign the current stand again at this airport**, and (when choosing) **Use this stand for this
    airline's domestic/international flights here**, saved as a rule and assigned first next time.
  - Before the arrival package the stand is sent with it; afterwards a **revised stand assignment** is sent; after
    parking nothing is sent, but exclusions and rules are still saved.
- **Map buttons** in the stand list open the moving map centred on that stand, with the airport layout.
- **The assigned stand is marked on the moving map** (STAND 14) until the aircraft is parked.

Updating from 0.62 or earlier? Version 0.64 added the Ops Centre (Hoppie ACARS dispatch) and fixed a crash when
starting the Flight Recorder on macOS: see the full changelog at https://github.com/soarbywire/toliss-efb/blob/main/CHANGELOG.md

## 0.64

### Ops Centre (Hoppie ACARS): new
A new Network page that turns the EFB into your airline's operations centre on the free Hoppie ACARS network. It
dispatches your flight to the ToLiss MCDU (ATSU, AOC, RECEIVED MESSAGES) from pre-flight to block-in. Every message is
formatted for the MCDU's 24-character lines, and automatic messages are spaced a few seconds apart.

- **Setup:** Hoppie logon code (stored only on your PC, masked with Show/Hide), an ops callsign of your choice (e.g.
  QFAOPS, up to 8 letters or digits, different from the aircraft) and the aircraft callsign, which can be taken from
  SimBrief. Saved as each field is changed and filled in automatically. Can be switched off in Settings (Network).
- **Messaging:** a message log with UTC times and delivery status, quick messages, an unread badge in the sidebar,
  and Clear messages. Messages are checked every 45 to 75 seconds, as Hoppie asks (Check now: once every 20 s).
- **Send as crew:** ToLiss's ATSU has no free-text page for company messages, so crew replies are sent from the EFB
  under the aircraft's callsign to the ops centre, as an MCDU message would be. The EFB only ever collects the ops
  centre's messages, never the aircraft's, so it doesn't interfere with ToLiss.
- **Messages wait for the aircraft:** while Hoppie shows the aircraft's ATSU offline (e.g. powered down), messages
  are held, shown as "Waiting for the aircraft (ATSU offline)", and sent when the ATSU is back online.
- **Flight panel:** headed by the flight (callsign, route, aircraft, OFP time), with a timeline strip (OUT, OFF, ON,
  IN, ETA, distance to go, EFOB, stand) and three phase sections. The current phase is open and marked; finished
  phases fold into a summary line. Each item has an Auto switch and Send now; "New flight / resend all" starts again.

**Pre-flight**
- **When the SimBrief plan is loaded:** flight release (route, level, cost index, alternate, times, fuel breakdown,
  estimated weights), weather for departure, destination and alternate (live METAR and TAF, or the plan's if
  unavailable), NOTAMs from the OFP, and the preliminary load sheet.
- **When the beacon comes on:** the final load sheet from the actual load in the sim (passengers, cargo per hold, ZFW,
  take-off fuel, TOW, trip fuel, LAW, limits and underload), asking for acknowledgement, and takeoff data from
  SimBrief's runway analysis (CONF, FLEX or TOGA, V1/VR/V2). The takeoff data adds CAUTION lines and "RECALCULATE
  BEFORE DEPARTURE" if the actual TOW is above SimBrief's, the ATIS runway is not the planned one, or it is more than
  3°C warmer than planned.
- **Departure ATIS** from VATSIM, and again on each new letter until takeoff.

**In flight**
- **OOOI times** (out, off, on, in) detected from the sim; IN is the parking brake set on stand, as real ACARS reports it.
- **Departure report** after takeoff: out and off times, ETA, and on time / early / late against the schedule.
- **ETA updates** from the distance remaining along the SimBrief route at the current ground speed, sent when the ETA
  moves by 10 minutes or more.
- **Fuel check:** estimated fuel at destination against SimBrief's planned landing fuel; a message at 500 kg below
  plan, and a stronger one if it would dip into alternate and final reserve.
- **Destination weather** in cruise on each new METAR, with a caution if visibility or cloud base look low.

**Arrival and after landing** (about 150 nm or 30 minutes out)
- **Arrival ATIS** from VATSIM, and again on each new letter until landing.
- **Stand assignment** from the destination's scenery, for the aircraft's ICAO size code (C for the A319/A320/A321,
  E for the A330/A340). Stands marked or named as cargo, freight, maintenance, hangar, general aviation, de-icing and
  similar are never assigned. Preference: the airline's rule, stands where it has parked before (learned
  automatically when you park), stands the scenery lists for it, unrestricted stands, and other airlines' stands only
  as a last resort; gates before remote stands. **Reassign** (beside the assigned stand) excludes an unsuitable stand
  at that airport for good and, in flight, sends a revised assignment. Exclusions, learned stands and optional airline
  rules (domestic and international) are kept in stand_rules.json in the ToLissWebTablet folder, e.g.
  "YSSY": {"airlines": {"QFA": {"domestic": ["1-16"], "international": ["50-63"]}}}.
- **Landing data** from SimBrief's runway analysis (LDA, wind, landing weight, CONF, VREF, autobrake and required
  distances dry and wet), with cautions if the ATIS runway differs from the planned one, the estimated landing weight
  is above SimBrief's, or the wet distance exceeds the LDA.
- **Block-in summary** when the parking brake is set on stand: OOOI times, block and flight time, arrival against
  schedule, fuel used against plan, touchdown rate (sampled 5 times a second below 300 ft) and the stand used.

**Without a VATSIM ATIS:** nothing waits for it. The ATIS rows show "No VATSIM ATIS online (checking)", and a one-off
"D-ATIS NOT AVBL" message with the latest METAR is sent instead (departure and destination). If an ATIS comes online
later, it is sent as usual. Optional (off by default): an expected runway into wind, marked as an estimate.

**Crew requests understood** (Send as crew): LOADSHEET RECEIVED, REQUEST WX [airports], REQUEST ATIS [airport],
REQUEST LOADSHEET, REQUEST TO DATA, REQUEST ETA (or PROGRESS), REQUEST STAND, REQUEST LANDING DATA.

### Fixes
- **Crash to desktop when pressing Start on the Flight Recorder (notably on macOS).** Starting a recording read
  X-Plane values from the web server's thread, which X-Plane does not allow. Start and Stop are now carried out on
  X-Plane's main thread by the flight loop; the page still shows the recording at once.
- **Saved settings were ignored after restarting X-Plane** for the Flight Recorder (mode, rate), automatic update
  checks and the Ops Centre, because they were set up before the settings file was read.

## 0.62

- **Approach trainer, how each start is flown:**
  - **Intercept:** flown as on vectors: selected HDG on the intercept heading, ALT held below the glide path, SPD
    selected, AP1 on and APPR armed. LOC (or APP NAV for RNAV approaches) captures the final course; no DIR TO.
  - **On final:** DIR TO the next fix ahead with RADIAL IN set to the final course (radial = course + 180°), so the
    F-PLN follows the centreline, then NAV.
  - **Full procedure:** DIR TO the first fix, then NAV.
  - The approach is now inserted after positioning for every start, so the FMS doesn't treat fixes the aircraft was
    moved past as already flown; a discontinuity before the approach's next fix is cleared.
- **Approach phase activated automatically** after positioning (the option is now ticked by default, and is
  switched on once for everyone with this version, even if it had been saved as off). The EFB recognises ToLiss's
  ACTIVATE / CONFIRM APPR PHASE prompt at LSK 6L, which is split over two lines on the PERF page. This puts the
  FMS in the right flight phase, so it tunes the destination ILS itself and shows managed approach speeds and V/DEV.
  The EFB then checks RAD NAV and tunes the ILS by hand only if the FMS hasn't (with a warning). The ILS is found
  from the runway's localiser ident, the approach's legs, or the runway it serves in X-Plane's navaid list.
- **AP1 engages reliably:** the ALT knob is pushed (managed vertical mode) before engaging AP1, and ToLiss's AP1
  button command is used as a fallback.
- **Guidance problems no longer stop the sequence** once the aircraft is positioned: if FD, A/THR, AP1, NAV or
  APPR does not engage, it is shown as a warning with what to do, and the rest of the setup still completes.

## 0.61

- **Fix: "SELECT HDG/TRK FIRST" stopped the approach trainer in flight.** The FMS refuses changes to the active leg
  while the autopilot is in NAV. Before changing the MCDU in the air, the EFB now selects HDG holding the current
  heading; if the message still appears, it clears it, selects HDG and retries instead of stopping.
- **FCU after every reposition:** QNH from the destination METAR on both sides (switching out of STD if needed), the
  speed shown on the Approach page selected, and the FCU altitude set to the altitude shown on the Approach page.

## 0.60

- **Engines (approach trainer):** the automatic engine start option is removed; starting the engines is up to the
  pilot, which works with any cockpit including hardware ENG MASTER switches. Before positioning in the air, the EFB
  checks that both engines are running and otherwise stops with a warning, without moving the aircraft.
- **Fix:** the engine check always reported "neither engine is running", because the plugin could not read X-Plane
  values that are lists of whole numbers (such as which engines are running). Engines are also counted as running
  when their N2 is above 50%.
- **Fix:** values an aircraft doesn't provide are now reported as unavailable instead of 0.
- **Update check:** if GitHub's release service doesn't answer normally (the "JSONDecodeError" message), the EFB now
  finds the latest release through its web address instead. Errors say what GitHub actually sent and are written to
  X-Plane's Log.txt.

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
- The EFB opens on **Map & Telemetry**, which is now always available (its on/off switch is removed). A new
  **Start page** setting can instead return you to the last page used.
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
