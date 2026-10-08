import os
import re


def checklist_dir(plugin_dir):
    directory = os.path.join(plugin_dir, "checklists")
    os.makedirs(directory, exist_ok=True)
    return directory


def checklist_safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(name or ""))[:120]

