# ToLiss EFB

A web-based electronic flight bag for the ToLiss Airbus family in X-Plane 12. It runs as an XPPython3 plugin and
opens in any browser on your network, including an iPad:

- **Preflight:** SimBrief dispatch with takeoff and landing performance, weight & balance with the live CG, ground
  services, and a built-in pushback: planned on a map with a towing model, done by X-Plane's own towbar tug, which
  drives up and connects (with NWS DISC on the E/WD) and works with the parking brake like a real tug, with X-Plane's
  physics keeping the wheels on the ground on any apron.
- **Weather strip:** on every page, the departure or destination weather in one line (ATIS letter, runway, wind
  components, QNH) with alerts only when something matters: from the METAR with real weather, from X-Plane's own
  weather when it is custom, and the ATIS from the source you choose (BeyondATC, VATSIM or the EFB's own ATIS). The
  full weather, ATIS and TAF on a tap.
- **Flight:** moving map with airport ground detail, satellite view and taxi routing to the stand or runway, ToLiss checklists, an MCDU laid out like the
  real unit, live PFD / E/WD / FCU, and a flight data recorder with landing reports.
- **IOS (Instructor Operating Station):** aircraft state (set speed, altitude, heading and attitude in flight),
  freezes, slew, repositioning with quick positions and an approach trainer, camera views (with your Quick Looks,
  a jump to the landing and a touchdown camera in replay), quick weather (wind, turbulence,
  visibility, armed wind shear), TCAS traffic injection for TA/RA practice, X-Plane replay controls, and ToLiss
  situations and malfunctions.
- **BeyondATC (optional):** the ATC transcript read live from BeyondATC's log, your current clearance (squawk,
  altitude, heading, speed, next frequency, approach), taxi clearances planned on the map automatically, your radios
  and the frequencies of the airports and centres BeyondATC is running (tap to tune), its ATIS and the runway it
  assigned you in the weather strip, and ATC's last instruction under the top bar.
- **Network:** VATSIM radar with live pilot details (flight levels, climb and descent), xPilot and nearby ATC, real-world airport frequencies with one-tap tuning, and an **Ops Centre** that acts as your airline's operations
  centre on the Hoppie ACARS network, dispatching the flight to the ToLiss MCDU (release, weather, NOTAMs, load sheets,
  takeoff data, ATIS, ETA and fuel updates, stand assignment, landing data and a block-in summary), shown as a
  message list or on an A320-family DCDU with response keys. Load sheets carry the CG (MACZFW, MACTOW, MACLAW),
  with LMC messages and revised load sheets for last-minute changes. Message times in sim time or real-world UTC.
- **Manual:** a full user manual is built in (📖 Manual in the sidebar).

## What's new in 0.70

The changes of 0.69.1 to 0.69.8 in one update (details in [CHANGELOG.md](CHANGELOG.md)):

- **Pushback with X-Plane's physics**, like Better Pushback: the gear keeps the wheels on the surface on sloping or
  uneven aprons and add-on scenery (fixes the aircraft sinking into the ground after a push, reported at LFML).
- **Weather strip** under the top bar on every page: ATIS letter, runway in use, wind components, QNH, conditions and
  alerts; the METAR with real weather, X-Plane's own weather when it is custom, and an **EFB ATIS** offline.
- **ATIS source** to choose: Automatic, BeyondATC, VATSIM or the EFB's own, for the strip, the Ops Centre's MCDU
  messages and the takeoff and landing data cautions.
- **BeyondATC page**: live transcript, your clearance, **radios and frequencies** (airports and centres BeyondATC is
  running, tap to tune), its ATIS, the runway it assigned you (in the weather strip too), ATC's last instruction under
  the top bar, and **taxi clearances planned on the map automatically**.
- **Taxi routing** matches taxiway names however the scenery labels them (C, C_1, TWY C, Charlie), and a name missing
  from the scenery no longer blocks the route.
- **Ops Centre**: OOOI and other message times in **sim time** (default) or real-world UTC.
- **Camera views** on the Replay and Slew pages, with your Quick Looks, a jump to the landing, a touchdown camera and
  auto-cycle in replay.
- **Fix**: no line across the map after a reposition.

## Installation

1. Install [XPPython3](https://xppython3.readthedocs.io/) for X-Plane 12.
2. Download the latest `ToLiss_EFB_x_xx.zip` from [Releases](https://github.com/Soarbywire/toliss-efb/releases).
3. Copy `PI_ToLissWebTablet.py` to `X-Plane 12/Resources/plugins/PythonPlugins/`
   and the `ToLissWebTablet` folder to the same `PythonPlugins` folder.
4. Start X-Plane, load a ToLiss aircraft and open the address shown by the plugin in your browser.

From version 0.59, updates can be installed from **Settings → Updates** inside the EFB. After installing an update
(or copying a new version in by hand), finish it in this order:

1. In X-Plane: **Plugins → XPPython3 → Reload scripts** (or restart X-Plane).
2. Reload the EFB page in your browser.

From 0.69 the EFB reminds you of both steps, and shows a banner if the page and the plugin are different versions.

Recommended ToLiss ISCS setting: **"X-Plane pause acts as freeze motion"**.

## License

Free for personal, non-commercial use under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/);
commercial use requires a separate license ([contact](https://soarbywire.com/contact/)). See [LICENSE](LICENSE).
