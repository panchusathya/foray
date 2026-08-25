"""The compose file and config.py have to agree about the environment.

Three production bugs in one day came from this seam, and none of them raised
anything -- the symptom each time was a feature quietly not working:

* ``RETELL_API_KEY`` was set in .env and absent from compose, so the api never
  saw it and refused every Retell webhook. `intake_calls` stayed empty.
* ``APIFY_LINKEDIN_ACTOR`` was in compose as ``${VAR:-}``, which sets the
  variable to an empty string, so ``_env(name, default)`` returned "" instead
  of the default and every LinkedIn lookup would have hit a malformed URL.
* ``PROPOSAL_SCORE_FLOOR`` was read by the code and missing from compose, so
  the kill switch for candidate-facing proposals did nothing when thrown.

These read the two files and compare them, the same way TestPersonaCopy and
TestUiMirrorsTheMachine keep other duplicated facts honest.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "packages" / "core" / "juicebox" / "config.py"
COMPOSE = REPO / "docker-compose.prod.yml"

_READS = re.compile(r'_env(?:_int|_bool|_float)?\(\s*"([A-Z0-9_]+)"')
_STRING_DEFAULT = re.compile(r'_env\(\s*"([A-Z0-9_]+)"\s*,\s*"([^"]*)"')
_PASSES = re.compile(r"\$\{([A-Z0-9_]+)")
_PASSES_EMPTY = re.compile(r"\$\{([A-Z0-9_]+):-\}")

# Anything shaped like a credential is something a person deliberately pastes
# into .env, which makes silently dropping it the expensive kind of wrong.
_CREDENTIAL = re.compile(r"(_API_KEY|_TOKEN|_SECRET|_PASSWORD|_COOKIE|_CLIENT_ID)$")


def _config() -> str:
    return CONFIG.read_text(encoding="utf-8")


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


class TestComposePassesWhatTheCodeReads:
    def test_every_credential_reaches_a_container(self):
        """A key in .env that compose does not pass is a key that does nothing.

        This is exactly how RETELL_API_KEY was set correctly and ignored for
        weeks: verify() refused every webhook, and the only trace was a log
        line nobody was reading.
        """
        read = set(_READS.findall(_config()))
        passed = set(_PASSES.findall(_compose()))
        missing = sorted(n for n in read - passed if _CREDENTIAL.search(n))
        assert not missing, (
            "config.py reads these credentials but docker-compose.prod.yml never "
            f"passes them, so they are always empty in production: {missing}"
        )

    def test_no_compose_entry_silently_kills_a_code_default(self):
        """``${VAR:-}`` SETS the variable, to an empty string.

        `_env(name, default)` therefore returns "" rather than the default,
        so a default written in config.py is decoration. Either pass the real
        default in compose, or do not give the setting one in code.
        """
        defaults = dict(_STRING_DEFAULT.findall(_config()))
        empty = set(_PASSES_EMPTY.findall(_compose()))
        dead = sorted(f"{n} (code default {defaults[n]!r})" for n in empty if defaults.get(n))
        assert not dead, (
            "compose passes an empty string for these, which overrides the "
            f"default in config.py rather than falling back to it: {dead}"
        )
