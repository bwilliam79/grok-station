"""HTTPS GET for a release manifest. release.py stays free of sockets.

Follows at most 5 redirects, https only, and only to github.com or
objects.githubusercontent.com. TLS certificate and hostname checks stay on.
Tests pass `opener`; the default is the only function here that opens a socket.
"""
import http.client
from urllib.parse import urljoin, urlsplit

import release

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 5


def _check_context(context):
    if context is None or getattr(context, "verify_mode", None) != release.ssl.CERT_REQUIRED:
        raise release.ManifestError("fetch-failed", "tls")
    if getattr(context, "check_hostname", None) is not True:
        raise release.ManifestError("fetch-failed", "tls")


def _resolve_redirect(base, location):
    if not isinstance(location, str) or not location:
        raise release.ManifestError("fetch-failed", "redirect")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in location) or "\\" in location:
        raise release.ManifestError("fetch-failed", "redirect")
    try:
        return urljoin(base, location)
    except ValueError:
        raise release.ManifestError("fetch-failed", "redirect") from None


def _open_once(url, context):
    """One HTTPS GET. Does not follow redirects. Uses `context` as given."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, context=context, timeout=20)
    try:
        conn.request("GET", path, headers={"User-Agent": "grok-relay", "Accept": "application/json"})
        response = conn.getresponse()
        location = response.getheader("Location")
        body = response.read(release.MAX_MANIFEST_BYTES + 1)
        return response.status, location, body
    except release.ManifestError:
        raise
    except Exception:
        raise release.ManifestError("fetch-failed", "transport") from None
    finally:
        conn.close()


def https_get(url, context, opener=None):
    """GET a release URL. `opener(url, context)` returns `(status, location, body)`."""
    open_once = opener or _open_once
    _check_context(context)
    current = release._require_manifest_url(url)
    for hop in range(_MAX_HOPS):
        try:
            status, location, body = open_once(current, context)
        except release.ManifestError:
            raise
        except Exception:
            raise release.ManifestError("fetch-failed", "transport") from None
        if status == 200:
            if isinstance(body, str):
                body = body.encode("utf-8")
            if not isinstance(body, (bytes, bytearray)):
                raise release.ManifestError("fetch-failed", "body")
            if len(body) > release.MAX_MANIFEST_BYTES:
                raise release.ManifestError("fetch-failed", "size")
            return bytes(body)
        if status not in _REDIRECTS or hop == _MAX_HOPS - 1 or not location:
            reason = "redirect" if status in _REDIRECTS else "status"
            raise release.ManifestError("fetch-failed", reason)
        current = release._require_manifest_url(_resolve_redirect(current, location))
    raise release.ManifestError("fetch-failed", "redirect")
