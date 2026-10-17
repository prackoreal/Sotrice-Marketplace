"""HTTP for the real-city-data pipeline (Overpass, Nominatim, terrain tiles).

TLS uses the OPERATING SYSTEM's trust store through the vendored
`truststore` package (clients/python/_vendor/truststore, MIT, pure Python)
instead of OpenSSL's own CA bundle. On the machines this app runs on
(school laptops with TLS-inspecting proxies, Windows boxes with a stale or
empty OpenSSL bundle) the OS store is what actually knows the right roots;
Python's bundled lookup answered "certificate has expired" for every
Overpass host while the OS trusted them fine. Certificate verification is
NEVER disabled: if the OS store does not trust a server either, the request
fails.

Everything here is stdlib + the vendored package, so it works unchanged
under the system Python and under the embeddable Python the app may bundle.
"""

import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

USER_AGENT = "Sotrice-CityData/1.0 (+educational city simulator; contact laszlo.siller.jr@silleradvisory.com)"

_HERE = os.path.dirname(os.path.abspath(__file__))
_trust_lock = threading.Lock()
_trust_source: Optional[str] = None


def ensure_trust() -> str:
    """Installs the OS trust store into `ssl` once per process. Returns
    "os-truststore" when active, or "openssl-default" if the vendored
    package could not be loaded (the request then relies on OpenSSL's
    own bundle, and may fail with a certificate error — loudly, not
    silently)."""
    global _trust_source
    with _trust_lock:
        if _trust_source is not None:
            return _trust_source
        vendor = os.path.join(_HERE, "_vendor")
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        try:
            import truststore  # type: ignore

            truststore.inject_into_ssl()
            _trust_source = "os-truststore"
        except Exception as exc:  # truststore needs Python >= 3.10 and a working ctypes
            print(f"citydata_http: OS trust store unavailable ({exc!r}); using OpenSSL's default CA bundle")
            _trust_source = "openssl-default"
        return _trust_source


class HttpError(Exception):
    def __init__(self, status: Optional[int], message: str, retry_after: Optional[float] = None):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.status is None or self.status in (408, 425, 429, 500, 502, 503, 504)


def request(url: str, data: Optional[dict] = None, headers: Optional[dict] = None, timeout: float = 60.0) -> bytes:
    """One request, no retry (callers decide retry/fallback policy, since
    the right one differs between Overpass mirrors and Nominatim). `data`
    makes it a form-encoded POST. Raises HttpError."""
    ensure_trust()
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        retry_after = None
        try:
            retry_after = float(exc.headers.get("Retry-After")) if exc.headers.get("Retry-After") else None
        except ValueError:
            pass
        raise HttpError(exc.code, (exc.reason or "")[:200], retry_after) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise HttpError(None, f"{type(exc).__name__}: {exc}"[:300]) from exc


class RateLimiter:
    """Blocks callers so at most one call starts per `min_interval`
    seconds (Nominatim's usage policy: max 1 request/second)."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if delay > 0:
            time.sleep(delay)
