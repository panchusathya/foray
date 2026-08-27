"""Who is on the other end: the name and address, on either channel.

These are **not preferences and must never enter the spec.** ``ParsedQuery`` is
the vocabulary a job description is parsed into, and a name has no counterpart
on the job side -- a ``contact_name`` key inside ``spec`` would be a field
``jobmatch.engine`` cannot score, which is how a record quietly turns back into
notes. They live on ``IntakeCall``/``IntakeThread`` and reach the domain as an
ordinary candidate row.

This module is in ``intake`` rather than ``messaging`` because ``intake`` is the
lower layer: ``messaging`` imports it, and the reverse would be a cycle. Both
channels ask the same two questions, and a second definition of "does this look
like a name" is a second answer to it -- the drift ``AGENTS.md`` records against
every other copied predicate here.

Nothing in here calls a model. An address is unambiguous, and a name arrives as
the answer to the question that just asked for one; neither needs judgement, and
both have to work with no API key. What *is* new on the voice side is that an
address arrives **spoken** -- "priya dot raman at gmail dot com" -- so a plain
address regex finds nothing at all in a call transcript.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# How an answer opens before the name arrives: a greeting, an acknowledgement
# of the question, or a politeness formula. Only "hi|hey|hello" used to be
# stripped, so "Sure, it's Dashawn Miller" was read as a caller called Sure --
# the wrong-name-on-a-profile failure this module exists to prevent.
#
# The ``\b`` is load-bearing: without it the greeting group ate the first two
# letters of "Hiro Tanaka".
_NAME_OPENER = (
    r"hi|hey|hello|good\s+(?:morning|afternoon|evening)"
    r"|yes|yeah|yep|yup|sure(?:\s+thing)?|of\s+course|certainly|absolutely"
    r"|definitely|no\s+problem|okay|ok|alright|right|well|um|uh|erm"
    r"|sorry|thanks|thank\s+you|please"
)
_NAME_LEAD = re.compile(
    # ``[,.;:!\s-]`` rather than ``[,\s]``: people put a full stop after the
    # filler. "Yeah. My name is Tushar" stripped to nothing under the
    # comma-only class, so a caller who answered the name question with an
    # ordinary two-sentence reply had no name read off the call at all.
    rf"^\s*(?:(?:{_NAME_OPENER})\b[,.;:!\s-]*)*"
    r"(?:(?:my\s+name\s+is|i\s*am|i'm|its|it's|this\s+is|name)\s*[:\-]?\s*)?",
    re.IGNORECASE,
)
_NAME_TRAILING_PUNCT = re.compile(r"[\s.,;:!?]+$")

# Words that are never part of a name, only of the sentence around one. The
# lead-in strip above handles an answer that *opens* with filler; this catches
# what is left when the filler is phrased some other way -- "that's right",
# "no problem", "one moment" -- each of which passes the shape test otherwise.
# Deliberately excludes short tokens that are real given names and surnames
# elsewhere in the world ("an", "no", "oh", "so", "ha"): the cost of the two
# lists disagreeing is a real person whose name we refuse.
_NOT_A_NAME = frozenset(
    """
    absolutely actually alright and anyway apologies certainly correct course
    definitely erm exactly for gotcha hello here hey hmm hold i i'm indeed
    is it it's its just me minute moment my name nope nah of okay ok perfect
    please problem right sec second sorry sure thank thanks that that's thats
    the there this to um uh understood wait we well what worries yeah yep yes
    you your yup
    """.split()  # noqa: SIM905 - one editable line per topic beats 80 quoted strings
)

MAX_NAME_WORDS = 4
MAX_NAME_CHARS = 80

# One batched question gets one batched answer: "Sathya, 2 yrs, 150k". Only the
# first segment carrying anything is ever considered for a name -- see
# ``first_segment``.
#
# ``and`` splits too, because a *spoken* batched answer has no commas: "Tushar
# and then tushar31 at gmail dot com" arrived as one segment, blew the
# four-word cap, and the name was thrown away on the very turn that answered
# the name question. No single person's name contains a bare "and", so the
# split costs nothing on the field it feeds.
_BATCH_SPLIT = re.compile(r"[,;\n]|\band\b", re.IGNORECASE)


def find_email(body: str) -> str:
    """A literally-written address, lowercased, or empty."""
    match = _EMAIL.search(body or "")
    return match.group(0).strip().lower() if match else ""


def find_name(body: str) -> str:
    """A name, or empty when the text does not plainly look like one.

    Only ever called on the answer to the name question, so the bar is "does
    this look like a name" rather than "find a name in this sentence". Empty is
    the safe answer: it costs one repeat of an easy question, whereas a wrong
    name reaches a client on a profile.
    """
    text = _EMAIL.sub(" ", body or "")
    text = _NAME_LEAD.sub("", text, count=1)
    text = _NAME_TRAILING_PUNCT.sub("", text).strip()
    if not text or len(text) > MAX_NAME_CHARS:
        return ""
    if any(ch.isdigit() or ch == "@" for ch in text):
        return ""
    words = text.split()
    if not 1 <= len(words) <= MAX_NAME_WORDS:
        return ""
    if not all(any(ch.isalpha() for ch in word) for word in words):
        return ""
    if any(word.strip(".'\"").lower() in _NOT_A_NAME for word in words):
        return ""
    return " ".join(words)


def first_segment(body: str) -> str:
    """The first segment that carries anything, before the next separator.

    The name is always the first thing in a batched answer, so this is where to
    look. Scanning every segment for the first name-shaped thing reads "150k,
    asap" as a candidate called Asap -- answering the other questions and
    skipping this one would put an invented name on a profile that goes in
    front of employers.

    A segment that is *nothing but* filler is skipped rather than returned,
    because "Sure, it's Dashawn Miller" is how people actually answer and
    stopping at "Sure" throws the name away. Skipping is not the loose scan
    above: a filler-only segment carries no answer to any question, so the next
    segment is still the first thing the caller said.
    """
    for segment in _BATCH_SPLIT.split(body or ""):
        if _NAME_LEAD.sub("", segment, count=1).strip(" .!?"):
            return segment
    return ""


# --------------------------------------------------------------------------
# spoken addresses
# --------------------------------------------------------------------------

# What a person says for a symbol. "at" is handled separately because it is
# also an ordinary English word, which is the whole difficulty here.
_SEPARATORS = {
    "dot": ".",
    "period": ".",
    "point": ".",
    "dash": "-",
    "hyphen": "-",
    "minus": "-",
    "underscore": "_",
    "underline": "_",
    "plus": "+",
}
# Only a separator when it is a spoken word on its own. The alternation is
# spliced in next to ``\s*``, so without these boundaries any name *containing*
# one of these substrings is torn apart mid-word and the leading ``\s*`` reaches
# back over the space to swallow the word before it: "my email is dashawn at
# gmail dot com" came out as "is-awn@gmail.com", which passes the address regex
# and so gets stored as a CRM row under a stranger's mailbox.
_SEP_WORD = (
    r"(?<![a-z0-9])(?:"
    + "|".join(sorted(_SEPARATORS, key=len, reverse=True))
    + r")(?![a-z0-9])"
)

# The last spoken token has to be a real suffix. Without this, "I worked at IBM
# dot com" is indistinguishable from an address, and so is the far more common
# "reach me at priya dot raman" -- where the *local* part gets read as a domain
# and the address comes out as "me@priya.raman". Requiring a known suffix at the
# end makes that candidate match fail outright, which lets the scan continue and
# find the real address later in the same sentence.
_SUFFIXES = (
    "com", "net", "org", "edu", "gov", "mil", "int",
    "io", "co", "ai", "dev", "app", "me", "info", "biz", "xyz", "tech", "cloud",
    "uk", "us", "ca", "au", "de", "fr", "es", "it", "nl", "se", "no", "fi", "dk",
    "ie", "in", "jp", "cn", "br", "mx", "pl", "pt", "ch", "at", "be", "nz", "za",
)
_SUFFIX = "|".join(sorted(_SUFFIXES, key=len, reverse=True))

_WORD = r"[a-z0-9]+"
_SPOKEN_EMAIL = re.compile(
    # Left boundary: not mid-word, and not already part of a written address.
    r"(?<![\w@.])"
    # Local part: words joined by spoken separators, or written ones.
    rf"(?P<local>{_WORD}(?:\s*(?:{_SEP_WORD}|[.\-_+])\s*{_WORD})*)"
    r"\s+(?:at|@)\s+"
    # Domain: at least one separator, and it must end in a known suffix.
    rf"(?P<domain>{_WORD}(?:\s*(?:{_SEP_WORD}|[.\-])\s*{_WORD})*"
    rf"\s*(?:{_SEP_WORD}|\.)\s*(?:{_SUFFIX}))"
    r"(?![\w@])",
    re.IGNORECASE,
)

# A spoken address is only read out of a turn that is *about* an address. The
# suffix rule above kills most prose, but "we're hosted at aws dot com" would
# still survive it, and a wrong address on a candidate record is mail to a
# stranger. Voice intake asks for the address explicitly, so the cue is
# reliably there -- on the candidate's own line, or on the agent's question
# immediately before it.
_EMAIL_CUE = re.compile(
    r"\b(e-?mail|email|address|inbox|reach me|reach you|contact me|contact you"
    r"|write to me|send it to|send me)\b",
    re.IGNORECASE,
)


def mentions_an_email(text: str) -> bool:
    """Is this turn about an email address at all?"""
    return bool(_EMAIL_CUE.search(text or ""))


def _assemble(local: str, domain: str) -> str:
    def collapse(part: str) -> str:
        out: list[str] = []
        for token in re.split(rf"\s*({_SEP_WORD}|[.\-_+])\s*|\s+", part, flags=re.IGNORECASE):
            if not token:
                continue
            lowered = token.lower()
            out.append(_SEPARATORS.get(lowered, lowered))
        return "".join(out)

    return f"{collapse(local)}@{collapse(domain)}"


# What may immediately precede the local part. A spoken address is introduced
# ("it's ...", "my email is ...", "reach me at ...") or it starts the turn; it
# does not appear with an unrelated word glued to its left.
#
# This is what stops the local part being read one word short. "priya raman at
# gmail dot com" has no separator between the two names, so the pattern can only
# match starting at "raman" -- and "raman@gmail.com" is not a near miss, it is
# somebody else's mailbox. Refusing to match at all is the only safe answer,
# because there is no way to tell whether the speaker meant "priyaraman" or
# "priya.raman". It also independently kills "we're hosted at aws dot com",
# where the word before the match is "we're".
#
# The correction openers -- "no", "not", "wrong", "actually", "wait" -- are in
# here because a caller rejecting a read-back says "No, it's actually
# tushar31gupta at gmail dot com", and "actually" sitting to the left of the
# address was enough to refuse the one turn whose whole purpose was to replace
# a misheard address.
_LEAD_INS = frozenset(
    """
    a about actually address am and at be best contact correction e-mail email
    get gmail have here hmm i i'm is it it's its just mail me my nah no not
    number of oh ok okay on one or put reach right send sends so sure that
    that's thats the then there this to try um uh use used uses using wait was
    we well what which will write wrong yeah yes you your
    """.split()  # noqa: SIM905 - one editable line per topic beats 70 quoted strings
)
# Must start with an actual word character. With ``-`` allowed as the *first*
# character too, the dash in "I've got that -- t-u-s-h-a-r at gmail dot com"
# was captured as the preceding "word", found not to be a lead-in, and the
# address refused -- the exact opposite of what ``_admissible_start`` documents
# for punctuation, which is that a dash means the address is being introduced.
_TRAILING_WORD = re.compile(r"([A-Za-z0-9][A-Za-z0-9'\-]*)\s*$")


def _admissible_start(text: str, start: int) -> bool:
    """Is the word to the left of ``start`` allowed to precede an address?"""
    before = text[:start]
    if not before.strip():
        return True
    match = _TRAILING_WORD.search(before)
    if match is None:
        # Punctuation, a colon, a dash: the address is being introduced.
        return True
    return match.group(1).lower() in _LEAD_INS


# Spoken digits, and the two-word compounds people say for a number in an
# address. "tushar three one gupta" and "tushar thirty one gupta" are the same
# mailbox said two ways, and neither survives a decoder that only reads [a-z0-9]
# tokens.
_UNITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_TEENS = {
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19",
}
_TENS = {
    "twenty": "2", "thirty": "3", "forty": "4", "fifty": "5",
    "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9",
}

# How TTS and dictation split the handful of consumer domains that cover most
# candidates. "at g mail dot com" is not a rare mishearing -- it is what the
# words sound like -- and without this the domain match starts at "mail" and
# quietly yields somebody@mail.com.
_SPOKEN_DOMAINS = (
    ("g mail", "gmail"), ("hot mail", "hotmail"), ("out look", "outlook"),
    ("i cloud", "icloud"), ("proton mail", "protonmail"), ("ya hoo", "yahoo"),
    ("y mail", "ymail"), ("fast mail", "fastmail"), ("live mail", "livemail"),
)

def _spoken_numbers(text: str) -> str:
    """"thirty one" and "three one" both become "31"."""
    words = text.split(" ")
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        bare = word.strip(".,;:!?").lower()
        if bare in _TENS:
            nxt = words[i + 1].strip(".,;:!?").lower() if i + 1 < len(words) else ""
            if nxt in _UNITS and nxt != "oh":
                out.append(_TENS[bare] + _UNITS[nxt])
                i += 2
                continue
            out.append(_TENS[bare] + "0")
        elif bare in _TEENS:
            out.append(_TEENS[bare])
        elif bare in _UNITS:
            out.append(_UNITS[bare])
        else:
            out.append(word)
        i += 1
    return " ".join(out)


def _collapse_spelling(text: str) -> str:
    """Join a run of spelled-out characters back into the word it spells.

    "t u s h a r three one g u p t a" is one local part, said the way this
    system's own prompt asks for it -- the agent is told to have the caller
    spell the part before the @. Before this, the run was read as thirteen
    separate words, no separator between any of them, and the decoder matched
    only the last one: "t u s h a r at gmail dot com" came out as
    ``r@gmail.com``. A stranger's mailbox on a candidate record is the exact
    failure this module exists to prevent, and it was being produced by the
    one input the prompt actively solicits.

    Two consecutive single characters are enough to count as spelling. Nobody
    means two separate words by "j p"; they mean "jp".

    Hyphens spell too, and want a longer run before they count. "t-u-s-h-a-r-3-1"
    is somebody spelling; "mary-jane" is a name and "x-1" could be either, so a
    hyphen run needs three characters where a space run needs two. This matters
    because the *agent* spells with hyphens -- the read-back it is required to
    perform arrives in the transcript as "t-u-s-h-a-r-3-1-g-u-p-t-a at gmail
    dot com", and that turn is what tells the scanner the next one is about an
    address.
    """
    spaced = re.sub(
        r"(?<![a-z0-9])(?:[a-z0-9] ){1,}[a-z0-9](?![a-z0-9])",
        lambda m: m.group(0).replace(" ", ""),
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?<![a-z0-9-])(?:[a-z0-9]-){2,}[a-z0-9](?![a-z0-9-])",
        lambda m: m.group(0).replace("-", ""),
        spaced,
        flags=re.IGNORECASE,
    )


def _normalise_spoken(text: str) -> str:
    """Put a spoken address into the shape the decoder's grammar expects."""
    out = " ".join((text or "").split())
    out = _spoken_numbers(out)
    out = _collapse_spelling(out)
    for spoken, written in _SPOKEN_DOMAINS:
        out = re.sub(rf"(?<![a-z0-9]){re.escape(spoken)}(?![a-z0-9])", written, out, flags=re.IGNORECASE)
    return out


def decode_spoken_email(text: str) -> str:
    """"priya dot raman at gmail dot com" -> "priya.raman@gmail.com".

    Returns the first well-formed address, or empty. Three things have to hold,
    and each one exists because of a specific way this goes wrong:

    * the last spoken token is a real suffix, or "reach me at priya dot raman"
      reads the *local* part as a domain and yields "me@priya.raman";
    * the word to the left is a lead-in (see ``_LEAD_INS``), or a local part
      with no spoken separator in it gets read one word short;
    * the assembled string satisfies the ordinary address regex, so a stray
      "dot dot" or an edge separator is dropped rather than stored.

    Deliberately not gated on the turn being *about* an address -- that is
    ``email_in_turn``'s job, and keeping the two apart makes the decoding
    testable on its own.
    """
    lowered = _normalise_spoken(text).lower()
    pos = 0
    while True:
        match = _SPOKEN_EMAIL.search(lowered, pos)
        if match is None:
            return ""
        pos = match.start() + 1
        if not _admissible_start(lowered, match.start()):
            continue
        address = _assemble(match.group("local"), match.group("domain"))
        if _EMAIL.fullmatch(address):
            return address


def email_in_turn(text: str, *, cued: bool = False) -> str:
    """An address from one conversational turn.

    A written address needs no cue -- nobody types ``a@b.com`` by accident. A
    spoken one is only trusted when the turn, or the question that prompted it,
    was about an address.
    """
    written = find_email(text)
    if written:
        return written
    if cued or mentions_an_email(text):
        return decode_spoken_email(text)
    return ""


# --------------------------------------------------------------------------
# transcript scanning
# --------------------------------------------------------------------------

# Retell renders a call as "Agent: ...\nCandidate: ...". A line with no speaker
# label is treated as the candidate's: the alternative is silently skipping a
# whole transcript when a provider changes its format, and the cue rules above
# are what actually keep the reading honest.
_SPEAKER = re.compile(r"^\s*(agent|assistant|ai|bot|user|candidate|caller|human)\s*[:\-]\s*", re.IGNORECASE)
_AGENT_SPEAKERS = frozenset({"agent", "assistant", "ai", "bot"})

_NAME_QUESTION = re.compile(
    r"\b(your name|who am i speaking|who's calling|who is calling|name should i"
    r"|what name|may i (?:take|have) your name|can i (?:take|have) your name)\b",
    re.IGNORECASE,
)


# How a caller opens the turn that rejects a read-back. Anchored at the start:
# "no" in the middle of a sentence is ordinary speech, "No, it's ..." is a
# correction, and only the second may overwrite an address already read.
#
# A short run of connective filler may precede the cue -- a real rejection
# came through as "And then t no. It's actually ...", where "And then" is the
# caller resuming their own sentence and a transcription stray sits between it
# and the "no". The filler run is bounded so a "no" buried mid-sentence still
# does not count as an opener.
_CORRECTION = re.compile(
    r"^\s*(?:(?:and|then|so|well|uh|um|oh|hmm|wait|hold\s+on|[a-z])\b[\s,.']*){0,4}"
    r"(?:no\b|nope\b|not\b|nah\b|wrong\b|that'?s\s+wrong|actually\b"
    r"|sorry\b|it'?s\s+not\b|almost\b|close\b)",
    re.IGNORECASE,
)


def _states_an_address(text: str) -> bool:
    """Does this turn actually contain an address, written or spoken?"""
    return bool(find_email(text) or decode_spoken_email(text))


def _turns(transcript: str) -> list[tuple[bool, str]]:
    """``(is_agent, text)`` per line, blank lines dropped."""
    turns: list[tuple[bool, str]] = []
    for raw in (transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _SPEAKER.match(line)
        if match is None:
            turns.append((False, line))
            continue
        speaker = match.group(1).lower()
        turns.append((speaker in _AGENT_SPEAKERS, line[match.end() :].strip()))
    return turns


def candidate_speech(transcript: str) -> str:
    """The caller's turns only.

    Heuristics have no speaker model, so a regex over the full transcript
    reads job titles and seniority out of the agent's greeting. Identity
    scanning still uses the full transcript because it keys off the question
    that prompted the answer.
    """
    return "\n".join(text for is_agent, text in _turns(transcript) if not is_agent)


def scan_transcript(transcript: str) -> tuple[str, str]:
    """``(name, email)`` the caller gave, from a call transcript.

    The first of each wins: a caller who corrects themselves says the new
    version in a later turn, but a caller whose name the agent *repeats* also
    produces a later turn, and there is no way to tell those apart from the
    text. Taking the first keeps the reading deterministic, and a correction is
    what the review queue is for.

    Both readings are anchored on the agent's question, for the same reason
    ``messaging.identity`` only reads a name from the answer to the name
    question: the heuristic path has no notion of who is speaking, and given a
    whole transcript it will happily read a job title out of our own greeting.
    """
    name = ""
    email = ""
    asked_name = False
    asked_email = False
    for is_agent, text in _turns(transcript):
        if is_agent:
            # An agent turn can ask for both at once, which is exactly what the
            # inbound prompt does: "your name and the email you use with us".
            asked_name = bool(_NAME_QUESTION.search(text))
            # Reading an address back is a question *about* that address, even
            # with no cue word in it. The shared rules make the read-back
            # mandatory -- spell the local part, wait for a yes -- so the turn
            # answering it is the likeliest place a corrected address appears,
            # and it was the one turn the cue rule refused to read.
            asked_email = mentions_an_email(text) or _states_an_address(text)
            continue
        if not email:
            email = email_in_turn(text, cued=asked_email)
        elif asked_email and _CORRECTION.match(text):
            # First-wins is right for a caller repeating themselves and wrong
            # for one saying "no, it's ...". The read-back exists precisely to
            # catch a mishearing, so the turn that rejects it has to be able to
            # win -- otherwise the record keeps the address the agent got
            # wrong, which is the misattribution this module exists to stop.
            corrected = email_in_turn(text, cued=True)
            if corrected:
                email = corrected
        if not name and asked_name:
            name = find_name(first_segment(text))
        # A question is answered once. Leaving the flags set would let the
        # caller's third sentence supply a name for a question two turns back.
        asked_name = False
        asked_email = False
    return name, email


__all__ = [
    "MAX_NAME_CHARS",
    "MAX_NAME_WORDS",
    "decode_spoken_email",
    "email_in_turn",
    "find_email",
    "find_name",
    "first_segment",
    "mentions_an_email",
    "scan_transcript",
]
