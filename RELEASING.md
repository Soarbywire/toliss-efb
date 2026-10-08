# Releasing a new ToLiss EFB version

Releases are published by a GitHub Actions workflow in this repository. The EFB's built-in updater
(Settings → Updates) checks the latest release of `soarbywire/toliss-efb` and installs it for users.

## One-time setup

1. Upload the contents of this folder to the `toliss-efb` repository (see "Uploading files" below), including
   `.github/workflows/release.yml`, `tools/`, `LICENSE`, `README.md`, `CHANGELOG.md`, `PI_ToLissWebTablet.py`
   and the whole `ToLissWebTablet` folder (the plugin package and `index.html`).
2. In the repository, open **Settings → Actions → General** and make sure **Allow all actions** is selected
   (it is by default). Under **Workflow permissions**, "Read and write" is not needed: the workflow asks for the
   permission it uses.

## Each release (e.g. 0.68)

1. Set the version in three places:
   - `ToLissWebTablet/__init__.py`: `EFB_VERSION = "0.68"`
   - `PI_ToLissWebTablet.py`: `STUB_VERSION = "0.68"`
   - `ToLissWebTablet/index.html`: `Version: 0.68 Release Beta`
2. Add a `## 0.68` section at the top of `CHANGELOG.md`. Its text becomes the release notes users see.
3. Upload the changed files to the repository (they replace the old ones).
4. Open the **Actions** tab → **Publish release** → **Run workflow**, type `0.68`, and press **Run workflow**.

After about a minute the release `v0.68` appears under **Releases**, with the zip and its `.sha256` checksum
attached. The workflow stops without publishing if the version numbers in the three places don't match what you
typed, if a Python file has an error, if a package file is missing, if `CHANGELOG.md` has no section for it, or if that version was already published.

Optionally post the same zip on the X-Plane.org forum.

## Uploading files through the GitHub website

- **Changed files**: on the repository page, **Add file → Upload files**, drag the files in, and press **Commit changes**.
  Files inside folders (e.g. `ToLissWebTablet/features/pushback.py`) go in by opening that folder first, then uploading,
  or by dragging the whole `ToLissWebTablet` folder in (GitHub keeps the folder structure).
- **The workflow file** (first time only): folders starting with a dot can be hidden on your computer, so the
  simplest way is **Add file → Create new file**, type the name `.github/workflows/release.yml`, paste the file's
  contents, and **Commit changes**.

## Good to know

- From 0.67 the updater replaces `PI_ToLissWebTablet.py` and the whole `ToLissWebTablet` package. Users' settings,
  learned stands, checklists, Ops Centre log, recordings and caches are carried over, and **Undo last update** restores
  the previous version (a full copy is kept in `PythonPlugins/.tolissefb-backups`).
- Users updating from 0.66 or earlier: their old updater only copies `PI_ToLissWebTablet.py` and `index.html`. The new
  `PI_ToLissWebTablet.py` notices the package is missing, downloads the release whose version is its `STUB_VERSION`,
  checks the checksum and adds the package files; the user then chooses Reload scripts once more. So the release
  must be published (with its zip and `.sha256`) under the tag that matches `STUB_VERSION`.
- Updates are refused while the flight data recorder is recording.
- The updater refuses a download that doesn't match its checksum, and only installs while the aircraft is parked.
- Users on versions before 0.59 must install 0.59 by hand once; after that, updates are one tap.
- `tools/make_release.py` can also be run on your PC to build a release zip manually.
