"""Nightly phase order: search-bucket jobs must not overlap."""

from __future__ import annotations

from foray.cli import _COMMANDS
from foray.refresh.nightly import _phases


def test_search_api_jobs_run_sequentially_after_harvest():
    names = [p.name for p in _phases()]
    assert names.index("harvest") < names.index("commit_search")
    assert names.index("commit_search") < names.index("name_bridge")
    # Own-repo mining (core REST) stays as the emails phase, before commit search.
    assert names.index("emails") < names.index("commit_search")
    # LinkedIn bulk (not Search API) then resolve dry-run so URL joins surface.
    assert names.index("linkedin") < names.index("resolve")
    assert names.index("linkedin") > names.index("emails")


def test_linkedin_phase_uses_the_existing_bulk_runner():
    linkedin = next(p for p in _phases() if p.name == "linkedin")
    assert linkedin.argv[0] == "foray.enrich.linkedin_bulk"
    resolve = next(p for p in _phases() if p.name == "resolve")
    assert "--scan" in resolve.argv
    assert "--apply" not in resolve.argv


def test_cli_wires_the_new_commands():
    assert _COMMANDS["commit-search"] == "foray.enrich.commit_search"
    assert _COMMANDS["name-bridge"] == "foray.enrich.name_bridge"
    assert _COMMANDS["verify"] == "foray.enrich.verify"
    assert _COMMANDS["resolve"] == "foray.ingest.resolve"
    assert _COMMANDS["linkedin"] == "foray.enrich.linkedin_bulk"
