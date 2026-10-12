"""HTTPS for the EFB (Hoppie, SimBrief, VATSIM, GitHub updates).

The computer's own certificate store is tried first. If it rejects a server's certificate (common when the Python
inside XPPython3 has an out-of-date store, e.g. on a Mac where "Install Certificates.command" was never run), the
request is retried with a combined set: the certifi package if installed, the operating system's own certificate
file (on a Mac /etc/ssl/cert.pem) and the copy of Mozilla's certificates shipped with the EFB (core/cacert.pem, from
certifi, MPL-2.0). If that fails too, the error explains what to check. Certificates are always checked: the EFB
never turns verification off.
"""
from __future__ import annotations

import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

_lock = threading.Lock()
_fallback_ctx = None
_use_fallback = False          # once the computer's store has failed, go straight to the fallback
_logger = None                 # the plugin's log (set_logger), told once when the fallback is first used
_sources = []                  # the certificate files the fallback uses

# operating-system certificate files: macOS (and *BSD), Debian/Ubuntu, Fedora/RHEL, openSUSE
SYSTEM_CA_FILES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                   "/etc/ssl/ca-bundle.pem")


def set_logger(fn):
    global _logger
    _logger = fn


def ca_files(extra=()):
    """The certificate files to fall back on, in order: certifi, the system's, then the given extra files."""
    out = []
    try:
        import certifi                           # newer than ours if the user has it
        out.append(certifi.where())
    except Exception:
        pass
    out += list(SYSTEM_CA_FILES) + list(extra)
    seen, files = set(), []
    for f in out:
        if f and f not in seen and os.path.isfile(f):
            seen.add(f)
            files.append(f)
    return files


class CertificateError(urllib.error.URLError):
    pass


def build_context(files):
    """A verifying SSL context trusting every certificate in the given files (unreadable files are skipped)."""
    ctx = ssl.create_default_context()           # verification on, hostname checked
    used = []
    for f in files:
        try:
            ctx.load_verify_locations(cafile=f)
            used.append(f)
        except Exception:
            continue
    return ctx, used


def _fallback_context():
    global _fallback_ctx, _sources
    with _lock:
        if _fallback_ctx is None:
            bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cacert.pem")
            _fallback_ctx, _sources = build_context(ca_files([bundled]))
        return _fallback_ctx


def _note_fallback(url):
    if _logger:
        try:
            host = urllib.parse.urlparse(url).netloc or url
        except Exception:
            host = url
        try:
            _logger(f"ToLiss EFB: this computer's certificates were rejected for {host}; using the EFB's certificate "
                    f"fallback ({', '.join(os.path.basename(os.path.dirname(f)) + '/' + os.path.basename(f) for f in _sources) or 'none found'}). "
                    f"Certificates are still checked.\n")
        except Exception:
            pass


def _is_cert_error(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _explain(exc):
    text = str(getattr(exc, "reason", exc))
    if "expired" in text or "not yet valid" in text:
        why = "the certificate looks expired or not yet valid: check this computer's date, time and time zone"
    else:
        why = "the server's certificate could not be checked"
    return CertificateError(
        f"secure connection failed ({why}). Usually this computer's clock is wrong or its security certificates are "
        f"out of date: set the date and time automatically; on a Mac run 'Install Certificates.command' in the "
        f"Python folder under Applications; on Windows run Windows Update; antivirus HTTPS scanning can also cause it.")


def urlopen(req, timeout=20, **kw):
    """urllib.request.urlopen with the certificate fallback above (same arguments and result)."""
    global _use_fallback
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    if not url.lower().startswith("https:") or kw.get("context") is not None:
        return urllib.request.urlopen(req, timeout=timeout, **kw)
    if not _use_fallback:
        try:
            return urllib.request.urlopen(req, timeout=timeout, **kw)
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            if not _is_cert_error(exc):
                raise
            first = exc
    else:
        first = None
    try:
        res = urllib.request.urlopen(req, timeout=timeout, context=_fallback_context(), **kw)
        if not _use_fallback:
            _use_fallback = True
            _note_fallback(url)
        return res
    except urllib.error.HTTPError:
        raise
    except Exception as exc:
        if _is_cert_error(exc):
            raise _explain(first or exc) from exc
        raise
