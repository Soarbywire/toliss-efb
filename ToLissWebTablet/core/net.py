"""HTTPS for the EFB (Hoppie, SimBrief, VATSIM, GitHub updates).

The computer's own certificate store is tried first. If it rejects a server's certificate (common when the Python
inside XPPython3 has an out-of-date store, e.g. on a Mac where "Install Certificates.command" was never run), the
request is retried with an up-to-date set: the certifi package if installed, else the copy of Mozilla's certificates
shipped with the EFB (core/cacert.pem, from certifi, MPL-2.0). If that fails too, the error explains what to check.
"""
from __future__ import annotations

import os
import ssl
import threading
import urllib.error
import urllib.request

_lock = threading.Lock()
_fallback_ctx = None
_use_fallback = False          # once the computer's store has failed, go straight to the fallback


class CertificateError(urllib.error.URLError):
    pass


def _fallback_context():
    global _fallback_ctx
    with _lock:
        if _fallback_ctx is None:
            cafile = None
            try:
                import certifi                       # newer than ours if the user has it
                cafile = certifi.where()
            except Exception:
                pass
            if not cafile or not os.path.isfile(cafile):
                cafile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cacert.pem")
            _fallback_ctx = ssl.create_default_context(cafile=cafile)
        return _fallback_ctx


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
        _use_fallback = True
        return res
    except urllib.error.HTTPError:
        raise
    except Exception as exc:
        if _is_cert_error(exc):
            raise _explain(first or exc) from exc
        raise
