from pathlib import Path
import struct

import pytest

from fnv_relay import toolchains


def compiler(root, name):
    path = root / name
    (path / "VC/bin").mkdir(parents=True)
    major, minor, build, revision = toolchains.VERSIONS[name]
    version = struct.pack(
        "<IIII", 0xFEEF04BD, 0x10000, major << 16 | minor, build << 16 | revision
    )
    for filename in toolchains.COMPONENTS:
        (path / "VC/bin" / filename).write_bytes(
            b"MZ" + (version if filename == "cl.exe" else filename.encode())
        )
    return path


def test_compiler_identity_rejects_wrong_build_and_changed_codegen(tmp_path):
    sp1 = compiler(tmp_path, "sp1")
    rtm = compiler(tmp_path, "rtm")
    records = {
        "sp1": toolchains.capture(sp1, "sp1"),
        "rtm": toolchains.capture(rtm, "rtm"),
    }
    toolchains.validate(records)
    with pytest.raises(ValueError, match="Wrong compiler version"):
        toolchains.capture(sp1, "rtm")
    (rtm / "VC/bin/c2.dll").write_bytes(b"changed")
    with pytest.raises(ValueError, match="generation changed"):
        toolchains.validate(records)

