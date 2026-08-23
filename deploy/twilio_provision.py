#!/usr/bin/env python3
"""Provision the voice intake phone number on Twilio and wire it to Retell.

Standard library only, like the other scripts in ``deploy/`` -- provisioning
should not depend on the application venv, and a telephony SDK is a large
dependency for nine HTTP calls.

Retell reaches a Twilio number through **elastic SIP trunking**, not through
Twilio's own webhooks. Three things have to line up:

* **Origination** (inbound: PSTN -> Twilio -> Retell) points at
  ``sip:sip.retellai.com``. This is how a candidate dialling the number reaches
  the agent.
* **Termination** (outbound: Retell -> Twilio -> PSTN) is a globally unique
  ``<name>.pstn.twilio.com`` domain. This is how the "call me" widget dials out.
* **Auth on termination**, so the trunk will not place calls for strangers.
  Retell documents an IP allowlist (``18.98.16.120/30``) or a credential list.
  This script uses a credential list by default: an allowlist silently breaks if
  Retell renumbers, and the failure looks like "outbound calls stopped working"
  with nothing in either dashboard saying why.

Order matters. ``buy`` spends money and every other subcommand does not, so it
is separate and needs ``--confirm``. Nothing here is idempotent by accident:
each step checks for what it is about to create first.

Usage:

    python deploy/twilio_provision.py verify
    python deploy/twilio_provision.py search --area-code 415
    python deploy/twilio_provision.py buy +14155550123 --confirm
    python deploy/twilio_provision.py trunk
    python deploy/twilio_provision.py attach +14155550123
    python deploy/twilio_provision.py status

Credentials are read from the environment, or from a ``.env`` at the repo root
(gitignored). Never passed on the command line, where they would land in shell
history:

    TWILIO_ACCOUNT_SID=AC...
    TWILIO_AUTH_TOKEN=...

``--write-env`` appends what provisioning produced -- trunk SID, termination
URI, SIP username and password, the number itself -- back into that same
``.env``, because those are exactly the values the Retell import needs and they
are secrets too.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"

API = "https://api.twilio.com/2010-04-01"
TRUNKING = "https://trunking.twilio.com/v1"

# Retell's inbound SIP endpoint. TCP is what their docs recommend.
RETELL_SIP_URI = "sip:sip.retellai.com;transport=tcp"
# Documented alternative to a credential list. Kept here so `trunk --ip-allowlist`
# is available, but not the default -- see the module docstring.
RETELL_IP_CIDR = "18.98.16.120/30"

TRUNK_FRIENDLY_NAME = "juicebox-voice-intake"
CRED_LIST_FRIENDLY_NAME = "juicebox-retell-termination"
IP_ACL_FRIENDLY_NAME = "juicebox-retell-ips"


class TwilioError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# credentials and .env
# --------------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Tolerates comments, blanks and quotes."""
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


def credentials() -> tuple[str, str]:
    """Account SID and auth token, environment first then ``.env``."""
    from_file = read_env_file(ENV_PATH)
    sid = os.environ.get("TWILIO_ACCOUNT_SID") or from_file.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN") or from_file.get("TWILIO_AUTH_TOKEN", "")
    if not sid or not token:
        raise TwilioError(
            "No Twilio credentials found.\n"
            f"Add them to {ENV_PATH} (gitignored) or export them:\n"
            "  TWILIO_ACCOUNT_SID=AC...\n"
            "  TWILIO_AUTH_TOKEN=...\n"
            "Console: https://console.twilio.com -> Account Info"
        )
    if not sid.startswith("AC"):
        raise TwilioError(
            "TWILIO_ACCOUNT_SID should start with 'AC'. An API Key SID (SK...) "
            "will not work for provisioning."
        )
    return sid, token


def append_env(pairs: dict[str, str]) -> None:
    """Append provisioning output to .env, skipping keys already present."""
    existing = read_env_file(ENV_PATH)
    new = {k: v for k, v in pairs.items() if k not in existing}
    if not new:
        print(f"  .env already has {', '.join(pairs)} -- not overwriting")
        return
    with ENV_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n# --- voice intake telephony (written by twilio_provision.py) ---\n")
        for key, value in new.items():
            handle.write(f"{key}={value}\n")
    print(f"  wrote {', '.join(new)} to {ENV_PATH}")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def request(
    method: str,
    url: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict:
    sid, token = credentials()
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        # Twilio puts a human-readable reason and a docs link in the body; the
        # status line alone ("400 Bad Request") is never enough to act on.
        try:
            parsed = json.loads(detail)
            message = parsed.get("message", detail)
            code = parsed.get("code", "")
            more = parsed.get("more_info", "")
            detail = f"{message}" + (f" (code {code}, {more})" if code else "")
        except json.JSONDecodeError:
            pass
        raise TwilioError(f"{method} {url.split('?')[0]} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TwilioError(f"cannot reach Twilio: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def account_path(suffix: str) -> str:
    sid, _ = credentials()
    return f"{API}/Accounts/{sid}/{suffix}"


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def cmd_verify(_args: argparse.Namespace) -> int:
    sid, _ = credentials()
    account = request("GET", f"{API}/Accounts/{sid}.json")
    print(f"account:  {account.get('friendly_name')}")
    print(f"sid:      {sid[:6]}...{sid[-4:]}")
    print(f"status:   {account.get('status')}")
    print(f"type:     {account.get('type')}")
    if account.get("type") == "Trial":
        # A trial account can only call numbers it has verified, which makes the
        # "call me" flow look broken for every real candidate.
        print(
            "\nWARNING: this is a TRIAL account. Outbound calls are restricted to "
            "verified numbers, so the website 'call me' flow will fail for real "
            "candidates. Upgrade before Phase 3."
        )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"VoiceEnabled": "true", "PageSize": args.limit}
    if args.sms:
        params["SmsEnabled"] = "true"
    if args.area_code:
        params["AreaCode"] = args.area_code
    payload = request(
        "GET", account_path(f"AvailablePhoneNumbers/{args.country}/Local.json"), params=params
    )
    numbers = payload.get("available_phone_numbers", [])
    if not numbers:
        print(f"No numbers available in {args.country} area code {args.area_code}.")
        print("Try a neighbouring area code (415 -> 628 for the Bay Area).")
        return 1
    print(f"{len(numbers)} available (search is free; nothing is reserved):\n")
    for entry in numbers:
        caps = entry.get("capabilities", {})
        flags = ",".join(k for k, v in sorted(caps.items()) if v)
        print(f"  {entry['phone_number']}  {entry.get('locality', '?'):<18} {flags}")
    print(f"\nBuy one with:\n  {_self()} buy {numbers[0]['phone_number']} --confirm")
    return 0


def cmd_buy(args: argparse.Namespace) -> int:
    if not args.confirm:
        print(
            f"This purchases {args.number} and starts monthly billing "
            "(~$1.15/mo US local, plus per-minute usage).\n"
            f"Re-run with --confirm to proceed:\n  {_self()} buy {args.number} --confirm"
        )
        return 1

    owned = _owned_numbers()
    for entry in owned:
        if entry["phone_number"] == args.number:
            print(f"Already owned: {args.number} ({entry['sid']}) -- nothing purchased.")
            return 0

    created = request(
        "POST",
        account_path("IncomingPhoneNumbers.json"),
        data={"PhoneNumber": args.number, "FriendlyName": TRUNK_FRIENDLY_NAME},
    )
    print(f"purchased: {created['phone_number']}")
    print(f"sid:       {created['sid']}")
    caps = created.get("capabilities", {})
    print(f"caps:      {','.join(k for k, v in sorted(caps.items()) if v)}")
    if args.write_env:
        append_env({"VOICE_INTAKE_NUMBER": created["phone_number"]})
    print(f"\nNext:\n  {_self()} trunk\n  {_self()} attach {created['phone_number']}")
    return 0


def cmd_trunk(args: argparse.Namespace) -> int:
    """Create the SIP trunk, its Retell origination, and termination auth."""
    trunk = _find_trunk()
    if trunk is None:
        domain = args.domain or f"{TRUNK_FRIENDLY_NAME}-{secrets.token_hex(3)}"
        if not domain.endswith(".pstn.twilio.com"):
            domain = f"{domain}.pstn.twilio.com"
        trunk = request(
            "POST",
            f"{TRUNKING}/Trunks",
            data={"FriendlyName": TRUNK_FRIENDLY_NAME, "DomainName": domain},
        )
        print(f"created trunk:    {trunk['sid']}")
    else:
        print(f"existing trunk:   {trunk['sid']}")
    trunk_sid = trunk["sid"]
    termination_uri = trunk["domain_name"]
    print(f"termination uri:  {termination_uri}")

    # Origination: inbound calls hand off to Retell.
    existing = request("GET", f"{TRUNKING}/Trunks/{trunk_sid}/OriginationUrls").get(
        "origination_urls", []
    )
    if any(u.get("sip_url") == RETELL_SIP_URI for u in existing):
        print("origination:      already points at Retell")
    else:
        request(
            "POST",
            f"{TRUNKING}/Trunks/{trunk_sid}/OriginationUrls",
            data={
                "FriendlyName": "retell",
                "SipUrl": RETELL_SIP_URI,
                "Priority": 1,
                "Weight": 1,
                "Enabled": "true",
            },
        )
        print(f"origination:      -> {RETELL_SIP_URI}")

    # Termination auth: what lets Retell place outbound calls on this trunk.
    produced: dict[str, str] = {
        "VOICE_INTAKE_TRUNK_SID": trunk_sid,
        "VOICE_INTAKE_TERMINATION_URI": termination_uri,
    }
    if args.ip_allowlist:
        _attach_ip_acl(trunk_sid)
        print(f"termination auth: IP allowlist {RETELL_IP_CIDR}")
        print(
            "  NOTE: this breaks silently if Retell renumbers. Prefer credentials."
        )
    else:
        username, password = _attach_credential_list(trunk_sid)
        if username:
            produced["VOICE_INTAKE_SIP_USERNAME"] = username
            produced["VOICE_INTAKE_SIP_PASSWORD"] = password
            print(f"termination auth: credential list, username {username}")
        else:
            print("termination auth: credential list already attached")
            print(
                "  Password is not retrievable from Twilio. If it was not saved, "
                f"run `{_self()} trunk --rotate-credential` to set a new one."
            )

    if args.write_env:
        append_env(produced)

    print("\nRetell import needs exactly these:")
    print(f"  phone_number:            {os.environ.get('VOICE_INTAKE_NUMBER', '<the number>')}")
    print(f"  termination_uri:         {termination_uri}")
    print("  sip_trunk_auth_username: (above / .env)")
    print("  sip_trunk_auth_password: (above / .env)")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    trunk = _find_trunk()
    if trunk is None:
        print(f"No trunk yet. Run `{_self()} trunk` first.")
        return 1
    match = next((e for e in _owned_numbers() if e["phone_number"] == args.number), None)
    if match is None:
        print(f"{args.number} is not on this account. Buy it first.")
        return 1

    attached = request("GET", f"{TRUNKING}/Trunks/{trunk['sid']}/PhoneNumbers").get(
        "phone_numbers", []
    )
    if any(p.get("phone_number") == args.number for p in attached):
        print(f"{args.number} is already on trunk {trunk['sid']}.")
        return 0

    request(
        "POST",
        f"{TRUNKING}/Trunks/{trunk['sid']}/PhoneNumbers",
        data={"PhoneNumberSid": match["sid"]},
    )
    print(f"attached {args.number} to trunk {trunk['sid']}")
    print(
        "\nInbound calls to this number now leave Twilio for Retell. Until the "
        "number is imported in Retell, callers will hear a failure -- do that next."
    )
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    numbers = _owned_numbers()
    print(f"owned numbers ({len(numbers)}):")
    for entry in numbers:
        caps = entry.get("capabilities", {})
        print(
            f"  {entry['phone_number']}  {entry.get('friendly_name', '')}  "
            f"[{','.join(k for k, v in sorted(caps.items()) if v)}]"
        )
    if not numbers:
        print("  (none)")

    trunk = _find_trunk()
    if trunk is None:
        print(f"\ntrunk '{TRUNK_FRIENDLY_NAME}': not created")
        return 0

    print(f"\ntrunk '{TRUNK_FRIENDLY_NAME}': {trunk['sid']}")
    print(f"  termination uri: {trunk['domain_name']}")
    origination = request("GET", f"{TRUNKING}/Trunks/{trunk['sid']}/OriginationUrls").get(
        "origination_urls", []
    )
    print(f"  origination:     {[u.get('sip_url') for u in origination] or '(none)'}")
    creds = request("GET", f"{TRUNKING}/Trunks/{trunk['sid']}/CredentialLists").get(
        "credential_lists", []
    )
    acls = request("GET", f"{TRUNKING}/Trunks/{trunk['sid']}/IpAccessControlLists").get(
        "ip_access_control_lists", []
    )
    print(f"  credential lists: {[c.get('friendly_name') for c in creds] or '(none)'}")
    print(f"  ip allowlists:    {[a.get('friendly_name') for a in acls] or '(none)'}")
    on_trunk = request("GET", f"{TRUNKING}/Trunks/{trunk['sid']}/PhoneNumbers").get(
        "phone_numbers", []
    )
    print(f"  numbers on trunk: {[p.get('phone_number') for p in on_trunk] or '(none)'}")

    if not origination:
        print("\n  INCOMPLETE: no origination URL, so inbound calls reach nothing.")
    if not creds and not acls:
        print("\n  INCOMPLETE: no termination auth, so Retell cannot dial out.")
    if not on_trunk:
        print("\n  INCOMPLETE: no number on the trunk.")
    return 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _self() -> str:
    return "python deploy/twilio_provision.py"


def _owned_numbers() -> list[dict]:
    return request("GET", account_path("IncomingPhoneNumbers.json"), params={"PageSize": 100}).get(
        "incoming_phone_numbers", []
    )


def _find_trunk() -> dict | None:
    trunks = request("GET", f"{TRUNKING}/Trunks", params={"PageSize": 100}).get("trunks", [])
    return next((t for t in trunks if t.get("friendly_name") == TRUNK_FRIENDLY_NAME), None)


def _generate_password() -> str:
    """Twilio requires 12+ chars with upper, lower and a digit."""
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(24))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


def _attach_credential_list(trunk_sid: str) -> tuple[str, str]:
    """Ensure a credential list is attached. Returns ("", "") if already there.

    Twilio never returns a SIP credential password after creation, so a caller
    that gets ("", "") cannot recover it and has to rotate instead.
    """
    attached = request("GET", f"{TRUNKING}/Trunks/{trunk_sid}/CredentialLists").get(
        "credential_lists", []
    )
    if attached:
        return "", ""

    lists = request("GET", account_path("SIP/CredentialLists.json"), params={"PageSize": 100}).get(
        "credential_lists", []
    )
    existing = next(
        (item for item in lists if item.get("friendly_name") == CRED_LIST_FRIENDLY_NAME), None
    )
    if existing is None:
        existing = request(
            "POST",
            account_path("SIP/CredentialLists.json"),
            data={"FriendlyName": CRED_LIST_FRIENDLY_NAME},
        )

    username = f"retell{secrets.token_hex(4)}"
    password = _generate_password()
    request(
        "POST",
        account_path(f"SIP/CredentialLists/{existing['sid']}/Credentials.json"),
        data={"Username": username, "Password": password},
    )
    request(
        "POST",
        f"{TRUNKING}/Trunks/{trunk_sid}/CredentialLists",
        data={"CredentialListSid": existing["sid"]},
    )
    return username, password


def _attach_ip_acl(trunk_sid: str) -> None:
    attached = request("GET", f"{TRUNKING}/Trunks/{trunk_sid}/IpAccessControlLists").get(
        "ip_access_control_lists", []
    )
    if attached:
        return
    acls = request(
        "GET", account_path("SIP/IpAccessControlLists.json"), params={"PageSize": 100}
    ).get("ip_access_control_lists", [])
    existing = next(
        (item for item in acls if item.get("friendly_name") == IP_ACL_FRIENDLY_NAME), None
    )
    if existing is None:
        existing = request(
            "POST",
            account_path("SIP/IpAccessControlLists.json"),
            data={"FriendlyName": IP_ACL_FRIENDLY_NAME},
        )
        address, _, prefix = RETELL_IP_CIDR.partition("/")
        request(
            "POST",
            account_path(f"SIP/IpAccessControlLists/{existing['sid']}/IpAddresses.json"),
            data={
                "FriendlyName": "retell-sip",
                "IpAddress": address,
                "CidrPrefixLength": int(prefix),
            },
        )
    request(
        "POST",
        f"{TRUNKING}/Trunks/{trunk_sid}/IpAccessControlLists",
        data={"IpAccessControlListSid": existing["sid"]},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="Check the credentials and account standing").set_defaults(
        func=cmd_verify
    )

    search = sub.add_parser("search", help="List purchasable numbers (free)")
    search.add_argument("--area-code", default="415")
    search.add_argument("--country", default="US")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument(
        "--no-sms",
        dest="sms",
        action="store_false",
        help="Do not require SMS capability (voice only)",
    )
    search.set_defaults(func=cmd_search, sms=True)

    buy = sub.add_parser("buy", help="Purchase a number (SPENDS MONEY)")
    buy.add_argument("number", help="E.164, e.g. +14155550123")
    buy.add_argument("--confirm", action="store_true", help="Required to actually purchase")
    buy.add_argument("--write-env", action="store_true")
    buy.set_defaults(func=cmd_buy)

    trunk = sub.add_parser("trunk", help="Create the SIP trunk and wire it to Retell")
    trunk.add_argument("--domain", default="", help="Termination subdomain (must be globally unique)")
    trunk.add_argument(
        "--ip-allowlist",
        action="store_true",
        help="Use Retell's IP block instead of a credential list (not recommended)",
    )
    trunk.add_argument("--write-env", action="store_true")
    trunk.set_defaults(func=cmd_trunk)

    attach = sub.add_parser("attach", help="Move a number onto the trunk")
    attach.add_argument("number")
    attach.set_defaults(func=cmd_attach)

    sub.add_parser("status", help="Show what exists so far").set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TwilioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
