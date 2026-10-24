"""HTTP transport for the owner-operated FNV project API."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .notion import _retry_after

USER_AGENT = "fnv-relay/" + __version__

READ_TOOLS = frozenset({"status", "list_bands", "function_info", "function_history", "packet"})
MAX_PACK = 1024 ** 3
MAX_INLINE_BACKOFF = 30


def _origin(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An HTTPS URL without embedded credentials is required")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or 443


class _SameOrigin(HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            allowed = _origin(newurl) == self.origin
        except ValueError:
            allowed = False
        if not allowed or req.get_method() not in {"GET", "HEAD"}:
            raise HTTPError(req.full_url, code, "Redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProjectError(RuntimeError):
    def __init__(self, status=None, retry_after=None, uncertain=False, code="transport_error", message=""):
        self.status = status
        self.retry_after = retry_after
        self.uncertain = uncertain
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "api_error"
        self.message = message[:300]
        super().__init__(f"FNV {self.code}" + (f" (HTTP {status})" if status else "") + (f": {self.message}" if self.message else ""))


class FNV:
    def __init__(self, key: str, *, base="https://fnvdecomp.com", opener=None, sleep=time.sleep, timeout=60):
        if not key or any(c.isspace() or ord(c) < 32 for c in key):
            raise ValueError("A project API key is required")
        self.base = base.rstrip("/")
        self._origin = _origin(self.base)
        if urlsplit(self.base).query or urlsplit(self.base).fragment:
            raise ValueError("Project base URL cannot include a query or fragment")
        self._key = key
        self._opener = opener or build_opener(_SameOrigin(self._origin)).open
        self._sleep = sleep
        self.timeout = timeout
        self._stop_wait = None

    def set_stop_event(self, event):
        self._stop_wait = event.wait if event is not None else None

    def pause(self, delay):
        if self._stop_wait is not None:
            if self._stop_wait(delay): raise ProjectError(code="cancelled")
        else:
            self._sleep(delay)

    def call(self, tool: str, args: dict | None = None):
        if not re.fullmatch(r"[a-z_]+", tool):
            raise ValueError("Invalid FNV tool name")
        raw = json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        read = tool in READ_TOOLS
        for attempt in range(4):
            if self._stop_wait is not None and self._stop_wait(0):
                raise ProjectError(code="cancelled")
            req = Request(self.base + "/api/v1/" + tool, data=raw, method="POST", headers={"User-Agent": USER_AGENT, "Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
            try:
                with self._opener(req, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as exc:
                try:
                    data = json.loads(exc.read())
                    if not isinstance(data, dict):
                        data = {}
                except (ValueError, OSError):
                    data = {}
                delay = _retry_after(exc.headers, data)
                code = data.get("code", "api_error")
                if isinstance(code, str) and self._key in code:
                    code = "api_error"
                # No service-controlled text enters logs; it may echo API keys or source.
                failure = ProjectError(exc.code, delay, not read and (exc.code >= 500 or 300 <= exc.code < 400), code)
                if not read or exc.code not in {429, 500, 502, 503, 504, 529} or attempt == 3:
                    raise failure from None
                backoff = max(delay or 0, 2 ** attempt)
                if backoff > MAX_INLINE_BACKOFF:
                    raise failure from None  # Scheduler waits without retaining lane/lease locks.
                self.pause(backoff)
            except (URLError, OSError, TimeoutError):
                if not read or attempt == 3:
                    raise ProjectError(uncertain=not read) from None
                self.pause(2 ** attempt)
            except (ValueError, UnicodeError):
                raise ProjectError(uncertain=not read, code="invalid_response") from None
        raise AssertionError("unreachable")

    def download_pack(self, url: str, dest) -> str:
        url = urljoin(self.base + "/", url)
        if _origin(url) != self._origin:
            raise ValueError("Pack URL must use the configured HTTPS project origin")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        digest = hashlib.sha256()
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Authorization": "Bearer " + self._key})
            with self._opener(req, timeout=self.timeout) as response:
                if hasattr(response, "geturl") and _origin(response.geturl()) != self._origin:
                    raise ProjectError(code="unsafe_redirect")
                length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                if length is not None and int(length) > MAX_PACK:
                    raise ProjectError(code="pack_too_large")
                with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".pack-", delete=False) as output:
                    temporary = Path(output.name)
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_PACK:
                            raise ProjectError(code="pack_too_large")
                        digest.update(chunk)
                        output.write(chunk)
                    if length is not None and total != int(length):
                        raise ProjectError(code="incomplete_pack")
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary, dest)
            temporary = None
            directory = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return digest.hexdigest()
        except ProjectError:
            raise
        except (HTTPError, URLError, OSError, ValueError):
            raise ProjectError(code="download_failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
