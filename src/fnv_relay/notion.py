"""Small, dependency-free Notion transport; writes are never retried ambiguously."""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
MAX_PAYLOAD = 500_000
MAX_RECORD = 64_000


class NotionError(RuntimeError):
    """Sanitized transport error. safe_retry means the request can be repeated."""

    def __init__(self, status=None, retry_after=None, safe_retry=False, code="transport_error"):
        self.status = status
        self.retry_after = retry_after
        self.safe_retry = safe_retry
        # Never include response messages, URLs, or exception strings (may contain secrets).
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "api_error"
        super().__init__(f"Notion {self.code}" + (f" (HTTP {status})" if status else ""))


def page_id(value: str) -> str:
    """Accept a UUID or a standard Notion page URL, never an arbitrary URL."""
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in {"notion.so", "www.notion.so", "app.notion.com"} or parsed.username or parsed.password or parsed.port:
            raise ValueError("Expected a Notion page URL or UUID")
        value = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$", value)
        if not match:
            raise ValueError("Notion URL has no page UUID")
        value = match.group(1)
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("Expected a Notion page UUID") from None


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _rich(text: str) -> list[dict]:
    # Notion's limit is 2,000 characters, not bytes. Slicing preserves exact Unicode.
    parts = [text[i:i + 2000] for i in range(0, len(text), 2000)] or [""]
    if len(parts) > 100:
        raise ValueError("Text exceeds Notion rich-text limit")
    return [{"type": "text", "text": {"content": part}} for part in parts]


def _record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("A relay record must be a JSON object")
    raw = _json(record)
    if len(raw) > MAX_RECORD:
        raise ValueError("Relay record exceeds 64,000 UTF-8 bytes")
    return {"object": "block", "type": "code", "code": {"language": "json", "rich_text": _rich(raw.decode("utf-8"))}}


def _text(block: dict) -> str:
    body = block.get(block.get("type"), {})
    return "".join(item.get("text", {}).get("content", item.get("plain_text", "")) for item in body.get("rich_text", []))


def _retry_after(headers, data) -> float | None:
    value = headers.get("Retry-After") or data.get("additional_data", {}).get("retry_after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


class _NotionRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != "api.notion.com" or parsed.port not in (None,443) or parsed.username or parsed.password or req.get_method() not in ("GET","HEAD"):
            raise HTTPError(req.full_url,code,"Redirect refused",headers,fp)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class Notion:
    def __init__(self, token: str, *, opener=None, sleep=time.sleep, timeout=45):
        if not token or any(c.isspace() or ord(c) < 32 for c in token):
            raise ValueError("A nonempty Notion connection token is required")
        self._token = token
        self._opener = opener or build_opener(_NotionRedirect()).open
        self._sleep = sleep
        self.timeout = timeout
        self._started = False

    def request(self, method: str, path: str, payload=None) -> dict:
        method = method.upper()
        if not path.startswith("/") or "://" in path:
            raise ValueError("Expected a relative Notion API path")
        raw = _json(payload) if payload is not None else None
        if raw is not None and len(raw) >= MAX_PAYLOAD:
            raise ValueError("Notion request must be smaller than 500,000 bytes")
        for attempt in range(4):
            if self._started:
                self._sleep(1 / 3)
            self._started = True
            req = Request(API + path, data=raw, method=method, headers={
                "Authorization": "Bearer " + self._token,
                "Notion-Version": VERSION,
                "Content-Type": "application/json",
            })
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
                reason = data.get("additional_data", {}).get("rate_limit_reason")
                blocked = reason == "public_api_request_blocked"
                safe = not blocked and (exc.code in {429, 529} or method == "GET")
                retryable = not blocked and (exc.code in {429, 529} or (method == "GET" and exc.code in {500, 502, 503, 504}))
                delay = _retry_after(exc.headers, data)
                code = data.get("code", "api_error")
                if isinstance(code, str) and self._token in code:
                    code = "api_error"
                error = NotionError(exc.code, delay, safe, code)
                if not retryable or attempt == 3:
                    raise error from None
                self._sleep(max(delay or 0, 2 ** attempt))
            except (URLError, TimeoutError, OSError):
                if method != "GET" or attempt == 3:
                    raise NotionError(safe_retry=method == "GET") from None
                self._sleep(2 ** attempt)
            except (ValueError, UnicodeError):
                # A write may have succeeded even if its response cannot be decoded.
                raise NotionError(safe_retry=method == "GET", code="invalid_response") from None
        raise AssertionError("unreachable")

    def whoami(self) -> dict:
        return self.request("GET", "/users/me")

    def page(self, identifier: str) -> dict:
        return self.request("GET", "/pages/" + page_id(identifier))

    def children(self, identifier: str) -> list[dict]:
        path = "/blocks/" + page_id(identifier) + "/children"
        result, cursor, seen = [], None, set()
        while True:
            query = {"page_size": 100}
            if cursor is not None:
                query["start_cursor"] = cursor
            data = self.request("GET", path + "?" + urlencode(query))
            result.extend(data.get("results", []))
            if not data.get("has_more"):
                return result
            cursor = data.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise NotionError(code="invalid_pagination", safe_retry=True)
            seen.add(cursor)

    def create_page(self, parent_id: str, title: str, records: list[dict], text: str = "") -> str:
        children = []
        if text:
            # A paragraph can contain at most 100 rich-text segments.
            for i in range(0, len(text), 200_000):
                children.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(text[i:i + 200_000])}})
        children.extend(_record(record) for record in records)
        if len(children) > 100:
            raise ValueError("A page creation can contain at most 100 blocks")
        payload = {"parent": {"type": "page_id", "page_id": page_id(parent_id)}, "properties": {"title": {"type": "title", "title": _rich(title)}}, "children": children}
        return self.request("POST", "/pages", payload)["id"]

    def append(self, identifier: str, record: dict) -> None:
        self.request("PATCH", "/blocks/" + page_id(identifier) + "/children", {"children": [_record(record)]})

    def messages(self, identifier: str) -> list[dict]:
        result, visited = [], set()

        def visit(current, depth):
            normalized = page_id(current)
            if normalized in visited:
                return
            visited.add(normalized)
            if depth > 8:
                raise NotionError(code="nesting_limit", safe_retry=True)
            for block in self.children(normalized):
                kind = block.get("type")
                if kind == "child_page":
                    continue
                content = _text(block)
                candidates = []
                if kind == "code" and block.get("code", {}).get("language") == "json":
                    candidates.append(content)
                if kind == "paragraph":
                    candidates.extend(re.findall(r"```json\s*\n(.*?)\n```", content, flags=re.DOTALL | re.IGNORECASE))
                for candidate in candidates:
                    try:
                        record = json.loads(candidate)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(record, dict):
                        result.append(record)
                if block.get("has_children"):
                    visit(block["id"], depth + 1)
        visit(identifier, 0)
        return result

    def child_pages(self, identifier: str) -> list[dict]:
        return [{"id": block["id"], "title": block["child_page"]["title"]} for block in self.children(identifier) if block.get("type") == "child_page"]
