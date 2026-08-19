from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.dbutil import requires_db

MIGRATE = Path(__file__).resolve().parent.parent / "scripts" / "migrate.sh"


@requires_db
def test_migrate_sh_is_idempotent():
    env = os.environ.copy()
    first = subprocess.run(
        [str(MIGRATE)], check=True, capture_output=True, text=True, env=env
    )
    second = subprocess.run(
        [str(MIGRATE)], check=True, capture_output=True, text=True, env=env
    )
    assert "migrations up to date" in first.stdout
    assert "migrations up to date" in second.stdout
    assert "apply " not in second.stdout
    for line in second.stdout.splitlines():
        if line.startswith("skip "):
            continue
        assert line.strip() in {"migrations up to date", ""}
