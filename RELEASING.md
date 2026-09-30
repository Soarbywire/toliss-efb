# Releasing a new ToLiss EFB version

Releases are published by a GitHub Actions workflow in this repository. The EFB's built-in updater
(Settings → Updates) checks the latest release of `soarbywire/toliss-efb` and installs it for users.

## One-time setup

1. Upload the contents of this folder to the `toliss-efb` repository (see "Uploading files" below), including
   `.github/workflows/release.yml`, `tools/`, `LICENSE`, `README.md`, `CHANGELOG.md`, `PI_ToLissWebTablet.py`
   and `ToLissWebTablet/index.html`.
2. In the repository, open **Settings → Actions → General** and make sure **Allow all actions** is selected
   (it is by default). Under **Workflow permissions**, "Read and write" is not needed: the workflow asks for the
   permission it uses.

## Each release (e.g. 0.60)

1. In the two EFB files, set the version:
   - `PI_ToLissWebTablet.py`: `EFB_VERSION = "0.60"`
   - `ToLissWebTablet/index.html`: `Version: 0.60 Release Beta`
2. Add a `## 0.60` section at the top of `CHANGELOG.md`. Its text becomes the release notes users see.
3. Upload the changed files to the repository (they replace the old ones).
4. Open the **Actions** tab → **Publish release** → **Run workflow**, type `0.60`, and press **Run workflow**.

After about a minute the release `v0.60` appears under **Releases**, with the zip and its `.sha256` checksum
attached. The workflow stops without publishing if the version numbers in the two files don't match what you
typed, if `CHANGELOG.md` has no section for it, or if that version was already published.

Optionally post the same zip on the X-Plane.org forum.

## Uploading files through the GitHub website

- **Changed files**: on the repository page, **Add file → Upload files**, drag the files in (put `index.html` inside the
  `ToLissWebTablet` folder: open that folder first, then upload), and press **Commit changes**.
- **The workflow file** (first time only): folders starting with a dot can be hidden on your computer, so the
  simplest way is **Add file → Create new file**, type the name `.github/workflows/release.yml`, paste the file's
  contents, and **Commit changes**.

## Good to know

- Only `PI_ToLissWebTablet.py` and `ToLissWebTablet/index.html` are replaced by the updater. Users' settings,
  favourites, checklists, recordings and caches are untouched, and **Undo last update** restores the previous version.
- The updater refuses a download that doesn't match its checksum, and only installs while the aircraft is parked.
- Users on versions before 0.59 must install 0.59 by hand once; after that, updates are one tap.
- `tools/make_release.py` can also be run on your PC to build a release zip manually.
