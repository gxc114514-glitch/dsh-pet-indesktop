#!/usr/bin/env python3
"""Probe local ACP agents with a Finder-like PATH (handshake only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pet.chat.acp.manager import AcpManager  # noqa: E402
from pet.chat.acp.presets import detect_builtin_acp_providers  # noqa: E402

FINDER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def main() -> int:
    os.environ["PATH"] = FINDER_PATH
    providers = detect_builtin_acp_providers()
    manager = AcpManager()
    failures = 0
    try:
        for provider_id, config in providers.items():
            try:
                ok, message = manager.probe_connection(config)
            except Exception as exc:  # Defensive: keep probing the other agents.
                ok, message = False, f"{type(exc).__name__}: {exc}"
            print(f"{provider_id}: {'PASS' if ok else 'FAIL'}: {message}", flush=True)
            failures += not ok
    finally:
        manager.shutdown_all()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
