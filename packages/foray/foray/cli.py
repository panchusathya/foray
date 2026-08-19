"""Thin console entry point.

Most workflows are run as modules (``python -m foray.harvest.search`` etc.),
matching the build plan. This dispatcher just forwards to those module mains so
``foray <cmd>`` also works.
"""

from __future__ import annotations

import importlib
import sys

_COMMANDS = {
    "shards": "foray.harvest.shards",
    "harvest": "foray.harvest.search",
    "enrich": "foray.enrich.profiles",
    "emails": "foray.enrich.emails",
    "derive": "foray.enrich.derive",
    "embed": "foray.embed.run",
    "gate": "foray.score.gate",
    "score": "foray.score.run",
    "serve": "foray.api.app",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: foray <command> [args]\n\ncommands:")
        for name, mod in _COMMANDS.items():
            print(f"  {name:10s} -> python -m {mod}")
        return 0
    cmd, rest = argv[0], argv[1:]
    mod_name = _COMMANDS.get(cmd)
    if mod_name is None:
        print(f"foray: unknown command {cmd!r}", file=sys.stderr)
        return 2
    module = importlib.import_module(mod_name)
    sys.argv = [f"foray {cmd}", *rest]
    return int(module.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
