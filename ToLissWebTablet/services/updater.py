from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request

from ..core.net import urlopen as net_urlopen
import zipfile

from .. import EFB_VERSION


UPDATE_REPO = "soarbywire/toliss-efb"
UPDATE_API = "https://api.github.com/repos/{repo}/releases/latest"
UPDATE_WEB = "https://github.com/{repo}/releases/latest"
UPDATE_DL = "https://github.com/{repo}/releases/download/v{ver}/{name}"
UPDATE_RAW = "https://raw.githubusercontent.com/{repo}/v{ver}/CHANGELOG.md"
UPDATE_CHECK_INTERVAL_S = 6 * 3600
UPDATE_MAX_BYTES = 30 * 1024 * 1024

PRESERVED_PATHS = (
    "config.json",
    "stand_rules.json",
    "backup",
    "fdr",
    "ops",
    "checklists",
    "cache",
    "navdata_cache.pkl",
    "apt_index_cache.json",
)


def version_tuple(value):
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in numbers) if numbers else (0,)


class Updater:
    def __init__(self, plugin):
        self.plugin = plugin
        cfg = plugin.config if isinstance(getattr(plugin, "config", None), dict) else {}
        self.enabled = bool(cfg.get("update_check", True))
        self.latest = None
        self.checked_at = 0.0
        self.error = ""
        self.state = "idle"
        self.message = ""
        self.lock = threading.Lock()

    def paths(self):
        package = os.path.abspath(self.plugin.plugin_dir)
        root = os.path.dirname(package)
        return {
            "root": root,
            "package": package,
            "entrypoint": os.path.join(root, "PI_ToLissWebTablet.py"),
            "backups": os.path.join(root, ".tolissefb-backups"),
        }

    def status(self):
        latest = self.latest or {}
        available = bool(latest) and version_tuple(latest.get("version")) > version_tuple(EFB_VERSION)
        try:
            backups = sorted(os.listdir(self.paths()["backups"]), reverse=True)
        except OSError:
            backups = []
        return {
            "current": EFB_VERSION,
            "repo": UPDATE_REPO,
            "enabled": self.enabled,
            "latest": latest.get("version"),
            "notes": latest.get("notes", ""),
            "published": latest.get("published", ""),
            "page": latest.get("page", ""),
            "available": available,
            "checked_at": int(self.checked_at),
            "error": self.error,
            "state": self.state,
            "message": self.message,
            "backup": backups[0] if backups else None,
        }

    def _get(self, url, limit=None):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": f"ToLissEFB/{EFB_VERSION}", "Accept": "application/vnd.github+json"},
        )
        with net_urlopen(request, timeout=20) as response:
            data = response.read(limit + 1 if limit else -1)
        if limit and len(data) > limit:
            raise ValueError("download is larger than expected")
        return data

    def _check_api(self):
        raw = self._get(UPDATE_API.format(repo=UPDATE_REPO), 2 * 1024 * 1024)
        release = json.loads(raw.decode("utf-8"))
        tag = release.get("tag_name") or release.get("name") or ""
        assets = release.get("assets") or []
        archive = next((x for x in assets if str(x.get("name", "")).lower().endswith(".zip")), None)
        checksum = next((x for x in assets if str(x.get("name", "")).lower().endswith(".sha256")), None)
        notes = release.get("body") or ""
        match = re.search(r"SHA-?256[^0-9a-fA-F]*([0-9a-fA-F]{64})", notes)
        return {
            "version": tag.lstrip("vV"),
            "notes": notes[:6000],
            "published": release.get("published_at", ""),
            "page": release.get("html_url", ""),
            "zip_url": archive.get("browser_download_url") if archive else None,
            "zip_name": archive.get("name") if archive else None,
            "sha_url": checksum.get("browser_download_url") if checksum else None,
            "sha_in_notes": match.group(1).lower() if match else None,
        }

    def _check_web(self):
        request = urllib.request.Request(
            UPDATE_WEB.format(repo=UPDATE_REPO), headers={"User-Agent": f"ToLissEFB/{EFB_VERSION}"}
        )
        with net_urlopen(request, timeout=20) as response:
            final_url = response.geturl()
        match = re.search(r"/releases/tag/v?([\d.]+)", final_url)
        if not match:
            raise LookupError("No release has been published yet.")
        version = match.group(1)
        name = f"ToLiss_EFB_{version.replace('.', '_')}.zip"
        return {
            "version": version,
            "notes": "",
            "published": "",
            "page": final_url,
            "zip_url": UPDATE_DL.format(repo=UPDATE_REPO, ver=version, name=name),
            "zip_name": name,
            "sha_url": UPDATE_DL.format(repo=UPDATE_REPO, ver=version, name=name + ".sha256"),
            "sha_in_notes": None,
        }

    def check(self, force=False):
        if not force and (not self.enabled or time.time() - self.checked_at < UPDATE_CHECK_INTERVAL_S):
            return
        with self.lock:
            self.state = "checking"
            errors = []
            for method in (self._check_api, self._check_web):
                try:
                    self.latest = method()
                    self.error = ""
                    break
                except LookupError as exc:
                    self.error = str(exc)
                    errors = []
                    break
                except urllib.error.HTTPError as exc:
                    errors.append("No release has been published yet." if exc.code == 404 else f"GitHub answered {exc.code}.")
                except Exception as exc:
                    errors.append(str(exc))
            else:
                self.error = errors[-1] if errors else "Could not check for updates."
            if errors:
                self.plugin.log.xplane("ToLiss EFB: update check: " + " | ".join(errors))
            self.checked_at = time.time()
            self.state = "idle"

    @staticmethod
    def _safe_extract(archive: zipfile.ZipFile, destination: str) -> None:
        base = os.path.realpath(destination)
        for member in archive.infolist():
            normalized = member.filename.replace("\\", "/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                raise ValueError(f"unsafe absolute archive path: {member.filename}")
            target = os.path.realpath(os.path.join(base, normalized))
            if os.path.commonpath((base, target)) != base:
                raise ValueError(f"unsafe archive path traversal: {member.filename}")
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"symbolic links are not allowed in updates: {member.filename}")
        archive.extractall(destination)

    @staticmethod
    def _find_layout(extracted: str) -> str:
        candidates = []
        for directory, _, files in os.walk(extracted):
            if "PI_ToLissWebTablet.py" in files and os.path.isfile(os.path.join(directory, "ToLissWebTablet", "index.html")):
                candidates.append(directory)
        if len(candidates) != 1:
            raise ValueError("update must contain exactly one plugin root")
        package = os.path.join(candidates[0], "ToLissWebTablet")
        required = (
            os.path.join(package, "__init__.py"),
            os.path.join(package, "plugin.py"),
            os.path.join(package, "core", "bridge.py"),
            os.path.join(package, "web", "server.py"),
        )
        if not all(os.path.isfile(path) for path in required):
            raise ValueError("update is missing required modular application files")
        return candidates[0]

    @staticmethod
    def _copy_preserved(old_package: str, new_package: str) -> None:
        for relative in PRESERVED_PATHS:
            source = os.path.join(old_package, relative)
            target = os.path.join(new_package, relative)
            if not os.path.exists(source):
                continue
            if os.path.isdir(source):
                if os.path.exists(target):
                    shutil.rmtree(target)
                shutil.copytree(source, target)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(source, target)

    def _install_bytes(self, data: bytes, expected_sha256: str, version: str) -> str:
        actual = hashlib.sha256(data).hexdigest()
        if not expected_sha256 or actual.lower() != expected_sha256.lower():
            raise ValueError("the download does not match its SHA-256 checksum")
        paths = self.paths()
        os.makedirs(paths["backups"], exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tolissefb-update-", dir=paths["root"]) as temporary:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self._safe_extract(archive, temporary)
            layout = self._find_layout(temporary)
            staged_package = os.path.join(layout, "ToLissWebTablet")
            entrypoint_source = os.path.join(layout, "PI_ToLissWebTablet.py")
            with open(entrypoint_source, "r", encoding="utf-8") as handle:
                compile(handle.read(), entrypoint_source, "exec")
            for directory, _, files in os.walk(staged_package):
                for filename in files:
                    if filename.endswith(".py"):
                        source_path = os.path.join(directory, filename)
                        with open(source_path, "r", encoding="utf-8") as handle:
                            compile(handle.read(), source_path, "exec")
            self._copy_preserved(paths["package"], staged_package)

            stamp = f"{EFB_VERSION}_{time.strftime('%Y%m%d_%H%M%S')}"
            backup = os.path.join(paths["backups"], stamp)
            os.makedirs(backup)
            shutil.copy2(paths["entrypoint"], os.path.join(backup, "PI_ToLissWebTablet.py"))
            shutil.copytree(paths["package"], os.path.join(backup, "ToLissWebTablet"))

            old_package = paths["package"] + ".update-old"
            new_entry = paths["entrypoint"] + ".new"
            if os.path.exists(old_package):
                shutil.rmtree(old_package)
            shutil.copy2(os.path.join(layout, "PI_ToLissWebTablet.py"), new_entry)
            moved_old = False
            installed_new = False
            try:
                os.replace(paths["package"], old_package)
                moved_old = True
                os.replace(staged_package, paths["package"])
                installed_new = True
                os.replace(new_entry, paths["entrypoint"])
            except Exception:
                if installed_new and os.path.exists(paths["package"]):
                    failed = paths["package"] + ".update-failed"
                    if os.path.exists(failed):
                        shutil.rmtree(failed)
                    os.replace(paths["package"], failed)
                    shutil.rmtree(failed, ignore_errors=True)
                if moved_old and os.path.exists(old_package):
                    os.replace(old_package, paths["package"])
                raise
            finally:
                if os.path.exists(new_entry):
                    os.unlink(new_entry)
            shutil.rmtree(old_package)
            return backup

    def install(self):
        with self.lock:
            latest = self.latest or {}
            try:
                if version_tuple(latest.get("version")) <= version_tuple(EFB_VERSION):
                    raise ValueError("no newer version to install")
                if not latest.get("zip_url"):
                    raise ValueError("the release has no zip file attached")
                fdr = getattr(self.plugin, "fdr", None)
                if fdr is not None and getattr(fdr, "recording", False):
                    raise ValueError("stop the flight data recorder first (its file is open in the EFB folder)")
                self.state = "downloading"
                data = self._get(latest["zip_url"], UPDATE_MAX_BYTES)
                expected = latest.get("sha_in_notes")
                if latest.get("sha_url"):
                    checksum_text = self._get(latest["sha_url"], 4096).decode("utf-8", errors="ignore")
                    match = re.search(r"[0-9a-fA-F]{64}", checksum_text)
                    expected = match.group(0).lower() if match else expected
                if not expected:
                    raise ValueError("the release has no SHA-256 checksum")
                backup = self._install_bytes(data, expected, latest.get("version", "unknown"))
                self.state = "installed"
                self.message = f"Version {latest.get('version')} installed; full backup: {backup}. Reload XPPython3 scripts."
                self.plugin.log.xplane(self.message)
            except Exception as exc:
                self.state = "failed"
                self.message = f"Update not installed: {exc}"
                self.plugin.log.xplane(self.message)

    def rollback(self):
        with self.lock:
            paths = self.paths()
            try:
                backups = sorted(os.listdir(paths["backups"]), reverse=True)
                if not backups:
                    raise ValueError("there is no backup to restore")
                backup = os.path.join(paths["backups"], backups[0])
                staged = paths["package"] + ".rollback-new"
                old = paths["package"] + ".rollback-old"
                if os.path.exists(staged):
                    shutil.rmtree(staged)
                shutil.copytree(os.path.join(backup, "ToLissWebTablet"), staged)
                shutil.copy2(os.path.join(backup, "PI_ToLissWebTablet.py"), paths["entrypoint"] + ".new")
                if os.path.exists(old):
                    shutil.rmtree(old)
                os.replace(paths["package"], old)
                os.replace(staged, paths["package"])
                os.replace(paths["entrypoint"] + ".new", paths["entrypoint"])
                shutil.rmtree(old)
                self.state = "restored"
                self.message = f"Restored backup {backups[0]}; reload XPPython3 scripts."
            except Exception as exc:
                self.state = "failed"
                self.message = f"Could not restore: {exc}"

