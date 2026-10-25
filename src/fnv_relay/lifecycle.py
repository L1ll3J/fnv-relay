"""Small lifecycle helpers; no service calls and no credentials."""
from __future__ import annotations

import os
from pathlib import Path


def process_identity(pid=None):
    """Bind a Linux PID to its boot-relative start time to defeat PID reuse."""
    pid = os.getpid() if pid is None else int(pid)
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start_time': fields[19]}
    except (OSError, IndexError, ValueError):
        return None


def owner_alive(owner):
    # Old state has no binding; retain its expiry-only guardian behavior.
    if not owner:
        return True
    return process_identity(owner.get('pid')) == owner
