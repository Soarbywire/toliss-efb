# ToLiss EFB

A web-based electronic flight bag for the ToLiss Airbus family in X-Plane 12. It runs as an XPPython3 plugin and
opens in any browser on your network, including an iPad:

- **Preflight:** SimBrief dispatch with takeoff and landing performance, weight & balance with the live CG, ground
  services, and a built-in pushback: planned on a map with a towing model, done by X-Plane's own towbar tug, which
  drives up and connects (with NWS DISC on the E/WD) and works with the parking brake like a real tug.
- **Weather strip:** on every page, the departure or destination weather in one line (ATIS letter, runway, wind
  components, QNH) with alerts only when something matters: from the METAR with real weather, from X-Plane's own
  weather when it is custom, with an EFB ATIS when there is no VATSIM ATIS. The full weather, ATIS and TAF on a tap.
- **Flight:** moving map with airport ground detail, satellite view and taxi routing to the stand or runway, ToLiss checklists, an MCDU laid out like the
  real unit, live PFD / E/WD / FCU, and a flight data recorder with landing reports.
- **IOS (Instructor Operating Station):** aircraft state (set speed, altitude, heading and attitude in flight),
  freezes, slew, repositioning with quick positions and an approach trainer, camera views (with your Quick Looks,
  a jump to the landing and a touchdown camera in replay), quick weather (wind, turbulence,
  visibility, armed wind shear), TCAS traffic injection for TA/RA practice, X-Plane replay controls, and ToLiss
  situations and malfunctions.
- **BeyondATC (optional):** the ATC transcript read live from BeyondATC's log, your current clearance (squawk,
  altitude, heading, speed, next frequency, approach, taxi route onto the map), its ATIS in the weather strip, and
  ATC's last instruction under the top bar.
- **Network:** VATSIM radar with live pilot details (flight levels, climb and descent), xPilot and nearby ATC, real-world airport frequencies with one-tap tuning, and an **Ops Centre** that acts as your airline's operations
  centre on the Hoppie ACARS network, dispatching the flight to the ToLiss MCDU (release, weather, NOTAMs, load sheets,
  takeoff data, ATIS, ETA and fuel updates, stand assignment, landing data and a block-in summary), shown as a
  message list or on an A320-family DCDU with response keys. Load sheets carry the CG (MACZFW, MACTOW, MACLAW),
  with LMC messages and revised load sheets for last-minute changes.
- **Manual:** a full user manual is built in (📖 Manual in the sidebar).

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
