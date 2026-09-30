#!/usr/bin/env python3
"""Build a ToLiss EFB release: the zip to attach to a GitHub release, plus its SHA-256 checksum file.

Usage (from the folder that contains "ToLiss EFB <version>"):
    python make_release.py "ToLiss EFB 0.60"

It checks that the version in PI_ToLissWebTablet.py (EFB_VERSION) and in index.html ("Version: ...")
match the folder name, then writes ToLiss_EFB_0_60.zip and ToLiss_EFB_0_60.zip.sha256.
"""
import hashlib, os, re, sys, zipfile

def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    folder = sys.argv[1].rstrip("/\\")
    m = re.search(r"(\d+(?:\.\d+)+)$", folder)
    if not m:
        sys.exit(f'The folder name should end with the version, e.g. "ToLiss EFB 0.60" (got "{folder}")')
    version = m.group(1)
    py = os.path.join(folder, "PI_ToLissWebTablet.py")
    html = os.path.join(folder, "ToLissWebTablet", "index.html")
    for f in (py, html):
        if not os.path.isfile(f):
            sys.exit(f"Missing {f}")
    py_src = open(py, encoding="utf-8").read()
    html_src = open(html, encoding="utf-8").read()
    pv = re.search(r'^EFB_VERSION\s*=\s*"([^"]+)"', py_src, re.M)
    hv = re.search(r"Version:\s*([\d.]+)", html_src)
    problems = []
    if not pv or pv.group(1) != version:
        problems.append(f'PI_ToLissWebTablet.py has EFB_VERSION = "{pv.group(1) if pv else "?"}"')
    if not hv or hv.group(1) != version:
        problems.append(f'index.html shows "Version: {hv.group(1) if hv else "?"}"')
    if problems:
        sys.exit(f"Version mismatch with the folder ({version}): " + "; ".join(problems))
    compile(py_src, py, "exec")   # the plugin file must at least be valid Python

    out = f"ToLiss_EFB_{version.replace('.', '_')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "backup", "fdr", "checklists")]
            for name in files:
                if name.endswith((".pyc", ".pkl")) or name in ("apt_index_cache.json", "config.json"):
                    continue
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, os.path.dirname(os.path.abspath(folder)) or "."))
    digest = hashlib.sha256(open(out, "rb").read()).hexdigest()
    with open(out + ".sha256", "w") as f:
        f.write(f"{digest}  {out}\n")
    print(f"Created {out} and {out}.sha256")
    print(f"SHA-256: {digest}")
    print(f'GitHub release tag: v{version}   (attach both files to the release)')

if __name__ == "__main__":
    main()
