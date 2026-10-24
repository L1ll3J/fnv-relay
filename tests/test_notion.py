import hashlib
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from fnv_relay.notion import Notion, NotionError, VERSION, page_id

ID = "01234567-89ab-cdef-0123-456789abcdef"


def response(value):
    return io.BytesIO(json.dumps(value, ensure_ascii=False).encode())


class Server:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.sleeps = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return response(result)

    def client(self):
        return Notion("secret_test", opener=self, sleep=self.sleeps.append)


def error(status, body=None, headers=None):
    return HTTPError("https://api.notion.com/v1/pages", status, "sensitive message", headers or {}, response(body or {"code": "api_error"}))


def test_pagination_passes_opaque_cursor_and_headers():
    server = Server({"results": [{"id": "one"}], "has_more": True, "next_cursor": "opaque /+?"}, {"results": [{"id": "two"}], "has_more": False})
    assert server.client().children(ID) == [{"id": "one"}, {"id": "two"}]
    assert "start_cursor=opaque+%2F%2B%3F" in server.calls[1].full_url
    assert server.calls[0].get_header("Authorization") == "Bearer secret_test"
    assert server.calls[0].get_header("Notion-version") == VERSION
    assert server.sleeps == [1 / 3]


def test_unicode_and_source_preserved_exactly():
    source = "é😀\r\n\t" * 3000
    record = {"source": source, "sha256": hashlib.sha256(source.encode()).hexdigest()}
    server = Server({"id": ID})
    assert server.client().create_page(ID, "Lane", [record], text="hello\r\n") == ID
    payload = json.loads(server.calls[0].data)
    rich = payload["children"][1]["code"]["rich_text"]
    assert len(rich) > 1
    assert all(len(part["text"]["content"]) <= 2000 for part in rich)
    decoded = json.loads("".join(part["text"]["content"] for part in rich))
    assert decoded == record
    assert hashlib.sha256(decoded["source"].encode()).hexdigest() == record["sha256"]
    assert payload["parent"] == {"type": "page_id", "page_id": ID}


@pytest.mark.parametrize("status", [429, 529])
def test_definite_rate_rejection_retries_identical_write(status):
    server = Server(error(status, headers={"Retry-After": "7"}), {})
    server.client().append(ID, {"id": "message"})
    assert server.sleeps == [7, 1 / 3]
    assert server.calls[0].data == server.calls[1].data


@pytest.mark.parametrize("failure", [error(500), error(503), URLError("secret_test"), TimeoutError("secret_test")])
def test_uncertain_write_never_retried(failure):
    server = Server(failure)
    with pytest.raises(NotionError) as caught:
        server.client().append(ID, {"message": "test"})
    assert not caught.value.safe_retry
    assert "secret_test" not in str(caught.value)
    assert len(server.calls) == 1


def test_get_retry_and_bounded_attempts():
    server = Server(*(error(503) for _ in range(4)))
    with pytest.raises(NotionError) as caught:
        server.client().whoami()
    assert caught.value.safe_retry
    assert len(server.calls) == 4


def test_blocked_is_never_retried():
    server = Server(error(429, {"code": "rate_limited", "additional_data": {"rate_limit_reason": "public_api_request_blocked"}}))
    with pytest.raises(NotionError) as caught:
        server.client().whoami()
    assert not caught.value.safe_retry
    assert len(server.calls) == 1


def test_parse_code_fences_and_nested_blocks_without_other_pages():
    nested_id = "11234567-89ab-cdef-0123-456789abcdef"
    server = Server({"results": [
        {"type": "code", "code": {"language": "json", "rich_text": [{"text": {"content": '{"id":'}}, {"plain_text": '"one"}'}]}},
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": '```json\n{"id":"two"}\n```'}]}},
        {"type": "toggle", "id": nested_id, "has_children": True},
        {"type": "child_page", "id": ID, "has_children": True},
    ]}, {"results": [{"type": "code", "code": {"language": "json", "rich_text": [{"plain_text": '{"id":"three"}'}]}}]})
    assert server.client().messages(ID) == [{"id": "one"}, {"id": "two"}, {"id": "three"}]
    assert len(server.calls) == 2


def test_oversize_is_rejected_before_any_write():
    server = Server()
    with pytest.raises(ValueError):
        server.client().append(ID, {"source": "a" * 64000})
    with pytest.raises(ValueError):
        server.client().create_page(ID, "lane", [{"a": 1}] * 101)
    with pytest.raises(ValueError):
        server.client().create_page(ID, "lane", [], text="é" * 300000)
    assert server.calls == []


def test_page_id_urls_and_external_url_rejection():
    assert page_id(ID) == ID
    assert page_id("https://app.notion.com/p/" + ID) == ID
    assert page_id("https://www.notion.so/Title-" + ID.replace("-", "") + "?a=1") == ID
    with pytest.raises(ValueError):
        page_id("https://example.com/" + ID)


def test_invalid_auth_is_local_and_errors_sanitized():
    with pytest.raises(ValueError):
        Notion("bad\ntoken")
    server = Server(error(401, {"code": "unauthorized", "message": "secret_test"}))
    with pytest.raises(NotionError) as caught:
        server.client().whoami()
    assert caught.value.status == 401
    assert "secret_test" not in str(caught.value)


def test_default_redirect_guard_never_forwards_auth_to_other_host():
    from fnv_relay.notion import _NotionRedirect
    from urllib.request import Request
    from urllib.error import HTTPError
    handler = _NotionRedirect()
    req = Request('https://api.notion.com/v1/users/me',headers={'Authorization':'Bearer fixture'})
    with pytest.raises(HTTPError):
        handler.redirect_request(req,None,302,'found',{},'https://other.invalid/collect')
