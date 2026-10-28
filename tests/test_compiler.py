"""Offline checker protocol and evidence tests. No Wine/compiler/API execution."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from fnv_relay import compiler


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    cfg = {}
    for name in ("game_exe", "function_index", "symbols"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        cfg[name] = str(path)
    for name in ("checker_dir", "compiler_dir"):
        path = tmp_path / name
        path.mkdir()
        cfg[name] = str(path)
    (Path(cfg["checker_dir"]) / "fnvcheck.py").write_text("# trusted fixture\n")
    root = Path(cfg["compiler_dir"])
    for sub in ("VC/bin", "VC/include", "VC/lib", "SDK/Include", "SDK/Lib"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "VC/bin/cl.exe").write_bytes(b"compiler-fixture")
    prefix = tmp_path / "private-prefix"
    prefix.mkdir()
    (prefix / compiler._MARKER).write_text("dedicated fixture")
    cfg["wine_prefix"] = str(prefix)
    server = tmp_path / "server"
    server.mkdir()
    monkeypatch.setattr(compiler, "_wine_servers", lambda _: [server])
    pack = tmp_path / "original-pack"
    pack.mkdir()
    (pack / "pack.json").write_text(json.dumps({"flags": ["/Od", "/Oy-"]}))
    (pack / "header.h").write_text("int f();\n")
    source = tmp_path / "input.cpp"
    source.write_bytes(b"int f(){ return 1; }\n")
    return cfg, pack, source, tmp_path / "check"


def executor(monkeypatch, *, code=0, stdout=b'{"status":"match","ndiff_insn":0}', stderr=b"", mutate=None, ready=True, timeout=False):
    calls = []

    def run(argv, env, seconds, cwd):
        calls.append((argv, env, seconds, cwd))
        if ready:
            (cwd / "sandbox-ready.json").write_text(json.dumps({"landlock_abi": 3, "network_namespace": True}))
        if mutate:
            mutate(cwd)
        return code, stdout, stderr, timeout

    monkeypatch.setattr(compiler, "_run", run)
    return calls


def test_diagnostic_protocol_and_evidence(inputs, monkeypatch):
    cfg, pack, source, work = inputs
    calls = executor(monkeypatch, stderr=b"wine:\xff\n")
    result = compiler.check(cfg, pack, source, work)
    assert result["status"] == "match"
    assert result["standalone_match"] is False
    assert result["checker"]["ndiff_insn"] == 0
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert Path(result["stderr_path"]).read_bytes() == b"wine:\xff\n"
    argv, env, timeout, _ = calls[0]
    assert "--net" in argv and "--kill-child=KILL" in argv
    assert env["FNVCHECK_TREE"] == str(work / "pack")
    assert env["FNVCHECK_IMAGE"] == cfg["game_exe"]
    assert "OPENAI_API_KEY" not in env
    assert timeout == 210
    assert (work / "pack/pack.json").read_bytes() == (pack / "pack.json").read_bytes()
    assert (work / "candidate.cpp").stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("stdout,code,match", [(b"MATCH\n", 0, True), (b"MATCH\n", 1, False), (b"NO MATCH\n", 0, False), (b"MATCH: extra\n", 0, False), (b'{"status":"match"}\n', 0, False)])
def test_independent_plain_requires_exact_match_line(inputs, monkeypatch, stdout, code, match):
    cfg, pack, source, work = inputs
    executor(monkeypatch, stdout=stdout, code=code)
    result = compiler.check(cfg, pack, source, work, plain=True)
    assert result["standalone_match"] is match
    assert (result["status"] == "match") is match
    payload = json.loads((work / "sandbox.json").read_text())
    assert payload["plain"] is True


@pytest.mark.parametrize("stdout,code", [(b'{"status":"match"}', 1), (b'MATCH\n', 0), (b'[]', 0), (b'{"status":null}', 0)])
def test_diagnostic_cannot_claim_match_on_bad_protocol(inputs, monkeypatch, stdout, code):
    cfg, pack, source, work = inputs
    executor(monkeypatch, stdout=stdout, code=code)
    result = compiler.check(cfg, pack, source, work)
    assert result["status"] != "match"
    assert result["standalone_match"] is False


@pytest.mark.parametrize("target", ["source", "pack", "checker", "compiler"])
def test_mutated_bound_input_invalidates_match(inputs, monkeypatch, target):
    cfg, pack, source, work = inputs

    def mutate(cwd):
        path = {"source": cwd / "candidate.cpp", "pack": cwd / "pack/header.h", "checker": Path(cfg["checker_dir"]) / "fnvcheck.py", "compiler": Path(cfg["compiler_dir"]) / "VC/bin/cl.exe"}[target]
        path.chmod(0o600)
        path.write_bytes(b"changed")

    executor(monkeypatch, mutate=mutate)
    result = compiler.check(cfg, pack, source, work)
    assert result["status"] == "infrastructure_error"
    assert "mutation" in result["checker"]["errors"][0]


def test_failed_sandbox_is_fail_closed(inputs, monkeypatch):
    cfg, pack, source, work = inputs
    executor(monkeypatch, ready=False, stdout=b'{"status":"match"}')
    result = compiler.check(cfg, pack, source, work)
    assert result["status"] == "infrastructure_error"
    assert "Sandbox unavailable" in result["checker"]["errors"][0]


def test_timeout_logs_preserved(inputs, monkeypatch):
    cfg, pack, source, work = inputs
    executor(monkeypatch, code=-signal.SIGKILL, stdout=b"partial\x00", stderr=b"error\xff", timeout=True)
    result = compiler.check(cfg, pack, source, work)
    assert result["status"] == "infrastructure_error"
    assert result["timed_out"] is True
    assert Path(result["log_path"]).read_bytes() == b"partial\x00"


@pytest.mark.parametrize("size", [0, 1048577])
def test_source_limit(inputs, monkeypatch, size):
    cfg, pack, source, work = inputs
    source.write_bytes(b"x" * size)
    calls = executor(monkeypatch)
    with pytest.raises(compiler.CompilerError, match="1..1048576"):
        compiler.check(cfg, pack, source, work)
    assert calls == []


def test_pack_symlink_rejected(inputs, monkeypatch):
    cfg, pack, source, work = inputs
    (pack / "escape").symlink_to(source)
    calls = executor(monkeypatch)
    with pytest.raises(compiler.CompilerError, match="Non-regular"):
        compiler.check(cfg, pack, source, work)
    assert calls == []


def test_existing_unmarked_prefix_refused(inputs):
    cfg, _, _, _ = inputs
    (Path(cfg["wine_prefix"]) / compiler._MARKER).unlink()
    with pytest.raises(compiler.CompilerError, match="unmarked"):
        compiler.prepare(cfg)


def test_fresh_checks_do_not_reuse_artifacts(inputs, monkeypatch):
    cfg, pack, source, work = inputs
    executor(monkeypatch)
    compiler.check(cfg, pack, source, work)
    with pytest.raises(compiler.CompilerError, match="empty"):
        compiler.check(cfg, pack, source, work)


def test_run_kills_process_group_on_timeout(tmp_path):
    code, out, err, timeout = compiler._run([sys.executable, "-c", "import time; print('before', flush=True); time.sleep(30)"], {"PATH": "/usr/bin:/bin"}, 0.1, tmp_path)
    assert timeout is True
    assert code == -signal.SIGKILL
    assert out == b"before\n"
    assert err == b""


def test_server_path_derived_from_uid_and_inode(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 4321)
    st = tmp_path.stat()
    name = f"server-{st.st_dev:x}-{st.st_ino:x}"
    assert compiler._wine_servers(tmp_path) == [Path("/run/user/4321/wine") / name, Path("/tmp/.wine-4321") / name]


def test_kernel_sandbox_denies_unlisted_file(inputs):
    """Real namespaces/Landlock with a fake checker; no Wine or compiler runs."""
    cfg, pack, source, work = inputs
    outside = work.parent / "unlisted-secret"
    outside.write_text("private fixture")
    script = Path(cfg["checker_dir"]) / "fnvcheck.py"
    script.write_text("import json,sys\nif '--check-image' in sys.argv:\n    print('IMAGE OK: fixture'); raise SystemExit(0)\ntry:\n    open(" + repr(str(outside)) + ").read()\nexcept PermissionError:\n    print(json.dumps({'status':'match'}))\nelse:\n    print(json.dumps({'status':'isolation_failed'}))\n")
    result = compiler.check(cfg, pack, source, work)
    if result["status"] == "infrastructure_error" and "Sandbox unavailable" in str(result["checker"]):
        stderr = Path(result["stderr_path"]).read_text()
        if "Operation not permitted" in stderr or "Landlock ABI 3+ required" in stderr:
            pytest.skip("Host lacks required namespaces/Landlock")
    assert result["status"] == "match", Path(result["stderr_path"]).read_text()
    assert result["standalone_match"] is False
    assert outside.read_text() == "private fixture"


@pytest.mark.parametrize("maximum", [False, 0, -1, 16 * 1024 * 1024 + 1, "100"])
def test_invalid_configured_source_limit(inputs, maximum):
    cfg, pack, source, work = inputs
    cfg["max_candidate_bytes"] = maximum
    with pytest.raises(compiler.CompilerError, match="max_candidate_bytes"):
        compiler.check(cfg, pack, source, work)


def test_configured_source_limit_is_applied(inputs, monkeypatch):
    cfg, pack, source, work = inputs
    cfg["max_candidate_bytes"] = 4
    calls = executor(monkeypatch)
    with pytest.raises(compiler.CompilerError, match="1..4"):
        compiler.check(cfg, pack, source, work)
    assert calls == []


def test_cancellation_reaps_owned_child_and_retains_logs(tmp_path, monkeypatch):
    import sys
    from fnv_relay import compiler
    real_popen = compiler.subprocess.Popen
    children = []

    class InterruptedProcess:
        def __init__(self, *args, **kwargs):
            self.child = real_popen(*args, **kwargs)
            self.pid = self.child.pid
            self.first_wait = True
            children.append(self.child)

        def wait(self, *args, **kwargs):
            if self.first_wait:
                self.first_wait = False
                raise KeyboardInterrupt()
            return self.child.wait(*args, **kwargs)

        @property
        def returncode(self):
            return self.child.returncode

    monkeypatch.setattr(compiler.subprocess, 'Popen', InterruptedProcess)
    with pytest.raises(KeyboardInterrupt):
        compiler._run([sys.executable,'-c','import time; time.sleep(60)'], {}, 90, tmp_path)
    assert len(children) == 1 and children[0].poll() is not None
    assert (tmp_path/'interrupted.stdout.log').exists()
    assert (tmp_path/'interrupted.stderr.log').exists()


def test_new_official_accepted_flags_output_requires_pack_permission():
    from fnv_relay.compiler import _interpret
    pack = {'flags':['/O2'], 'accept_also':[['/Od','/Ob1']]}
    line = b'MATCH  (under /Od /Ob1, a flag set this function also accepts: submit it as it is)\n'
    matched, result = _interpret(0, line, plain=True, pack=pack)
    assert matched and result['accepted_flags'] == ['/Od','/Ob1']
    assert not _interpret(0, line, plain=True, pack={'flags':['/O2']})[0]
    assert not _interpret(0, b'LITERAL_MISMATCH\n', plain=True, pack=pack)[0]
    assert not _interpret(0, b"MATCH  (under your --flags, not the pack's /O2: the site does not accept this yet.)\n", plain=True, pack=pack)[0]
    raw = b'{"status":"match","accepted_flags":["/Od","/Ob1"]}'
    assert _interpret(0, raw, plain=False, pack=pack)[1]['accepted_flags'] == ['/Od','/Ob1']
    assert not _interpret(0, raw, plain=False, pack={'flags':['/O2']})[0]


RTM_SUFFIX = "  (compiled with Visual C++ 2008 RTM, cl 15.00.21022.08, the compiler the game was built with: submit it as it is)"


def test_rtm_plain_protocol_is_exact_and_pack_bound():
    pack = {"flags":["/O2"], "accept_also":[["/Od"]]}
    accepted = "MATCH  (under /Od, a flag set this function also accepts: submit it as it is)"
    for line in ("MATCH", accepted):
        assert compiler._interpret(0,(line+RTM_SUFFIX).encode(),plain=True,pack=pack)[0]
        assert not compiler._interpret(1,(line+RTM_SUFFIX).encode(),plain=True,pack=pack)[0]
        assert not compiler._interpret(0,(line+RTM_SUFFIX+" extra").encode(),plain=True,pack=pack)[0]
    assert not compiler._interpret(0,(accepted+RTM_SUFFIX).encode(),plain=True,pack={})[0]
    proof = "MATCH"+RTM_SUFFIX+"  (under your --flags, not the pack's /O2: the site does not accept this yet.)"
    assert not compiler._interpret(0,proof.encode(),plain=True,pack=pack)[0]


def test_rtm_config_environment_provenance_and_protected_prefix(inputs, monkeypatch):
    from test_toolchains import compiler as fixture_compiler
    cfg, pack, source, work = inputs
    sp1 = fixture_compiler(work.parent, "sp1")
    rtm = fixture_compiler(work.parent, "rtm")
    for root in (sp1, rtm):
        for sub in ("VC/include", "VC/lib", "SDK/Include", "SDK/Lib"):
            (root/sub).mkdir(parents=True)
    cfg.update(compiler_dir=str(sp1), rtm_compiler_dir=str(rtm))
    calls=executor(monkeypatch,stdout=("MATCH"+RTM_SUFFIX).encode())
    result=compiler.check(cfg,pack,source,work,plain=True)
    assert result["standalone_match"]
    assert calls[0][1]["FNVCHECK_RTM_ROOT"] == str(rtm)
    before=compiler._versions(compiler._config(cfg))
    (rtm/"VC/bin/c2.dll").write_bytes(b"changed")
    assert compiler._versions(compiler._config(cfg)) != before
    cfg["wine_prefix"] = str(rtm/"prefix")
    with pytest.raises(compiler.CompilerError,match="protected input"):
        compiler._config(cfg)
