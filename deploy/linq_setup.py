#!/usr/bin/env python3
"""Wire Linq iMessage intake: verify credentials, list lines, create webhooks.

Standard library only, like ``deploy/twilio_provision.py`` — provisioning should
not depend on the application venv.

Usage::

    python deploy/linq_setup.py verify
    python deploy/linq_setup.py numbers
    python deploy/linq_setup.py webhooks
    python deploy/linq_setup.py create-webhook \\
        --url https://app.example.com/v1/intake/linq/webhook?version=2026-02-03 \\
        --phone +14155551234 --write-env
    python deploy/linq_setup.py status
    python deploy/linq_setup.py contact-card
    python deploy/linq_setup.py set-contact-card --first-name Foray \\
        --image-url https://cdn.example.com/foray-mark.png
    python deploy/linq_setup.py print-register --org <org_id> --phone +14155551234

Credentials (never on the command line)::

    LINQ_API_KEY=...
    LINQ_FROM_NUMBER=+1...        # optional, for status / filtering
    LINQ_WEBHOOK_SECRET=whsec_... # written by create-webhook --write-env

After ``create-webhook``, register the line in Juicebox (cannot be scripted from
Linq's side)::

    juicebox intake-number +14155551234 --org <org_id> --provider linq
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"

API = "https://api.linqapp.com/api/partner/v3"
DEFAULT_EVENTS = (
    "message.received",
    "message.sent",
    "message.delivered",
    "message.read",
    "message.failed",
)
WEBHOOK_VERSION = "2026-02-03"


class LinqError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def api_key() -> str:
    from_file = read_env_file(ENV_PATH)
    key = os.environ.get("LINQ_API_KEY") or from_file.get("LINQ_API_KEY", "")
    if not key:
        raise LinqError(
            "No LINQ_API_KEY found.\n"
            f"Add it to {ENV_PATH} (gitignored) or export it.\n"
            "Dashboard: https://dashboard.linqapp.com/api-tooling"
        )
    return key


def from_number() -> str:
    from_file = read_env_file(ENV_PATH)
    return os.environ.get("LINQ_FROM_NUMBER") or from_file.get("LINQ_FROM_NUMBER", "")


def default_image_url() -> str:
    """Where the app serves the Foray mark, if we know our own base URL.

    Linq fetches the photo over the public internet at the moment the card is
    set, so this has to be the deployed host and not localhost. Empty when
    ``BASE_URL`` is unset, which leaves the card name-only rather than
    pointing Linq at a URL that will 404.
    """
    from_file = read_env_file(ENV_PATH)
    base = (os.environ.get("BASE_URL") or from_file.get("BASE_URL", "")).rstrip("/")
    if not base.startswith("https://"):
        return ""
    return f"{base}/v1/intake/linq/contact-card.png"


def request(method: str, path: str, *, body: dict | None = None) -> Any:
    url = f"{API}{path}"
    data = None
    headers = {"Authorization": f"Bearer {api_key()}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise LinqError(f"{method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LinqError(f"cannot reach Linq: {exc.reason}") from exc


def append_env(path: Path, key: str, value: str) -> None:
    line = f"{key}={value}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if f"{key}=" in existing:
        lines = []
        replaced = False
        for row in existing.splitlines(keepends=True):
            if row.startswith(f"{key}="):
                lines.append(line)
                replaced = True
            else:
                lines.append(row)
        if not replaced:
            lines.append(line)
        path.write_text("".join(lines), encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("\n# --- Linq intake (written by linq_setup.py) ---\n")
            handle.write(line)


def cmd_verify(_args: argparse.Namespace) -> int:
    payload = request("GET", "/phone-numbers")
    items = payload.get("data") or payload.get("phone_numbers") or payload
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or []
    count = len(items) if isinstance(items, list) else 0
    print(f"LINQ_API_KEY ok — {count} phone number(s) visible")
    return 0


def cmd_numbers(_args: argparse.Namespace) -> int:
    payload = request("GET", "/phone-numbers")
    items = payload.get("data") or payload.get("phone_numbers") or payload
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or []
    if not isinstance(items, list) or not items:
        print("no phone numbers on this account")
        return 0
    for row in items:
        if not isinstance(row, dict):
            continue
        phone = row.get("phone_number") or row.get("number") or row.get("handle") or "?"
        rep = (row.get("reputation") or {}).get("status") if isinstance(row.get("reputation"), dict) else row.get("reputation")
        status = row.get("status") or rep or "-"
        print(f"{phone}  status={status}")
    return 0


def cmd_webhooks(_args: argparse.Namespace) -> int:
    payload = request("GET", "/webhook-subscriptions")
    items = payload.get("data") or payload
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or []
    if not isinstance(items, list) or not items:
        print("no webhook subscriptions")
        return 0
    for row in items:
        if not isinstance(row, dict):
            continue
        sub_id = row.get("id") or "?"
        url = row.get("target_url") or row.get("url") or "?"
        active = row.get("active", True)
        events = row.get("subscribed_events") or row.get("events") or []
        print(f"{sub_id}  active={active}  url={url}")
        print(f"  events={', '.join(events) if events else '-'}")
    return 0


def cmd_create_webhook(args: argparse.Namespace) -> int:
    phone = args.phone or from_number()
    body: dict[str, Any] = {
        "target_url": args.url,
        "subscribed_events": list(DEFAULT_EVENTS),
    }
    if phone:
        body["phone_numbers"] = [phone]
    payload = request("POST", "/webhook-subscriptions", body=body)
    secret = payload.get("signing_secret") or payload.get("signingSecret") or ""
    sub_id = payload.get("id") or "?"
    print(f"created subscription {sub_id}")
    print(f"target_url: {args.url}")
    print(f"events: {', '.join(DEFAULT_EVENTS)}")
    if phone:
        print(f"phone_numbers: {phone}")
    if secret:
        print(f"\n signing_secret (store now — cannot be retrieved later):\n  {secret}")
        if args.write_env:
            append_env(ENV_PATH, "LINQ_WEBHOOK_SECRET", secret)
            append_env(ENV_PATH, "LINQ_ENABLED", "true")
            if phone:
                append_env(ENV_PATH, "LINQ_FROM_NUMBER", phone)
            print(f"\n appended LINQ_WEBHOOK_SECRET (+ LINQ_ENABLED) to {ENV_PATH}")
    else:
        print("\n(no signing_secret in response — check dashboard)")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    print("Linq intake setup status\n")
    try:
        api_key()
        print("  LINQ_API_KEY: set")
    except LinqError:
        print("  LINQ_API_KEY: missing")
        return 1
    from_file = read_env_file(ENV_PATH)
    secret = os.environ.get("LINQ_WEBHOOK_SECRET") or from_file.get("LINQ_WEBHOOK_SECRET", "")
    phone = from_number()
    enabled = os.environ.get("LINQ_ENABLED") or from_file.get("LINQ_ENABLED", "")
    print(f"  LINQ_WEBHOOK_SECRET: {'set' if secret else 'missing'}")
    print(f"  LINQ_FROM_NUMBER: {phone or '(not set)'}")
    print(f"  LINQ_ENABLED: {enabled or 'false'}")
    card_enabled = os.environ.get("LINQ_CONTACT_CARD_ENABLED") or from_file.get(
        "LINQ_CONTACT_CARD_ENABLED", ""
    )
    print(f"  LINQ_CONTACT_CARD_ENABLED: {card_enabled or 'false'}")
    print()
    cmd_contact_card(_args)
    print()
    cmd_numbers(_args)
    print()
    cmd_webhooks(_args)
    print(
        "\nNext: register the line in Juicebox (org from `juicebox seed` or your workspace):\n"
        f"  juicebox intake-number {phone or '<linq-e164>'} --org <org_id> --provider linq\n"
        "Then text the number from an iPhone and run `juicebox work --once` to drain turns."
    )
    return 0


def cmd_contact_card(_args: argparse.Namespace) -> int:
    """Show the iMessage identity this account offers, if any."""
    phone = from_number()
    path = f"/contact_card?phone_number={urllib.parse.quote(phone)}" if phone else "/contact_card"
    payload = request("GET", path)
    cards = payload.get("contact_cards")
    if not isinstance(cards, list):
        cards = [payload] if payload.get("phone_number") else []
    if not cards:
        print("no contact card on this account — run set-contact-card")
        return 0
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = " ".join(
            part for part in (card.get("first_name"), card.get("last_name")) if part
        )
        print(f"{card.get('phone_number') or '?'}  active={card.get('is_active')}")
        print(f"  name: {name or '(none)'}")
        print(f"  image: {card.get('image_url') or '(none)'}")
    return 0


def cmd_set_contact_card(args: argparse.Namespace) -> int:
    """Create or replace the card. Does not share it with anybody.

    Sharing happens per chat, from the app, once the candidate has replied --
    see ``messaging/contact_card.py``. This command only decides what the
    banner would say.
    """
    phone = args.phone or from_number()
    if not phone:
        raise LinqError("pass --phone or set LINQ_FROM_NUMBER in .env")
    body: dict[str, Any] = {"phone_number": phone, "first_name": args.first_name}
    if args.last_name:
        body["last_name"] = args.last_name
    image = args.image_url or default_image_url()
    if image:
        body["image_url"] = image
    payload = request("POST", "/contact_card", body=body)
    print(f"contact card set for {payload.get('phone_number') or phone}")
    print(f"  active: {payload.get('is_active')}")
    print(f"  name: {payload.get('first_name')} {payload.get('last_name') or ''}".rstrip())
    print(f"  image: {payload.get('image_url') or '(none)'}")
    print(
        "\nNothing is shared yet. Set LINQ_CONTACT_CARD_ENABLED=true and the app "
        "will offer the card once a candidate replies."
    )
    return 0


def cmd_print_register(args: argparse.Namespace) -> int:
    phone = args.phone or from_number()
    if not phone:
        raise LinqError("pass --phone or set LINQ_FROM_NUMBER in .env")
    if not args.org:
        raise LinqError("--org is required")
    print(
        f"juicebox intake-number {phone} --org {args.org} --provider linq --label 'linq intake'"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="check LINQ_API_KEY and list account access").set_defaults(
        func=cmd_verify
    )
    sub.add_parser("numbers", help="list phone numbers on the account").set_defaults(func=cmd_numbers)
    sub.add_parser("webhooks", help="list webhook subscriptions").set_defaults(func=cmd_webhooks)
    sub.add_parser("status", help="summary of credentials and Linq-side wiring").set_defaults(
        func=cmd_status
    )

    create = sub.add_parser("create-webhook", help="create a message.received subscription")
    create.add_argument(
        "--url",
        required=True,
        help="public URL, e.g. https://app.example.com/v1/intake/linq/webhook?version=2026-02-03",
    )
    create.add_argument("--phone", default="", help="E.164 filter (defaults to LINQ_FROM_NUMBER)")
    create.add_argument(
        "--write-env",
        action="store_true",
        help=f"append signing_secret and LINQ_ENABLED to {ENV_PATH}",
    )
    create.set_defaults(func=cmd_create_webhook)

    sub.add_parser(
        "contact-card", help="show the iMessage contact card on this account"
    ).set_defaults(func=cmd_contact_card)

    card = sub.add_parser(
        "set-contact-card",
        help="create or replace the contact card (name + photo). Shares it with nobody.",
    )
    card.add_argument("--first-name", required=True, help='e.g. "Foray"')
    card.add_argument("--last-name", default="", help="optional; omit for a one-word identity")
    card.add_argument(
        "--image-url",
        default="",
        help="public https URL for the photo (defaults to BASE_URL + the app's mark)",
    )
    card.add_argument("--phone", default="", help="E.164 (defaults to LINQ_FROM_NUMBER)")
    card.set_defaults(func=cmd_set_contact_card)

    reg = sub.add_parser(
        "print-register",
        help="print the juicebox intake-number command for this line",
    )
    reg.add_argument("--org", required=True)
    reg.add_argument("--phone", default="")
    reg.set_defaults(func=cmd_print_register)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LinqError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
