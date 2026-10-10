# ToLiss EFB

A web-based electronic flight bag for the ToLiss Airbus family in X-Plane 12. It runs as an XPPython3 plugin and
opens in any browser on your network, including an iPad:

- **Preflight:** SimBrief dispatch with takeoff and landing performance, weight & balance, ground services, and a
  built-in pushback (a towing model planned on a map, works with the parking brake like a real tug).
- **Flight:** moving map with airport ground detail and satellite view, ToLiss checklists, an MCDU laid out like the
  real unit, live PFD / E/WD / FCU, and a flight data recorder with landing reports.
- **IOS (Instructor Operating Station):** aircraft state (set speed, altitude, heading and attitude in flight),
  freezes, slew, repositioning with quick positions and an approach trainer, quick weather (wind, turbulence,
  visibility, armed wind shear), TCAS traffic injection for TA/RA practice, X-Plane replay controls, and ToLiss
  situations and malfunctions.
- **Network:** VATSIM radar, xPilot and nearby ATC, real-world airport frequencies with one-tap tuning, and an **Ops Centre** that acts as your airline's operations
  centre on the Hoppie ACARS network, dispatching the flight to the ToLiss MCDU (release, weather, NOTAMs, load sheets,
  takeoff data, ATIS, ETA and fuel updates, stand assignment, landing data and a block-in summary).

## Installation

1. Install [XPPython3](https://xppython3.readthedocs.io/) for X-Plane 12.
2. Download the latest `ToLiss_EFB_x_xx.zip` from [Releases](https://github.com/Soarbywire/toliss-efb/releases).
3. Copy `PI_ToLissWebTablet.py` to `X-Plane 12/Resources/plugins/PythonPlugins/`
   and the `ToLissWebTablet` folder to the same `PythonPlugins` folder.
4. Start X-Plane, load a ToLiss aircraft and open the address shown by the plugin in your browser.

From version 0.59, updates can be installed from **Settings → Updates** inside the EFB.

Recommended ToLiss ISCS setting: **"X-Plane pause acts as freeze motion"**.

## License

Free for personal, non-commercial use under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/);
commercial use requires a separate license ([contact](https://soarbywire.com/contact/)). See [LICENSE](LICENSE).
