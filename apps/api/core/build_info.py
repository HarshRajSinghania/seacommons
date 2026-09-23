# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small public-safe build/runtime identity helpers."""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def revision() -> str:
    for name in ("SEACOMMONS_BUILD_SHA", "GIT_COMMIT", "VERCEL_GIT_COMMIT_SHA"):
        value = str(os.getenv(name, "") or "").strip()
        if value:
            return value[:12]
    try:
        root = Path(__file__).resolve().parents[3]
        value = subprocess.check_output(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
        ).strip()
        return value[:12] if value else "unknown"
    except Exception:
        return "unknown"
