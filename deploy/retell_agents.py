#!/usr/bin/env python3
"""Create or update the two Retell agents and point them at our endpoints.

Standard library only, like the rest of ``deploy/``.

**A script rather than a handful of curl calls, because the URL moves.** During
development the endpoints sit behind an ephemeral tunnel that changes every time
it restarts; in production they are on ``app.goforay.io``. Re-pointing has to be
one command, or the agents quietly keep calling a dead host and the failure looks
like an agent that has stopped asking questions.

Idempotent on ``agent_name``: run it again and it updates in place rather than
filling the workspace with duplicates.

Two agents, because Retell allows one inbound and one outbound per number and
they are genuinely different conversations:

* **outbound** — the website "call me" flow. A first full intake, driven by the
  coverage gate.
* **inbound** — the published status line. Someone calling us back, usually to
  say something changed.

Both call ``check_coverage`` mid-call. That tool is the reason a cooperative
call ends in two or three minutes: it answers "what do I still need, and may I
wrap up?" from the transcript so far, so the model never has to judge how long
it has been talking.

Usage:

    python deploy/retell_agents.py plan   --base-url https://example.com
    python deploy/retell_agents.py apply  --base-url https://example.com
    python deploy/retell_agents.py list
    python deploy/retell_agents.py attach --number +18882600142   # goes live

``attach`` is separate on purpose. Creating agents changes nothing a caller can
reach; attaching one to a number makes a bot answer real people.
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
API = "https://api.retellai.com"

OUTBOUND_NAME = "foray-intake-outbound"
INBOUND_NAME = "foray-status-inbound"

# Retell runs the conversation on its own model list, which is entirely
# proprietary -- no open-source option exists there, so the house policy of
# cheap open models governs our extraction (OpenRouter) but cannot reach this.
# `mini` rather than `nano`: this agent drives an eleven-item checklist through
# a tool call every few turns, and the nano tier drops tool calls under that
# load, which presents as an agent that forgets to ask things.
AGENT_MODEL = "gpt-4.1-mini"

# Female, warm, American. Do not guess a replacement -- `voices --gender female`
# lists the catalogue so the pick is auditable. Audition the sample, not the name.
VOICE_ID = "11labs-Cimo"

# Spoken so TTS does not read +18882600142 as noise. Recognition of the
# follow-up sender is the entire point of saying it.
SMS_NUMBER_SPOKEN = "eight eight eight, two six zero, zero one four two"
RECRUITER_NAME = "Vivian"

# Slightly under 1.0 so she does not sound rushed.
VOICE_SPEED = 0.92
# A little variation so the delivery is not flat.
VOICE_TEMPERATURE = 0.85
# Lower than default so she does not talk over a thinking pause.
INTERRUPTION_SENSITIVITY = 0.4
# Still answers promptly after they finish.
RESPONSIVENESS = 0.85
# The "mm-hm" that makes a listener sound present.
ENABLE_BACKCHANNEL = True
# Present, not constant -- more than this sounds like a tic.
BACKCHANNEL_FREQUENCY = 0.6

# A hard ceiling, not a target. The gate ends a good call in two to three
# minutes; this only stops a wedged one from billing for an hour.
MAX_CALL_MS = 15 * 60 * 1000

_SHARED_RULES = """
## How to run the call

Ask ONE question at a time and wait. This is a phone call, not a form.

Call the `check_coverage` tool after every two or three answers. It tells you
what is still needed and whether you may wrap up. Trust it over your own sense
of how much you have covered -- you cannot see the record, and it can.

- If `wrap_up` is false, ask about the first item in `still_needed`.
- If `wrap_up` is true, you have everything required. If they still sound
  willing, ask ONE topic from `optional_topics` -- the projects question is the
  most valuable one you can ask. Then recap the two or three most important
  facts back to them for confirmation, close the way `guidance` tells you to
  -- it is written for whichever follow-up is actually live -- and end the
  call. When you name the number, it is {sms_number_spoken}.

If `progress_line` is present, say it, then ask the next question. If it is
empty, do not invent a count and do not mention how many are left.

Two follow-ups maximum on any topic. If an answer stays vague after that, move
on -- a vague answer recorded honestly beats a precise one you pushed for.

When an answer is vague, ask the question a hiring company would ask. "I want a
startup" -> "What stage? Would you join something pre-product-market-fit, or do
you want post-Series-B stability?"

If they decline to answer something, accept it immediately and say you will note
it. Never ask a declined question twice.

## Hard rules

- Never promise a role, an interview, or an introduction.
- Never name a client company, and never invent one.
- Never give legal, visa, or immigration advice beyond noting what they told you
  about their own work authorisation.
- Never tell them how strong a candidate they are, or evaluate them at all. You
  are collecting what they want, not judging what they are worth.
- Never ask about age, race, religion, health, disability, family or marital
  status, or anything else protected. If they volunteer it, do not repeat it and
  do not ask a follow-up.
- If they ask something you do not know, or ask for a person, say a recruiter
  will follow up and take a message. Never guess.
- If they sound distressed or want to stop, stop. Thank them and end the call.

## Sounding human

No filler, but not clipped. This is a conversation, not a form.

You may react once to the *domain* of what they said -- the field, never the
person. Allowed: "Backend infrastructure -- that's an area with real movement
right now." That reacts to the work, promises nothing.

Never:
- react to compensation, notice period, work authorisation, or anything
  protected
- tell them they are strong, in demand, or a good fit
- name or imply a client
- quote a number of roles
- say anything a candidate could reasonably hear as a promise

At most one reaction per topic. Never two reactions in a row. Never a reaction
instead of the next question.
"""

def _shared_rules(sms_spoken: str) -> str:
    return _SHARED_RULES.format(sms_number_spoken=sms_spoken)


# Outbound: Retell interpolates {{recruiter_name}} / {{sms_number_spoken}}.
# Four braces because this is an f-string; two would render as a literal
# {recruiter_name} and the agent would say the placeholder aloud.
OUTBOUND_PROMPT = f"""You are {{{{recruiter_name}}}}, an AI recruiting assistant
with Foray. You are calling a candidate who just asked to be called from
Foray's website, to learn what they want from their next role so Foray can
match them properly.

Speak like a competent recruiter: warm, direct, unhurried. No filler, but not
clipped.

## Opening, in this order, before anything else

1. Say who you are: {{{{recruiter_name}}}}, an AI recruiting assistant with
   Foray, calling because they asked.
2. Say the call is recorded so a recruiter can read it later, and ask if that is
   OK. Wait for an answer. If they say no, say you will not record, and end the
   call politely -- tell them a recruiter will reach out directly instead.
3. Once they agree, ask how much time they have. Then confirm what you will fit
   into that, and that they can skip any question. Pass their answer to
   `check_coverage` as `minutes_available` the first time you call the tool.

Do not ask anything substantive before they have answered the recording
question.
{_shared_rules("{{sms_number_spoken}}")}"""

# Inbound gets no dynamic variables from us, so bake the defaults into the text.
INBOUND_PROMPT = f"""You are {RECRUITER_NAME}, an AI recruiting assistant
on Foray's candidate line. Someone has called in. Usually it is a candidate
Foray already knows, calling to say something has changed -- a new interview,
an offer, a timeline that moved. It may also be someone Foray has never spoken
to.

Speak like a competent recruiter: warm, direct, unhurried. No filler, but not
clipped.

## Opening, in this order, before anything else

1. Say who you are: {RECRUITER_NAME}, an AI recruiting assistant on
   Foray's candidate line.
2. Say the call is recorded so a recruiter can read it later, and ask if that is
   OK. Wait. If they say no, take a brief message and end the call.
3. Ask for their name and the email they use with Foray, so a recruiter can find
   them. Do not tell them anything about their own record, even if they ask -- you
   cannot verify who is on the phone, and reading someone's salary or interview
   pipeline to the wrong caller is the worst thing you could do on this call.
   Say a recruiter will confirm the details with them directly.

## Then

Ask what has changed since they last spoke to Foray. Let them talk.

If they turn out to be new to Foray, say so plainly and run the full intake
instead: use `check_coverage` exactly as below and collect everything. Ask how
much time they have and pass it as `minutes_available`.

For a returning caller, still call `check_coverage` -- anything it lists as
`still_needed` is worth asking while you have them.
{_shared_rules(SMS_NUMBER_SPOKEN)}"""

COVERAGE_TOOL_DESCRIPTION = (
    "Check what this intake call still needs and whether you may wrap up. Call "
    "this after every two or three answers, and before you consider ending the "
    "call. Pass minutes_available (integer) once the candidate has said how "
    "long they have -- omit it if they have not. Returns wrap_up (true when "
    "everything required is covered), still_needed (topics to ask next, in "
    "order), optional_topics (worth asking only once wrap_up is true), "
    "progress_line (say it if present, then ask; if empty do not invent a "
    "count) and guidance."
)


# Post-call analysis is how consent reaches the record: the agent cannot set a
# dynamic variable mid-call, so the platform reads it back out of the transcript
# afterwards. `service._consent_given` looks for exactly this key.
POST_CALL_ANALYSIS = [
    {
        "type": "boolean",
        "name": "consent_recording",
        "description": (
            "True only if the caller explicitly agreed to the call being "
            "recorded when asked at the start. False if they refused, or if "
            "they were never asked."
        ),
    }
]


class RetellError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("RETELL_API_KEY", "")
    if not key and ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("RETELL_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
                break
    if not key:
        raise RetellError(f"No RETELL_API_KEY in the environment or {ENV_PATH}")
    return key


def request(method: str, path: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key()}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RetellError(f"{method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RetellError(f"cannot reach Retell: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def llm_body(*, base_url: str, prompt: str, begin_message: str) -> dict:
    base = base_url.rstrip("/")
    return {
        "model": AGENT_MODEL,
        "general_prompt": prompt,
        "begin_message": begin_message,
        "general_tools": [
            {
                "type": "custom",
                "name": "check_coverage",
                "description": COVERAGE_TOOL_DESCRIPTION,
                "url": f"{base}/v1/intake/coverage",
                # The call answers in about a second, so an "let me check"
                # filler would be more jarring than the pause it covers.
                "speak_during_execution": False,
                # The whole point: the result decides the next question.
                "speak_after_execution": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutes_available": {
                            "type": "integer",
                            "description": (
                                "How many minutes the candidate said they have. "
                                "Pass once they have answered. Omit if they "
                                "have not said."
                            ),
                        }
                    },
                    "required": [],
                },
            }
        ],
    }


def delivery_body() -> dict:
    """Voice and delivery knobs. Used on create AND update.

    Leaving these off the PATCH body is how changing VOICE_ID used to be a
    no-op against the live agent.
    """
    return {
        "voice_id": VOICE_ID,
        "voice_speed": VOICE_SPEED,
        "voice_temperature": VOICE_TEMPERATURE,
        "interruption_sensitivity": INTERRUPTION_SENSITIVITY,
        "responsiveness": RESPONSIVENESS,
        "enable_backchannel": ENABLE_BACKCHANNEL,
        "backchannel_frequency": BACKCHANNEL_FREQUENCY,
    }


def agent_body(*, llm_id: str, name: str, base_url: str) -> dict:
    return {
        "agent_name": name,
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "language": "en-US",
        "webhook_url": f"{base_url.rstrip('/')}/v1/intake/webhook",
        "post_call_analysis_data": POST_CALL_ANALYSIS,
        "max_call_duration_ms": MAX_CALL_MS,
        **delivery_body(),
    }


SPECS = [
    {
        "name": OUTBOUND_NAME,
        "prompt": OUTBOUND_PROMPT,
        "begin_message": (
            f"Hi, this is {RECRUITER_NAME}, an AI recruiting assistant with Foray, "
            "calling because you asked us to get in touch. Before we start -- this "
            "call is recorded so a recruiter can read it later. Is that OK with you?"
        ),
    },
    {
        "name": INBOUND_NAME,
        "prompt": INBOUND_PROMPT,
        # Inbound has no begin_message: the caller dialled us and speaks first.
        "begin_message": "",
    },
]


def existing_agents() -> dict[str, dict]:
    agents = request("GET", "/list-agents") or []
    return {a["agent_name"]: a for a in agents if a.get("agent_name")}


def cmd_plan(args: argparse.Namespace) -> int:
    have = existing_agents()
    print(f"base url : {args.base_url.rstrip('/')}")
    print(f"model    : {AGENT_MODEL}   voice: {VOICE_ID}")
    print(f"persona  : {RECRUITER_NAME}")
    print(f"spoken # : {SMS_NUMBER_SPOKEN}")
    print(f"coverage : {args.base_url.rstrip('/')}/v1/intake/coverage")
    print(f"webhook  : {args.base_url.rstrip('/')}/v1/intake/webhook")
    print()
    for spec in SPECS:
        verb = "update" if spec["name"] in have else "create"
        print(f"  {verb:<6} {spec['name']}")
    print("\nNothing has been changed. Re-run with `apply`.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    have = existing_agents()
    for spec in SPECS:
        name = spec["name"]
        body = llm_body(
            base_url=args.base_url, prompt=spec["prompt"], begin_message=spec["begin_message"]
        )
        current = have.get(name)
        if current is None:
            llm = request("POST", "/create-retell-llm", body)
            agent = request(
                "POST",
                "/create-agent",
                agent_body(llm_id=llm["llm_id"], name=name, base_url=args.base_url),
            )
            print(f"created {name}: agent={agent['agent_id']} llm={llm['llm_id']}")
            continue

        # Update in place. The llm_id is stable, so re-running only rewrites the
        # prompt and the URLs -- which is the point when the tunnel moves.
        llm_id = (current.get("response_engine") or {}).get("llm_id")
        if not llm_id:
            raise RetellError(f"{name} has no retell-llm engine; delete it and re-run")
        request("PATCH", f"/update-retell-llm/{llm_id}", body)
        request(
            "PATCH",
            f"/update-agent/{current['agent_id']}",
            {
                "webhook_url": f"{args.base_url.rstrip('/')}/v1/intake/webhook",
                "post_call_analysis_data": POST_CALL_ANALYSIS,
                "max_call_duration_ms": MAX_CALL_MS,
                **delivery_body(),
            },
        )
        print(f"updated {name}: agent={current['agent_id']} llm={llm_id}")

    print("\nAgents exist but answer nobody until attached:")
    print("  python deploy/retell_agents.py attach --number +18882600142")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    agents = request("GET", "/list-agents") or []
    if not agents:
        print("no agents")
        return 0
    for a in agents:
        engine = a.get("response_engine") or {}
        print(f"{a.get('agent_name','(unnamed)'):<28} {a['agent_id']}")
        print(f"  llm={engine.get('llm_id')}  voice={a.get('voice_id')}")
        print(f"  webhook={a.get('webhook_url')}")
    numbers = request("GET", "/list-phone-numbers") or []
    print()
    for n in numbers:
        print(
            f"{n['phone_number']}  inbound={n.get('inbound_agent_id') or '-'}  "
            f"outbound={n.get('outbound_agent_id') or '-'}"
        )
    return 0


def cmd_voices(args: argparse.Namespace) -> int:
    """List the Retell catalogue so a voice pick is auditable."""
    voices = request("GET", "/list-voices") or []
    gender = (args.gender or "").strip().lower()
    shown = 0
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        raw_gender = str(voice.get("gender") or voice.get("voice_gender") or "")
        if gender and raw_gender.lower() != gender:
            continue
        vid = voice.get("voice_id") or voice.get("id") or ""
        name = voice.get("voice_name") or voice.get("voice_type") or voice.get("name") or ""
        mark = "  <-- current" if vid == VOICE_ID else ""
        print(f"{vid:<28} {raw_gender:<8} {name}{mark}")
        shown += 1
    print(f"\n{shown} voices" + (f" (gender={gender})" if gender else ""))
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Make the agents answer real callers. Deliberately a separate command."""
    have = existing_agents()
    missing = [s["name"] for s in SPECS if s["name"] not in have]
    if missing:
        print(f"missing agents: {', '.join(missing)}. Run `apply` first.")
        return 1
    body = {
        "inbound_agent_id": have[INBOUND_NAME]["agent_id"],
        "outbound_agent_id": have[OUTBOUND_NAME]["agent_id"],
    }
    if args.detach:
        body = {"inbound_agent_id": None, "outbound_agent_id": None}
    number = urllib.parse.quote(args.number)
    request("PATCH", f"/update-phone-number/{number}", body)
    verb = "detached from" if args.detach else "attached to"
    print(f"{verb} {args.number}")
    if not args.detach:
        print("This number now answers real callers with the inbound agent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, needs_url in (
        ("plan", cmd_plan, True),
        ("apply", cmd_apply, True),
        ("list", cmd_list, False),
    ):
        p = sub.add_parser(name)
        if needs_url:
            p.add_argument(
                "--base-url",
                required=True,
                help="Public origin serving /v1/intake/* (tunnel or app.goforay.io)",
            )
        p.set_defaults(func=func)

    voices = sub.add_parser("voices", help="list the Retell voice catalogue")
    voices.add_argument("--gender", default="", help="filter, e.g. female")
    voices.set_defaults(func=cmd_voices)

    attach = sub.add_parser("attach", help="point a number at the agents (GOES LIVE)")
    attach.add_argument("--number", required=True)
    attach.add_argument("--detach", action="store_true", help="unset both agents instead")
    attach.set_defaults(func=cmd_attach)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RetellError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
