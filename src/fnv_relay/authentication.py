"""FNV device approval without exposing device codes or API keys."""
from __future__ import annotations

import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, build_opener

from .project import FNV, _SameOrigin, _origin

BASE = "https://fnvdecomp.com"


class ConnectionError(RuntimeError):
    """A sanitized device-flow failure, never containing remote response text."""


def connect(store, label="fnv-relay", *, opener=None, sleep=time.sleep, clock=time.monotonic, show=print):
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100 or any(ord(c) < 32 for c in label):
        raise ValueError("Invalid agent label")
    opener = opener or build_opener(_SameOrigin(_origin(BASE))).open

    def request(path, body, timeout=30):
        req = Request(BASE + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with opener(req, timeout=timeout) as response:
            # Bound responses before parsing; a connection record is tiny.
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ConnectionError("Invalid approval response.")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ConnectionError("Invalid approval response.")
            return result

    # Creating an approval is a mutation: never automatically repeat it.
    try:
        initial = request("/api/v1/connect", {"agent_label": label.strip()})
    except Exception:
        raise ConnectionError("Approval request failed; it was not automatically retried.") from None
    try:
        device = initial["device_code"]
        public = initial["user_code"]
        url = initial["approve_url"]
        interval, lifetime = float(initial["interval"]), float(initial["expires_in"])
        if not isinstance(device, str) or not 1 <= len(device) <= 4096:
            raise ValueError()
        if not isinstance(public, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", public) or device in public:
            raise ValueError()
        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url):
            raise ValueError()
        if _origin(url) != _origin(BASE) or device in unquote(url) or urlsplit(url).fragment:
            raise ValueError()
        if not math.isfinite(interval) or not 1 <= interval <= 300 or not math.isfinite(lifetime) or not 1 <= lifetime <= 3600:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ConnectionError("Invalid approval response; no credentials were displayed.") from None
    show("Open this FNV approval link and approve with GitHub: " + url)
    show("Public approval code: " + public)
    deadline = clock() + lifetime
    delay = interval
    while True:
        remaining = deadline - clock()
        if remaining <= delay:
            raise ConnectionError("Approval expired. Start connect again when ready.")
        sleep(delay)
        remaining = deadline - clock()
        if remaining <= 0:
            raise ConnectionError("Approval expired. Start connect again when ready.")
        delay = interval
        try:
            result = request("/api/v1/connect/poll", {"device_code": device}, timeout=min(30, remaining))
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504, 529}:
                raise ConnectionError("Approval polling failed; no credentials were displayed.") from None
            try:
                retry = float(exc.headers.get("Retry-After", interval))
                if math.isfinite(retry):
                    delay = max(interval, retry)
            except (ValueError, TypeError):
                pass
            continue
        except (URLError, OSError, TimeoutError):
            continue  # Polling is safe, but always wait the server's interval.
        except (ValueError, UnicodeError):
            raise ConnectionError("Invalid approval poll response.") from None
        status = result.get("status")
        if status == "approved":
            key = result.get("api_key")
            try:
                FNV(key)  # Validate locally before private persistence, no extra request.
            except (ValueError, TypeError):
                raise ConnectionError("Approval returned an invalid credential.") from None
            store.set_secret("fnv_api_key", key)
            show("FNV credential stored privately.")
            return
        if status in {"expired", "denied", "rejected"}:
            raise ConnectionError("Approval expired or was declined. Start connect again when ready.")
        if status not in {"pending", "authorization_pending"}:
            raise ConnectionError("Unrecognized approval status; no credentials were displayed.")
