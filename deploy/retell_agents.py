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
    python deploy/retell_agents.py import --number +16283865454   # own-Twilio number
    python deploy/retell_agents.py attach --number +16283865454   # goes live

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

# Warm, calm, unhurried American female. Do not guess a replacement --
# `voices --gender female` lists the catalogue so the pick is auditable, and
# nearby fits if this one wears badly on a phone are 11labs-Nina (softer) and
# 11labs-Kate (more corporate). Judge through a phone speaker, not a demo page.
VOICE_ID = "11labs-Myra"

# Spoken so TTS does not read +16283865454 as noise. Recognition of the
# follow-up sender is the entire point of saying it.
SMS_NUMBER_SPOKEN = "six two eight, three eight six, five four five four"
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

Acknowledge an answer in five words or fewer, then ask the next question.
NEVER read their answer back to them -- "Got it." beats "Thank you for sharing
that you want a level four backend role in the Bay Area." The recap at the end
is the only place you repeat anything, and only if they want it.

A cooperative call is done in two or three minutes. If they ask how long this
will take, say "just a couple of minutes" -- never a larger number.

Call the `check_coverage` tool after every two or three answers. It tells you
what is still needed and whether you may wrap up. Trust it over your own sense
of how much you have covered -- you cannot see the record, and it can.
If the tool fails or does not answer, carry on smoothly with the most useful
question you have not asked yet and try it again after the next answer. Never
mention tools, "checking coverage", or technical issues -- the caller must
never hear about your internal state.

- If `wrap_up` is false, ask about the first item in `still_needed`.
- If `wrap_up` is true, you have everything required. If they still sound
  willing, ask ONE topic from `optional_topics` -- the projects question is the
  most valuable one you can ask. Then recap the two or three most important
  facts back to them for confirmation, close the way `guidance` tells you to
  -- it is written for whichever follow-up is actually live -- and then ask, in
  these words: "Last thing -- can I text you updates at this number? You can
  reply STOP any time to opt out." Only an explicit, unambiguous yes is a yes;
  hesitation, "I guess", silence, or "email is better" is a no, and you must
  not re-ask or reframe to get a yes -- a soft yes recorded as consent is worse
  than a no. Then thank them and end the call. When you name the number, it is
  {sms_number_spoken}.

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
- On compensation, ask only what they are targeting -- NEVER what they earn now
  or earned before. Salary-history questions are illegal in California and many
  other states. If they volunteer a current salary, do not repeat it back, do
  not use it to steer the conversation, and move on. If they ask what a role
  pays, say the recruiter can share the range -- and in California they are
  entitled to the pay scale on request.
- On work authorisation, ask at most two questions: whether they are legally
  authorised to work in the US, and whether they will need sponsorship now or
  in the future. Never ask about citizenship, visa type, green-card status, or
  country of origin -- and if they volunteer those, do not pursue them.
- If they ask whether you are a real person, say plainly that you are an AI
  assistant. If they ask whether the call is recorded, answer truthfully.
  Never deflect either question.
- If the caller is clearly a minor, do not run the intake. Say a recruiter will
  follow up, take a message, and end the call.
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
   them. Then call `identify_caller` with that address and do exactly what its
   `guidance` says.

## What you may say about somebody's record

`identify_caller`'s `guidance` decides this, every time, and it is the only
thing that decides it. Never work it out for yourself, and never fill a gap
from what you can infer -- you cannot verify who is on the phone, and reading
somebody's salary or interview pipeline to the wrong caller is the worst thing
you could do on this call.

Sometimes it will tell you to reveal nothing at all, including whether a record
exists. Follow that without commenting on it: do not apologise, do not say you
are not allowed, do not hint. Take what they want to tell you and say a
recruiter will confirm the details directly. That is a complete, normal,
successful call.

When it does let you speak, `on_file` is a list of *topics*. Ask whether a
topic has changed. Never quote a figure, a company or a date back to them, even
one they gave you earlier in this same call.

## Then

Ask what has changed since they last spoke to Foray. Let them talk.

If they turn out to be new to Foray, say so plainly and run the full intake
instead: use `check_coverage` exactly as below and collect everything. Ask how
much time they have and pass it as `minutes_available`.

For a returning caller, still call `check_coverage` -- anything it lists as
`still_needed` is worth asking while you have them. It already knows what we
have on file, so trust it: if it says you may wrap up on the first check, that
is right, and re-asking things we already have is what makes a two-minute call
take fifteen.
{_shared_rules(SMS_NUMBER_SPOKEN)}"""

IDENTIFY_TOOL_DESCRIPTION = (
    "Look up a caller who says they already deal with Foray. Pass the email "
    "address they gave you. Returns guidance -- follow it exactly, it decides "
    "what you are allowed to say back, and it is not always the same. It may "
    "tell you to reveal nothing at all, including whether we have a record; "
    "that is normal and is not an error, so do not apologise or explain it. "
    "on_file lists topics only. Talk about a topic, never quote a figure, a "
    "company or a date from it. Call this once, after they give you an "
    "address. Call it a second time with send_matches true only if they said "
    "yes to being texted the roles."
)

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
# afterwards. `service._analysis_flag` looks for exactly these keys.
POST_CALL_ANALYSIS = [
    {
        "type": "boolean",
        "name": "consent_recording",
        "description": (
            "True only if the caller explicitly agreed to the call being "
            "recorded when asked at the start. False if they refused, or if "
            "they were never asked."
        ),
    },
    {
        "type": "boolean",
        "name": "sms_opt_in",
        "description": (
            "True only if the caller gave an explicit, unambiguous yes when "
            "asked whether they may be texted updates at this number. "
            "Hesitation, a conditional answer, a stated preference for email, "
            "or never being asked are all false."
        ),
    },
]


class RetellError(RuntimeError):
    pass


def env_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value and ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                break
    return value


def api_key() -> str:
    key = env_value("RETELL_API_KEY")
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
            },
            {
                "type": "custom",
                "name": "identify_caller",
                "description": IDENTIFY_TOOL_DESCRIPTION,
                "url": f"{base}/v1/intake/identify",
                "speak_during_execution": False,
                "speak_after_execution": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": (
                                "The email address the caller gave, exactly as "
                                "they gave it. Convert a spoken address to "
                                "normal form: \"priya dot raman at gmail dot "
                                "com\" is priya.raman@gmail.com."
                            ),
                        },
                        "send_matches": {
                            "type": "boolean",
                            "description": (
                                "True only when the caller has just said yes to "
                                "being texted the roles. Omit otherwise."
                            ),
                        },
                    },
                    "required": ["email"],
                },
            },
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
                # voice_id included so a VOICE_ID change actually reaches
                # existing agents -- create set it, update used to forget it.
                "voice_id": VOICE_ID,
                "webhook_url": f"{args.base_url.rstrip('/')}/v1/intake/webhook",
                "post_call_analysis_data": POST_CALL_ANALYSIS,
                "max_call_duration_ms": MAX_CALL_MS,
                **delivery_body(),
            },
        )
        print(f"updated {name}: agent={current['agent_id']} llm={llm_id}")

    print("\nAgents exist but answer nobody until attached:")
    print("  python deploy/retell_agents.py attach --number +16283865454")
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

    def bound(number: dict, direction: str) -> str:
        # Weighted lists are current; the single-agent field survives on
        # numbers configured before the 2026-03-31 deprecation.
        agents = number.get(f"{direction}_agents") or []
        if agents:
            return ",".join(a.get("agent_id", "?") for a in agents)
        return number.get(f"{direction}_agent_id") or "-"

    for n in numbers:
        print(f"{n['phone_number']}  inbound={bound(n, 'inbound')}  outbound={bound(n, 'outbound')}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Bring a number from our own Twilio account into Retell.

    The counterpart to ``twilio_provision.py``: that script builds the SIP
    trunk (origination to Retell, credential-list termination auth) and puts
    the number on it; this command tells Retell the trunk exists. The trunk
    values default from the ``.env`` entries ``trunk --write-env`` produced,
    because they are secrets and must not land in shell history.

    Importing changes nothing a caller can reach -- until ``attach``, a call to
    the number reaches Retell and finds no agent. Deliberately the same split
    as ``apply``/``attach``.
    """
    termination = args.termination_uri or env_value("VOICE_INTAKE_TERMINATION_URI")
    username = args.sip_username or env_value("VOICE_INTAKE_SIP_USERNAME")
    password = args.sip_password or env_value("VOICE_INTAKE_SIP_PASSWORD")
    missing = [
        flag
        for flag, value in (
            ("--termination-uri / VOICE_INTAKE_TERMINATION_URI", termination),
            ("--sip-username / VOICE_INTAKE_SIP_USERNAME", username),
            ("--sip-password / VOICE_INTAKE_SIP_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        print("missing trunk details:\n  " + "\n  ".join(missing))
        print("\nRun `python deploy/twilio_provision.py trunk --write-env` first.")
        return 1

    existing = {n.get("phone_number") for n in request("GET", "/list-phone-numbers") or []}
    if args.number in existing:
        print(f"{args.number} is already in Retell.")
        return 0

    request(
        "POST",
        "/import-phone-number",
        {
            "phone_number": args.number,
            "termination_uri": termination,
            "sip_trunk_auth_username": username,
            "sip_trunk_auth_password": password,
            "nickname": args.nickname,
        },
    )
    print(f"imported {args.number} via trunk {termination}")
    print("No agent answers it yet -- that is `attach`, deliberately separate.")
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
    # Weighted agent lists (the single-agent fields were removed 2026-03-31).
    # One agent per direction, so each list is a single entry at weight 1.
    body = {
        "inbound_agents": [{"agent_id": have[INBOUND_NAME]["agent_id"], "weight": 1}],
        "outbound_agents": [{"agent_id": have[OUTBOUND_NAME]["agent_id"], "weight": 1}],
    }
    if args.detach:
        body = {"inbound_agents": [], "outbound_agents": []}
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

    imp = sub.add_parser("import", help="import a Twilio trunk number into Retell")
    imp.add_argument("--number", required=True, help="E.164, e.g. +16283865454")
    imp.add_argument(
        "--termination-uri", default="", help="defaults to VOICE_INTAKE_TERMINATION_URI in .env"
    )
    imp.add_argument("--sip-username", default="", help="defaults to VOICE_INTAKE_SIP_USERNAME")
    imp.add_argument("--sip-password", default="", help="defaults to VOICE_INTAKE_SIP_PASSWORD")
    imp.add_argument("--nickname", default="foray-intake")
    imp.set_defaults(func=cmd_import)

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
