"""Load archetypes.yaml, compute rubric hashes, validate gates, sync to the DB.

The effective rubric is (rubric + "\n\n" + shared_rubric_tail) concatenated in
Python -- YAML anchors do not expand inside block scalars, and hashing the rubric
alone would let a shared-tail edit silently fail to invalidate stored scores.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import PACKAGE_ROOT

ARCHETYPES_PATH = PACKAGE_ROOT / "archetypes.yaml"

# The gate is evaluated inside this shape; validation uses EXPLAIN (plan-only).
GATE_FROM = "candidate_signals s join candidates c on c.id = s.candidate_id"


@dataclass(frozen=True)
class Archetype:
    id: str
    label: str
    gate: str
    rubric: str  # effective (concatenated) rubric
    rubric_hash: str

    def gate_markers(self) -> set[str]:
        """Marker literals referenced in the gate (for the offline heuristic)."""
        found: set[str] = set()
        for group in re.findall(r"array\[([^\]]*)\]", self.gate):
            for token in group.split(","):
                found.add(token.strip().strip("'\""))
        return {m for m in found if m}


@lru_cache(maxsize=1)
def load_archetypes(path: str | Path = ARCHETYPES_PATH) -> dict[str, Archetype]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    tail = (doc.get("shared_rubric_tail") or "").strip()
    out: dict[str, Archetype] = {}
    for entry in doc["archetypes"]:
        effective = entry["rubric"].rstrip() + "\n\n" + tail
        rubric_hash = hashlib.sha256(effective.encode("utf-8")).hexdigest()
        out[entry["id"]] = Archetype(
            id=entry["id"],
            label=entry["label"],
            gate=" ".join(entry["gate"].split()).strip(),
            rubric=effective,
            rubric_hash=rubric_hash,
        )
    return out


async def validate_gates(conn, archetypes: dict[str, Archetype]) -> None:
    """Fail fast if any gate does not parse or references a missing column."""
    for arch in archetypes.values():
        try:
            await conn.execute(f"explain select 1 from {GATE_FROM} where ({arch.gate})")
        except Exception as exc:
            raise SystemExit(
                f"archetype {arch.id!r} has an invalid gate: {exc}\n  gate: {arch.gate}"
            ) from exc


async def sync_archetypes(conn, archetypes: dict[str, Archetype] | None = None) -> dict[str, Archetype]:
    archetypes = archetypes or load_archetypes()
    await validate_gates(conn, archetypes)
    for arch in archetypes.values():
        await conn.execute(
            """
            insert into archetypes (id, label, gate, rubric, rubric_hash, updated_at)
            values ($1, $2, $3, $4, $5, now())
            on conflict (id) do update set
                label = excluded.label,
                gate = excluded.gate,
                rubric = excluded.rubric,
                rubric_hash = excluded.rubric_hash,
                updated_at = now()
            """,
            arch.id,
            arch.label,
            arch.gate,
            arch.rubric,
            arch.rubric_hash,
        )
    return archetypes
