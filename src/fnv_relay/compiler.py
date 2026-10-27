"""Official fnvcheck bridge. Candidate text is compiled, never executed.

Linux user/network/PID namespaces and Landlock ABI 3+ are mandatory. No
unsandboxed fallback exists. Only owner-configured checker/toolchain code runs.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


class CompilerError(RuntimeError):
    """Configuration or isolation failure; no successful check may be claimed."""


_MARKER = ".fnv-relay-dedicated-prefix"
_READ = (1 << 0) | (1 << 2) | (1 << 3)
_ALL = (1 << 15) - 1


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree(path: Path) -> str:
    """Bind names and contents; reject links/devices rather than following them."""
    rows = []
    for item in sorted(path.rglob("*")):
        mode = item.lstat().st_mode
        if stat.S_ISDIR(mode):
            rows.append([item.relative_to(path).as_posix() + "/", "directory"])
        elif stat.S_ISREG(mode):
            rows.append([item.relative_to(path).as_posix(), _sha(item)])
        else:
            raise CompilerError(f"Non-regular input in {path}: {item.relative_to(path)}")
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _config(config: dict) -> dict:
    result = {}
    for name in ("game_exe", "checker_dir", "compiler_dir", "function_index", "symbols", "wine_prefix"):
        if not config.get(name):
            raise CompilerError(f"Missing compiler configuration: {name}")
        result[name] = str(Path(config[name]).expanduser().absolute())
    for name in ("game_exe", "function_index", "symbols"):
        if not Path(result[name]).is_file():
            raise CompilerError(f"Missing input: {name}")
    for name, relative in (("checker_dir", "fnvcheck.py"), ("compiler_dir", "VC/bin/cl.exe")):
        if not (Path(result[name]) / relative).is_file():
            raise CompilerError(f"Missing {name}/{relative}")
    for relative in ("VC/include", "VC/lib", "SDK/Include", "SDK/Lib"):
        if not (Path(result["compiler_dir"]) / relative).is_dir():
            raise CompilerError(f"Missing compiler_dir/{relative}")
    python = Path(config.get("python", sys.executable)).expanduser().absolute()
    if not python.is_file():
        raise CompilerError("Configured Python executable does not exist")
    result["python"] = str(python)
    result["check_timeout_seconds"] = float(config.get("check_timeout_seconds", 210))
    if not 0 < result["check_timeout_seconds"] <= 3600:
        raise CompilerError("check_timeout_seconds must be positive and at most 3600")
    maximum = config.get("max_candidate_bytes", 1048576)
    if type(maximum) is not int or not 0 < maximum <= 16 * 1024 * 1024:
        raise CompilerError("max_candidate_bytes must be a positive integer at most 16 MiB")
    result["max_candidate_bytes"] = maximum
    if config.get("compiler_tips"):
        result["compiler_tips"] = str(Path(config["compiler_tips"]).expanduser().resolve(strict=True))
    prefix = Path(result["wine_prefix"])
    # A dedicated prefix must never grant write access to input trees or HOME.
    for protected in [Path.home(), Path(result["python"]).parent.parent] + [Path(result[k]) for k in ("game_exe", "checker_dir", "compiler_dir", "function_index", "symbols")]:
        if prefix.resolve() == protected.resolve() or prefix.resolve() in protected.resolve().parents:
            raise CompilerError("wine_prefix overlaps a protected input or HOME")
    for name in ("checker_dir", "compiler_dir"):
        protected = Path(result[name]).resolve()
        if protected in prefix.resolve().parents:
            raise CompilerError("wine_prefix is inside a protected input tree")
    return result


def _env(config: dict, work: Path) -> dict:
    root = config["compiler_dir"]
    win = lambda p: "Z:" + str(p).replace("/", "\\")
    return {
        "PATH": "/usr/bin:/bin", "HOME": str(work / "home"),
        "WINEPREFIX": config["wine_prefix"], "WINEARCH": "win32", "WINEDEBUG": "-all",
        "WINEPATH": win(Path(root) / "VC/bin"),
        "INCLUDE": ";".join(win(Path(root) / p) for p in ("VC/include", "SDK/Include")),
        "LIB": ";".join(win(Path(root) / p) for p in ("VC/lib", "SDK/Lib")),
        "TMPDIR": str(work / "tmp"), "XDG_RUNTIME_DIR": str(work / "runtime"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "FNVCHECK_TREE": str(work / "pack"), "FNVCHECK_CACHE": str(work / "cache"),
        "FNVCHECK_TOOLS": config["checker_dir"], "FNVCHECK_INDEX": config["function_index"],
        "FNVCHECK_SYMBOLS": config["symbols"], "FNVCHECK_IMAGE": config["game_exe"],
        "FNV_MSVC9_ROOT": root, "FNV_MSVC9_PREFIX": config["wine_prefix"],
    }


def _run(argv: list[str], env: dict, timeout: float, cwd: Path) -> tuple[int, bytes, bytes, bool]:
    # File-backed capture avoids an unbounded pipe buffer in the controller.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=errors, start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        except BaseException:
            # The compiler is our child process group; never leave it orphaned
            # when cancellation interrupts the wait. Retain available diagnostics.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            output.seek(0)
            errors.seek(0)
            (cwd / "interrupted.stdout.log").write_bytes(output.read())
            (cwd / "interrupted.stderr.log").write_bytes(errors.read())
            raise
        output.seek(0)
        errors.seek(0)
        return proc.returncode, output.read(), errors.read(), timed_out



def prepare(config: dict) -> dict:
    """Initialize only a new/previously marked dedicated Wine prefix.

    This runs trusted Wine initialization, with no candidate or checker. Existing
    unmarked prefixes are refused, including all live research prefixes. Wine may
    stop its idle server; the checker can start it inside the sandbox later.
    """
    cfg = _config(config)
    prefix = Path(cfg["wine_prefix"])
    if prefix.exists() and not (prefix / _MARKER).is_file():
        raise CompilerError("Refusing existing unmarked Wine prefix; choose a NEW dedicated directory")
    prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
    (prefix / _MARKER).write_text("fnv-relay dedicated CLI prefix v1\n")
    state = prefix / ".relay-host"
    for relative in ("home", "tmp", "runtime"):
        (state / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    env = _env(cfg, state)
    for command in (["/usr/bin/wineboot", "-i"], ["/usr/bin/wine", "cmd", "/c", "exit"], ["/usr/bin/wineserver", "-p"]):
        code, out, err, timeout = _run(command, env, 90, state)
        # The official msvc9 wrapper accepts 2: Wine's idle server has exited.
        accepted = (0, 2) if command[0] == "/usr/bin/wineserver" else (0,)
        if code not in accepted or timeout:
            (state / "prepare.stdout.log").write_bytes(out)
            (state / "prepare.stderr.log").write_bytes(err)
            raise CompilerError(f"Dedicated Wine initialization failed ({Path(command[0]).name}: {code}); see prefix/.relay-host logs")
    servers = _wine_servers(prefix)
    if not any(p.is_dir() for p in servers):
        raise CompilerError("Wine initialized but no prefix-specific server socket directory found")
    return {"wine_prefix": str(prefix), "ready": True, "server_directories": [str(p) for p in servers if p.is_dir()]}


def _wine_servers(prefix: Path) -> list[Path]:
    info = prefix.stat()
    name = f"server-{info.st_dev:x}-{info.st_ino:x}"
    uid = os.getuid()
    return [Path(f"/run/user/{uid}/wine") / name, Path(f"/tmp/.wine-{uid}") / name]


def _versions(cfg: dict) -> dict:
    result = {"controller_python": sys.version.split()[0], "platform": platform.platform(),
              "backend_sha256": _sha(Path(__file__)),
              "checker_python_sha256": _sha(Path(cfg["python"]).resolve())}
    result["evidence_backend_sha256"] = _sha(Path(__file__).with_name("candidate_evidence.py"))
    for tool in ("wine", "wineserver", "wineboot", "unshare"):
        path = shutil.which(tool, path="/usr/bin:/bin")
        result[tool + "_sha256"] = _sha(Path(path).resolve()) if path else None
    for key in ("game_exe", "function_index", "symbols", "compiler_tips"):
        if key in cfg:
            result[key + "_sha256"] = _sha(Path(cfg[key]))
    for key in ("checker_dir", "compiler_dir"):
        result[key + "_sha256"] = _tree(Path(cfg[key]))
    return result


def _interpret(code: int, stdout: bytes, *, plain: bool, pack=None) -> tuple[bool, dict]:
    if plain:
        lines = stdout.decode("utf-8", "replace").splitlines()
        first = lines[0] if lines else ""
        allowed = (pack or {}).get("accept_also") or []
        accepted = next((flags for flags in allowed if first == "MATCH  (under %s, a flag set this function also accepts: submit it as it is)" % " ".join(flags)), None)
        match = code == 0 and (first == "MATCH" or accepted is not None)
        return match, {"status": "match" if match else "not_match", "mode": "plain", "accepted_flags": accepted or (pack or {}).get("flags")}
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeDecodeError):
        return False, {"status": "infrastructure_error", "errors": ["Official checker did not emit valid JSON"]}
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return False, {"status": "infrastructure_error", "errors": ["Unknown official checker JSON shape"]}
    # Preserve only the official public diagnostic fields.
    checker = {k: value[k] for k in ("status", "errors", "diffs", "ndiff_insn", "extent", "diff", "accepted_flags") if k in value}
    if "accepted_flags" in checker and checker["accepted_flags"] not in ((pack or {}).get("accept_also") or []):
        return False, {"status": "infrastructure_error", "errors": ["Checker reported a flag set absent from the original pack"]}
    return code == 0 and checker["status"] == "match", checker


def check(config: dict, pack_dir: Path, candidate: Path, work: Path, *, plain: bool = False) -> dict:
    """Run exactly one official invocation in a fresh workspace; no model loop."""
    cfg = _config(config)
    prefix = Path(cfg["wine_prefix"])
    if not (prefix / _MARKER).is_file() or not any(p.is_dir() for p in _wine_servers(prefix)):
        raise CompilerError("Dedicated Wine prefix is not ready; run doctor --prepare-wine")
    work = Path(work).resolve()
    pack_dir = Path(pack_dir).resolve(strict=True)
    candidate = Path(candidate)
    with candidate.open("rb") as stream:
        raw = stream.read(cfg["max_candidate_bytes"] + 1)
    if not 0 < len(raw) <= cfg["max_candidate_bytes"]:
        raise CompilerError(f"Candidate must contain 1..{cfg['max_candidate_bytes']} bytes")
    if work == pack_dir or pack_dir in work.parents or work in pack_dir.parents:
        raise CompilerError("Check work directory must be separate from input pack")
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(work.iterdir()):
        raise CompilerError("Check work directory must be empty")
    pack_hash = _tree(pack_dir)
    versions = _versions(cfg)
    shutil.copytree(pack_dir, work / "pack")
    if _tree(work / "pack") != pack_hash:
        raise CompilerError("Pack changed while copying")
    snapshot = work / "candidate.cpp"
    snapshot.write_bytes(raw)
    snapshot.chmod(0o400)
    source_hash = _sha(snapshot)
    for name in ("home", "tmp", "runtime", "cache"):
        (work / name).mkdir(mode=0o700)
    payload = work / "sandbox.json"
    payload.write_text(json.dumps({"config": cfg, "work": str(work), "plain": plain,
                                   "parent_namespaces": {name: os.readlink(f"/proc/self/ns/{name}")
                                                         for name in ("user", "net", "pid")}}))
    payload.chmod(0o400)
    unshare = shutil.which("unshare", path="/usr/bin:/bin")
    if not unshare:
        raise CompilerError("Linux unshare is required; unsandboxed compilation is disabled")
    argv = [unshare, "--user", "--map-current-user", "--net", "--pid", "--fork", "--kill-child=KILL",
            cfg["python"], "-I", "-B", str(Path(__file__).resolve()), "--sandbox-child", str(payload)]
    code, stdout, stderr, timed_out = _run(argv, _env(cfg, work), cfg["check_timeout_seconds"], work)
    log = work / "stdout.log"
    log.write_bytes(stdout)
    (work / "stderr.log").write_bytes(stderr)
    unchanged = (versions == _versions(cfg) and _sha(snapshot) == source_hash
                 and _tree(pack_dir) == pack_hash and _tree(work / "pack") == pack_hash)
    match, checker = _interpret(code, stdout, plain=plain, pack=json.loads((work / "pack/pack.json").read_text()))
    # Only a completed attestation proves that setup reached exec.
    try:
        ready = json.loads((work / "sandbox-ready.json").read_text())
        sandbox_ready = ready.get("landlock_abi", 0) >= 3 and ready.get("network_namespace") is True
    except (OSError, ValueError, AttributeError, TypeError):
        sandbox_ready = False
    if timed_out or not unchanged or not sandbox_ready or code == 125:
        match = False
        checker = {"status": "infrastructure_error", "errors": [
            "Checker timeout" if timed_out else "Input/component mutation detected" if not unchanged else
            "Sandbox unavailable; requires Linux user/network/PID namespaces and Landlock ABI 3+. See stderr.log"]}
    result = {"status": "match" if match else checker.get("status", "not_match"),
              "exit_code": code, "standalone_match": bool(match and plain),
              "source_sha256": source_hash, "source_path": str(snapshot), "pack_sha256": pack_hash,
              "log_path": str(log), "log_sha256": _sha(log),
              "stderr_path": str(work / "stderr.log"), "stderr_sha256": _sha(work / "stderr.log"),
              "checker": checker, "versions": versions, "timed_out": timed_out}
    if result["status"] == "match" and not match:
        result["status"] = "infrastructure_error"
    if not plain:
        from .candidate_evidence import collect
        result['diagnostics'], result['diagnostic_artifacts'] = collect(work, source_hash)
        if not unchanged or not sandbox_ready or timed_out:
            result['diagnostics'] = {'status': 'unavailable', 'reason_code': 'check_integrity_unproven',
                                     'additional_compiler_invocations': 0}
            result['diagnostic_artifacts'] = []
    image_log = work / "cache/image-check.stdout.log"
    if image_log.is_file():
        result["image_log_path"] = str(image_log)
        result["image_log_sha256"] = _sha(image_log)
    (work / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def _landlock(read_paths: list[Path], write_paths: list[Path], directory_paths: list[Path] | None = None) -> int:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise CompilerError("Linux x86_64/aarch64 Landlock required")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise CompilerError("Landlock ABI 3+ required; unsandboxed compilation is disabled")

    class Ruleset(ctypes.Structure):
        _fields_ = [("access", ctypes.c_uint64)]

    class Rule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    rules = Ruleset(_ALL)
    fd = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    if fd < 0:
        raise CompilerError("Cannot create Landlock ruleset")
    try:
        for paths, access in ((read_paths, _READ), (write_paths, _ALL), (directory_paths or [], 1 << 3)):
            for path in paths:
                if not path.exists():
                    continue
                op = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    allowed = access if path.is_dir() else access & ((1 << 0) | (1 << 1) | (1 << 2) | (1 << 14))
                    rule = Rule(allowed, op)
                    if libc.syscall(445, fd, 1, ctypes.byref(rule), 0):
                        raise CompilerError(f"Cannot add Landlock rule: {path}")
                finally:
                    os.close(op)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
            raise CompilerError("Cannot enforce Landlock")
    finally:
        os.close(fd)
    return abi


def _child(payload: Path) -> None:
    data = json.loads(payload.read_text())
    cfg, work = data["config"], Path(data["work"])
    # Load trusted observer code before restricting filesystem access. It only
    # observes the diagnostic run; the independent plain invocation stays execve.
    import importlib.util
    evidence_spec = importlib.util.spec_from_file_location('relay_candidate_evidence', Path(__file__).with_name('candidate_evidence.py'))
    evidence = importlib.util.module_from_spec(evidence_spec)
    evidence_spec.loader.exec_module(evidence)

    # Check namespaces rather than trusting invocation flags or a marker alone.
    for name in ("user", "net", "pid"):
        if os.readlink(f"/proc/self/ns/{name}") == data["parent_namespaces"][name]:
            raise CompilerError(f"{name} namespace isolation was not established")
    if os.getpid() != 1:
        raise CompilerError("Sandbox child must initialize its private PID namespace")
    read = [Path(p) for p in ("/usr", "/bin", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime", "/etc/fonts", "/proc/self", "/proc/meminfo", "/proc/cpuinfo", "/proc/stat", "/proc/uptime", "/proc/sys/vm/mmap_min_addr", "/proc/sys/kernel/random/boot_id")]
    read.extend(Path(cfg[k]) for k in ("game_exe", "checker_dir", "compiler_dir", "function_index", "symbols"))
    if "compiler_tips" in cfg:
        read.append(Path(cfg["compiler_tips"]))
    # Preserve a configured venv's site-packages without granting its project root.
    python_home = Path(cfg["python"]).parent.parent
    if (python_home / "pyvenv.cfg").is_file():
        read.append(python_home)
    read.extend([work / "candidate.cpp", work / "pack"])
    write = [work / p for p in ("home", "tmp", "runtime", "cache")]
    write.extend([Path(cfg["wine_prefix"]), *_wine_servers(Path(cfg["wine_prefix"]))])
    # Wine uses these devices; directory-wide /dev and shared-memory grants are avoided.
    write.extend(Path(p) for p in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"))
    ready = work / "sandbox-ready.json"
    ready.touch(mode=0o600)
    write.append(ready)
    uid = os.getuid()
    directory_paths = [Path(f"/run/user/{uid}"), Path(f"/run/user/{uid}/wine"), Path(f"/tmp/.wine-{uid}")]
    abi = _landlock(read, write, directory_paths)
    ready.write_text(json.dumps({"landlock_abi": abi, "network_namespace": True}))
    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    image = subprocess.run([cfg["python"], "-I", "-B", str(Path(cfg["checker_dir"]) / "fnvcheck.py"), "--check-image"],
                           env=_env(cfg, work), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
    (work / "cache/image-check.stdout.log").write_bytes(image.stdout)
    (work / "cache/image-check.stderr.log").write_bytes(image.stderr)
    if image.returncode or not image.stdout.startswith(b"IMAGE OK:"):
        raise CompilerError("Official image preflight failed; see cache/image-check logs")
    argv = [cfg["python"], "-I", "-B", str(Path(cfg["checker_dir"]) / "fnvcheck.py"),
            "--pack", str(work / "pack"), str(work / "candidate.cpp")]
    if not data["plain"]:
        argv.append("--json")
        evidence.run(cfg['checker_dir'], work, argv[3:])
        return
    os.execve(cfg["python"], argv, _env(cfg, work))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--sandbox-child":
        raise SystemExit("Internal sandbox entry point only")
    try:
        _child(Path(sys.argv[2]))
    except (CompilerError, OSError) as error:
        print(f"Compiler sandbox refused: {error}", file=sys.stderr)
        raise SystemExit(125)
