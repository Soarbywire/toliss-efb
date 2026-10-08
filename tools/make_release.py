#!/usr/bin/env python3
"""Build a ToLiss EFB release: the zip to attach to a GitHub release, plus its SHA-256 checksum file.

Usage (from the folder that contains "ToLiss EFB <version>"):
    python make_release.py "ToLiss EFB 0.67"

The folder holds PI_ToLissWebTablet.py, the ToLissWebTablet package (with index.html) and CHANGELOG.md.
It checks that the version matches everywhere (ToLissWebTablet/__init__.py EFB_VERSION, STUB_VERSION in
PI_ToLissWebTablet.py and "Version: ..." in index.html), that every Python file compiles and that the package is
complete, then writes ToLiss_EFB_0_67.zip and ToLiss_EFB_0_67.zip.sha256. Users' data (settings, recordings,
caches, backups) is never packed, even if it is in the folder.
"""
import hashlib, os, re, sys, zipfile

REQUIRED = ("PI_ToLissWebTablet.py", "ToLissWebTablet/__init__.py", "ToLissWebTablet/plugin.py",
            "ToLissWebTablet/index.html", "ToLissWebTablet/core/bridge.py", "ToLissWebTablet/web/server.py")
SKIP_DIRS = {"__pycache__", "backup", "fdr", "checklists", "ops", "cache", ".git", ".tolissefb-backups"}
SKIP_FILES = {"config.json", "stand_rules.json", "apt_index_cache.json", "navdata_cache.pkl", ".DS_Store"}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    folder = sys.argv[1].rstrip("/\\")
    m = re.search(r"(\d+(?:\.\d+)+)$", folder)
    if not m:
        sys.exit(f'The folder name should end with the version, e.g. "ToLiss EFB 0.67" (got "{folder}")')
    version = m.group(1)
    missing = [r for r in REQUIRED if not os.path.isfile(os.path.join(folder, *r.split("/")))]
    if missing:
        sys.exit("Missing: " + ", ".join(missing))
    read = lambda *p: open(os.path.join(folder, *p), encoding="utf-8").read()
    found = {
        "ToLissWebTablet/__init__.py EFB_VERSION": re.search(r'^EFB_VERSION\s*=\s*"([^"]+)"', read("ToLissWebTablet", "__init__.py"), re.M),
        "PI_ToLissWebTablet.py STUB_VERSION": re.search(r'^STUB_VERSION\s*=\s*"([^"]+)"', read("PI_ToLissWebTablet.py"), re.M),
        "index.html Version": re.search(r"Version:\s*([\d.]+)", read("ToLissWebTablet", "index.html")),
    }
    problems = [f'{k} is "{v.group(1) if v else "?"}"' for k, v in found.items() if not v or v.group(1) != version]
    if problems:
        sys.exit(f"Version mismatch with the folder ({version}): " + "; ".join(problems))

    files = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name in SKIP_FILES or name.endswith((".pyc", ".pkl", ".tmp")):
                continue
            full = os.path.join(root, name)
            if name.endswith(".py"):
                compile(open(full, encoding="utf-8").read(), full, "exec")    # every Python file must be valid
            files.append(full)

    out = f"ToLiss_EFB_{version.replace('.', '_')}.zip"
    base = os.path.dirname(os.path.abspath(folder))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for full in files:
            z.write(full, os.path.relpath(os.path.abspath(full), base).replace(os.sep, "/"))
    digest = hashlib.sha256(open(out, "rb").read()).hexdigest()
    with open(out + ".sha256", "w") as f:
        f.write(f"{digest}  {out}\n")
    print(f"Created {out} ({len(files)} files) and {out}.sha256")
    print(f"SHA-256: {digest}")
    print(f"GitHub release tag: v{version}   (attach both files to the release)")


if __name__ == "__main__":
    main()
