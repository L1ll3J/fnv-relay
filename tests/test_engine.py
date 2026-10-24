"""Offline protocol integration with durable real state and fake external services."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tarfile
import uuid
from types import SimpleNamespace

import pytest

from fnv_relay import engine as module
from fnv_relay.engine import Engine, PROTOCOL, sha
from fnv_relay.store import Store, UncertainOperation


class FakeNotion:
    def __init__(self):
        self.pages = {}
        self.appends = 0

    def create_page(self, parent, title, records, text=""):
        identifier = str(uuid.uuid4())
        self.pages[identifier] = deepcopy(records)
        return identifier

    def messages(self, identifier):
        return deepcopy(self.pages[identifier])

    def append(self, identifier, record):
        self.appends += 1
        self.pages[identifier].append(deepcopy(record))


class FakeProject:
    def __init__(self, now):
        self.now = now
        self.calls = []
        self.open = []
        self.claims = 0
        self.fail_submit = False

    def call(self, tool, args):
        self.calls.append((tool, deepcopy(args)))
        if tool == "status":
            return {"limits": {"claims": 4, "subsPerDay": 60, "pending": 25}, "open_claims": deepcopy(self.open), "submissions_today": 0, "awaiting_verification": 0}
        if tool == "list_bands":
            return {"bands": [{"band": "<=255", "allowed": True, "open": 10}]}
        if tool == "claim":
            self.claims += 1
            claim = {"claim_id": "claim-" + str(self.claims), "address": "0x401000", "size": 32, "expires_at": datetime.fromtimestamp(self.now + 7200, timezone.utc).isoformat(), "this_key": True}
            self.open.append(claim)
            return deepcopy(claim)
        if tool == "packet":
            return {"pack_url": "/fake-pack.tar.gz"}
        if tool == "submit":
            if self.fail_submit:
                raise RuntimeError("Ambiguous write failure")
            self.open = [row for row in self.open if row["claim_id"] != args["claim_id"]]
            return {"submission_id": "sub_fixture1", "state": "queued for verification (fixture)"}
        if tool == "giveup":
            self.open = [row for row in self.open if row["claim_id"] != args["claim_id"]]
            return {"released": True}
        raise AssertionError(tool)

    def download_pack(self, url, dest):
        files = {"pack.json": json.dumps({"address": "0x401000", "size": 32, "flags": []}), "source.cpp": "int target();\n", "types.h": "struct Example {};\n"}
        with tarfile.open(dest, "w:gz") as archive:
            for name, text in files.items():
                raw = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        return sha(Path(dest).read_bytes())


class FakeChecker:
    def __init__(self):
        self.calls = []
        self.wrong_plain_hash = False

    def __call__(self, config, pack, source, work, *, plain=False):
        raw = source.read_bytes()
        self.calls.append({"source": raw, "source_path": source, "work": work, "plain": plain})
        matched = b"return 42" in raw
        return {"source_sha256": "0" * 64 if plain and self.wrong_plain_hash else sha(raw), "status": "match" if matched else "diff", "exit_code": 0 if matched else 1, "standalone_match": matched and plain, "checker": {"ndiff_insn": 0 if matched else 4}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    store.set_config({"schema": 1, "paths": {}})
    notion = FakeNotion()
    now = 1_800_000_000
    project, checker = FakeProject(now), FakeChecker()

    def export(config, pack, target):
        target.mkdir(exist_ok=True)
        path = target / "original-context.txt"
        path.write_text("original context")
        return path

    monkeypatch.setattr(module.context, "export", export)
    engine = Engine(store, project, notion, checker=checker, clock=lambda: now, watchdog=False)
    lane = engine.create_lane(str(uuid.uuid4()), {"order": "smallest"})
    result = SimpleNamespace(store=store, notion=notion, project=project, checker=checker, engine=engine, route=lane["route_token"])
    yield result
    store.close()


def lane(env):
    return env.store.lane(env.route)


def message(env, kind, mid, **extra):
    current = lane(env)
    scope = {key: current["lease"][key] for key in ("claim_id", "pack_sha256")} if current.get("lease") else {}
    return {"protocol": PROTOCOL, "sender": "chatgpt", "route_token": env.route, "kind": kind, "message_id": mid, **scope, **extra}


def assign(env):
    env.notion.append(lane(env)["inbox"], message(env, "HELLO", "hello"))
    env.engine.poll()
    assert lane(env)["state"] == "assigned"
    return lane(env)


def candidate(env, cid, text, *, file_override=None):
    current = lane(env)
    raw = text.encode()
    file = {"filename": "candidate.cpp", "source_utf8": text, "source_sha256": sha(raw), "source_bytes": len(raw)}
    if file_override:
        file.update(file_override)
    row = message(env, "CANDIDATE", "unused-" + cid, candidate_id=cid, files=[file])
    page = env.notion.create_page(current["data"], cid, [row])
    return {"candidate_id": cid, "page": page}


def post_batch(env, pointers, mid="batch-1"):
    msg = message(env, "BATCH", mid, candidates=pointers)
    env.notion.append(lane(env)["inbox"], msg)
    return msg


def test_hello_context_multicandidate_match_plain_submit_and_next_claim(env):
    first = assign(env)
    records = env.notion.messages(first["outbox"])
    assignment = next(row["payload"] for row in records if row.get("payload", {}).get("status") == "ASSIGNED")
    assert assignment["scope"]["claim_id"] == "claim-1"
    assert assignment["pages"]
    assert any(env.notion.messages(ref["page"])[0]["source_utf8"] == "original context" for ref in assignment["pages"])
    post_batch(env, [candidate(env, "try-1", "int target(){return 41;}"), candidate(env, "try-2", "int target(){return 42;}")])
    env.engine.poll()
    submits = [args for tool, args in env.project.calls if tool == "submit"]
    assert submits == [{"claim_id": "claim-1", "source": "int target(){return 42;}", "model":"ChatGPT Web via Notion"}]
    assert [row["plain"] for row in env.checker.calls] == [False, False, True]
    assert env.checker.calls[-1]["source_path"] != env.checker.calls[-2]["source_path"]
    assert env.checker.calls[-1]["work"] != env.checker.calls[-2]["work"]
    assert lane(env)["lease"]["claim_id"] == "claim-2"
    results = [row["payload"] for row in env.notion.messages(first["outbox"]) if "payload" in row]
    assert any(row["status"] == "SUBMITTED_AWAITING_VERIFICATION" and row["verified"] is False for row in results)
    batch = next(row for row in results if row["status"] == "BATCH_RESULT")
    assert len(batch["results"]) == 2


def test_stale_message_does_not_block_current_batch(env):
    assign(env)
    env.notion.append(lane(env)["inbox"], message(env, "BATCH", "old-batch", claim_id="old-claim", candidates=[]))
    post_batch(env, [candidate(env, "current", "int target(){return 41;}")])
    env.engine.poll()
    assert len(env.checker.calls) == 1
    assert not env.store.get("last_error:" + env.route)


def test_wrong_independent_source_proof_never_submits_or_replays(env):
    assign(env)
    env.checker.wrong_plain_hash = True
    post_batch(env, [candidate(env, "match", "int target(){return 42;}")])
    env.engine.poll()
    assert not any(tool == "submit" for tool, _ in env.project.calls)
    assert len(env.checker.calls) == 2
    env.engine.poll()
    assert len(env.checker.calls) == 2
    assert not any(tool == "submit" for tool, _ in env.project.calls)


def test_duplicate_checked_source_reuses_result_without_checker_replay(env):
    assign(env)
    pointer = candidate(env, "first", "int target(){return 41;}")
    post_batch(env, [pointer])
    env.engine.poll()
    post_batch(env, [pointer], "batch-2")
    env.engine.poll()
    assert len(env.checker.calls) == 1
    result = [row["payload"] for row in env.notion.messages(lane(env)["outbox"]) if row.get("payload", {}).get("status") == "BATCH_RESULT"][-1]
    assert result["results"][0]["reused_prior_result"] is True


def test_incomplete_check_reservation_is_not_replayed(env):
    first = assign(env)
    text = "int target(){return 41;}"
    env.store.set("check:claim-1:" + sha(text.encode()), {"state": "reserved"})
    post_batch(env, [candidate(env, "reserved", text)])
    env.engine.poll()
    env.engine.poll()
    assert env.checker.calls == []
    assert lane(env)["lease"]["claim_id"] == first["lease"]["claim_id"]


def test_checked_best_is_preserved_on_release(env):
    first = assign(env)
    text = "int target(){return 41;}"
    post_batch(env, [candidate(env, "best", text)])
    env.engine.poll()
    current = lane(env)
    env.engine.release(current, "Unfinished; instruction difference remains")
    gives = [args for tool, args in env.project.calls if tool == "giveup"]
    assert len(gives) == 1
    assert gives[0]["best_code"] == text
    assert gives[0]["claim_id"] == first["lease"]["claim_id"]


def test_uncertain_submit_does_not_retry_or_give_up(env):
    assign(env)
    env.project.fail_submit = True
    post_batch(env, [candidate(env, "match", "int target(){return 42;}")])
    env.engine.poll()
    env.engine.poll()
    assert len([1 for tool, _ in env.project.calls if tool == "submit"]) == 1
    assert env.store.operation_record("submit:claim-1")["status"] == "uncertain"
    with pytest.raises(RuntimeError):
        env.engine.release(lane(env), "do not release ambiguous submission")
    assert not any(tool == "giveup" for tool, _ in env.project.calls)


def test_uncertain_mailbox_write_is_not_resent(env):
    assign(env)
    current = lane(env)
    with pytest.raises(RuntimeError):
        env.store.operation("notion-append:unclear", {}, lambda: (_ for _ in ()).throw(RuntimeError()))
    count = env.notion.appends
    with pytest.raises(UncertainOperation):
        env.engine.publish(current, "unclear", {"status": "DIAGNOSTIC"})
    assert env.notion.appends == count


def test_patch_base_traversal_rejected_before_reading_or_checking(env):
    assign(env)
    pointer = candidate(env, "malicious", "int target(){return 41;}", file_override={"base_source_sha256": "../../outside", "literal_edits": [{"before_utf8": "a", "after_utf8": "b"}]})
    with pytest.raises(ValueError, match="Invalid base source digest"):
        env.engine.candidate(lane(env), pointer)
    assert env.checker.calls == []


def test_duplicate_batch_rejected_before_any_compilation(env):
    assign(env)
    pointer = candidate(env, "repeat", "int target(){return 41;}")
    post_batch(env, [pointer, pointer])
    env.engine.poll()
    assert env.checker.calls == []
    assert not any(tool == "submit" for tool, _ in env.project.calls)


def test_uncertain_claim_intent_never_allocates_again_on_next_poll(env, monkeypatch):
    original = env.project.call

    def uncertain(tool, args):
        if tool == "claim":
            original(tool, args)  # The server may have committed the claim.
            raise RuntimeError("Claim response lost")
        return original(tool, args)

    monkeypatch.setattr(env.project, "call", uncertain)
    env.notion.append(lane(env)["inbox"], message(env, "HELLO", "hello"))
    env.engine.poll()
    env.engine.poll()
    assert len([1 for tool, _ in env.project.calls if tool == "claim"]) == 1


def test_malformed_old_message_does_not_starve_registration(env):
    inbox = lane(env)['inbox']
    env.notion.append(inbox, message(env, 'BATCH', '../bad', claim_id='old', pack_sha256='old'))
    assign(env)
    assert env.project.claims == 1
    assert lane(env)['state'] == 'assigned'


def test_changed_prior_message_is_rejected_without_starving_new_round(env):
    current = assign(env)
    original = next(row for row in env.notion.pages[current['inbox']] if row.get('kind') == 'HELLO')
    altered = deepcopy(original); altered['note'] = 'same ID changed after acceptance'
    env.notion.append(current['inbox'],altered)
    env.notion.append(current['inbox'],message(env,'FINDING','new-finding',note='new useful observation'))
    env.engine.poll()
    assert env.store.get('message:'+env.route+':new-finding') is not None
    assert env.store.get('message:'+env.route+':hello') == sha(module.canonical(original))
