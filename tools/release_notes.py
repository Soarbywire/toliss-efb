#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version, for use as GitHub release notes.
Usage: python tools/release_notes.py 0.60"""
import re, sys

version = sys.argv[1]
text = open("CHANGELOG.md", encoding="utf-8").read()
m = re.search(rf"^## {re.escape(version)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
if not m:
    sys.exit(f"CHANGELOG.md has no '## {version}' section")
print(m.group(1).strip())
print("\n---\nInstall from **Settings → Updates** in the EFB, or manually: copy `PI_ToLissWebTablet.py` and the "
      "`ToLissWebTablet` folder into `X-Plane 12/Resources/plugins/PythonPlugins/` (replace the files, keep your settings).")
