"""Historical FormSpec derivation from a page nobody mapped.

Public-form filling is the governed browser agent. This module remains so older
``JobPosting.form_spec`` rows deserialize, leftover ``jobs.formspec.one`` jobs
skip, and ``identity_key`` stays available to tests. It is not a submitter.

**A derived spec is a claim about a page we read once**, so every guard here
exists to make the false-positive cost zero rather than to maximise coverage:

* No resume-shaped file input, no spec. A mailing-list form has an email
  field; only an application form asks for a CV.
* Any password control, no spec. That is an account wall, and registering is
  the two-leg flow the account-gated vendors get by hand -- a derived spec
  typing a fresh password into a sign-in form is the disaster ``base.py``
  documents.
* A field with no selector is dropped, and if that drops email or the resume,
  no spec. ``driver.fill`` cannot address what enumeration could not name.
* The fill itself stays honest: a derived spec that stops matching ends the
  run as ``needs_human`` naming the field, exactly like a handwritten one.
"""

from __future__ import annotations

import logging
import re

from . import driver
from .driver import RunLog
from .forms import FormSpec
from .questions import _ENUMERATE_JS, FormQuestion, classify, questions_from_raw

logger = logging.getLogger(__name__)

#: What a derived spec labels itself in artifacts and logs. Deliberately not a
#: detect.py vendor: nothing routes on it, and inventing a URL-pattern vendor
#: for "unrecognised" would put a guess where detect keeps facts.
DERIVED_VENDOR = "derived"

#: Identity labels -> the logical keys a public-form fill already knows.
#: ``classify`` says a control IS identity; this says which field. Order
#: matters where fragments overlap ("first name" before "name").
_IDENTITY_KEYS: tuple[tuple[str, str], ...] = (
    ("first name", "first_name"),
    ("given name", "first_name"),
    ("last name", "last_name"),
    ("family name", "last_name"),
    ("surname", "last_name"),
    ("full name", "full_name"),
    ("your name", "full_name"),
    ("preferred name", "full_name"),
    ("email", "email"),
    ("e-mail", "email"),
    ("phone", "phone"),
    ("mobile", "phone"),
    ("linkedin", "linkedin"),
    ("cover letter", "cover_letter"),
    ("resume", "resume"),
    ("resumé", "resume"),
    ("curriculum", "resume"),
    (" cv", "resume"),
    ("location", "location"),
    ("city", "location"),
)

_RESUME_LABEL = re.compile(r"resum|curriculum|\bcv\b", re.IGNORECASE)

#: Selectors that mark a control as the candidate's own statement whatever its
#: label says. Found live: JazzHR's EEOC section labels its disability
#: signature box in a way that reads as a name field, and the first probe
#: mapped full_name onto #resumator-eeoc_disability_signature-value -- which
#: would have auto-signed a legal self-identification. The label lies; the
#: id does not.
_ATTESTATION_SELECTOR = re.compile(
    r"eeoc|disab|veteran|gender|race|ethnic|self.?id|signature", re.IGNORECASE
)
_COVER_LABEL = re.compile(r"cover\s*letter", re.IGNORECASE)

#: Button text that opens a collapsed or second-URL form.
_OPEN_TEXT = re.compile(r"apply|i'?m interested", re.IGNORECASE)
#: What may actually be FOLLOWED as an opener: anchored, because the loose
#: match reads marketing copy as a control -- mindpal.co's "Check if your
#: resume is ATS-ready before applying" was chosen and clicked live.
_OPENER_TEXT = re.compile(r"^\s*(apply\b|i'?m interested)", re.IGNORECASE)
#: Button text that sends it. "apply" is deliberately absent: on a page whose
#: form is already open, a leftover Apply control is navigation, not a send.
_SUBMIT_TEXT = re.compile(r"submit|send", re.IGNORECASE)


def identity_key(label: str, field_type: str = "text") -> str:
    """Which ``_values`` key an identity-classified control fills, or ``""``.

    Pure and table-driven, same testing contract as ``classify``. A file input
    is a document whatever else its label says: "upload your resume/CV here
    (PDF preferred)" is the resume field, not a text field about resumes.
    """
    bare = (label or "").strip().lower()
    if not bare:
        return ""
    if field_type == "file":
        if _COVER_LABEL.search(bare):
            return "cover_letter"
        if _RESUME_LABEL.search(bare):
            return "resume"
        return ""
    if bare == "name":
        return "full_name"
    padded = f" {bare} "
    for fragment, key in _IDENTITY_KEYS:
        if fragment in padded:
            return key
    return ""


#: Runs in the page. Candidate open/submit controls, which _ENUMERATE_JS
#: deliberately skips (it walks answerable fields). Hidden elements are
#: dropped here: a display:none Apply button is a template leftover, and
#: clicking it is how a run wanders off the page it was reading.
_BUTTONS_JS = """
() => {
  const out = [];
  const seen = new Set();
  const visible = (el) => {
    if (!el.offsetParent && el.tagName !== 'INPUT') return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const selectorFor = (el, text) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && el.type === 'submit') return "input[type='submit']";
    const short = text.replace(/"/g, '').slice(0, 60);
    return short ? `${tag}:has-text("${short}")` : '';
  };
  const controls = document.querySelectorAll(
    "button, input[type='submit'], a[role='button'], a"
  );
  for (const el of controls) {
    const text = (el.innerText || el.value || '').trim();
    if (!text || text.length > 80) continue;
    if (!/apply|interested|submit|send/i.test(text)) continue;
    if (!visible(el)) continue;
    const selector = selectorFor(el, text);
    if (!selector || seen.has(selector)) continue;
    seen.add(selector);
    out.push({
      text,
      selector,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      // An anchor's destination: the open-form control is a target=_blank
      // link more often than a same-page toggle, and a click on one opens a
      // tab the enumeration never reads. The href lets the caller follow it.
      href: el.tagName === 'A' ? (el.href || '') : '',
    });
  }
  return out;
}
"""


def _has_password(controls: list[dict]) -> bool:
    return any(str(c.get("fieldType") or "") == "password" for c in controls)


def _resume_control(controls: list[dict]) -> dict | None:
    """The file input the resume goes into, labelled ones first.

    The Ashby trap, generalised: boards put an *autofill* uploader above the
    form, and a bare first-file-input match parses the CV into the page while
    the real field stays empty. A file input whose label names the resume wins
    outright; the positional fallback only applies when no label anywhere does.
    """
    files = [
        c
        for c in controls
        if str(c.get("fieldType") or "") == "file" and str(c.get("selector") or "")
    ]
    if not files:
        return None
    for control in files:
        if _RESUME_LABEL.search(str(control.get("label") or "")):
            return control
    return files[0]


def _submit_selectors(buttons: list[dict]) -> tuple[str, ...]:
    """Send controls, most deliberate first.

    ``type=submit`` and submit/send wording outrank a bare "Apply": on a page
    whose form is open, Apply is usually the control that *opened* it, still
    rendered, and clicking it again navigates instead of sending.
    """
    typed = [b["selector"] for b in buttons if b.get("type") == "submit"]
    worded = [
        b["selector"]
        for b in buttons
        if b.get("type") != "submit" and _SUBMIT_TEXT.search(str(b.get("text") or ""))
    ]
    apply_only = [
        b["selector"]
        for b in buttons
        if b.get("type") != "submit"
        and not _SUBMIT_TEXT.search(str(b.get("text") or ""))
        and _OPEN_TEXT.search(str(b.get("text") or ""))
    ]
    return tuple(dict.fromkeys(typed + worded + apply_only))


def _openers(buttons: list[dict]) -> list[dict]:
    """Controls that plausibly open the form, most deliberate first.

    Anchored on purpose: the loose ``_OPEN_TEXT`` is fine for *ordering*
    submit candidates, but following a control means trusting it, and
    "…before applying" marketing copy must not qualify.
    """
    return [b for b in buttons if _OPENER_TEXT.search(str(b.get("text") or ""))]


def _open_selectors(buttons: list[dict]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(b["selector"] for b in _openers(buttons)))


def derive_spec(
    controls: list[dict],
    buttons: list[dict],
    *,
    open_form: tuple[str, ...] = (),
    vendor: str = DERIVED_VENDOR,
) -> FormSpec | None:
    """A ``FormSpec`` from one page's enumeration, or ``None``.

    ``controls`` is ``_ENUMERATE_JS`` output verbatim; ``buttons`` is
    ``_BUTTONS_JS`` output from the same view. Pure, so a saved enumeration
    tests the mapping without a browser.

    ``None`` is the ordinary answer, not a failure: it means this page did not
    show us an application form we could honestly promise to fill.
    """
    if _has_password(controls):
        logger.info("not deriving: page has a password control (account wall)")
        return None

    resume = _resume_control(controls)
    if resume is None:
        logger.info("not deriving: no resume-shaped file input on the page")
        return None

    fields: dict[str, tuple[str, ...]] = {"resume": (str(resume["selector"]),)}
    required: set[str] = set()
    files: set[str] = {"resume"}

    for control in controls:
        field_type = str(control.get("fieldType") or "text")
        selector = str(control.get("selector") or "")
        label = str(control.get("label") or "")
        if control is resume or not selector:
            continue
        if _ATTESTATION_SELECTOR.search(selector):
            continue
        kind, _ = classify(label)
        if kind != "identity":
            continue
        key = identity_key(label, field_type)
        if not key or key in fields:
            continue
        fields[key] = (selector,)
        if field_type == "file":
            files.add(key)
        if control.get("required"):
            required.add(key)

    if "email" not in fields:
        logger.info("not deriving: no email field survived mapping")
        return None

    submit = _submit_selectors(buttons)
    if not submit:
        logger.info("not deriving: no submit control found")
        return None

    # What we always fill, whatever the page marked. An application sent
    # without a name or address is not one anybody can respond to.
    required.update({"email", "resume"})
    if "full_name" in fields:
        required.add("full_name")
    if "first_name" in fields:
        required.add("first_name")
        if "last_name" in fields:
            required.add("last_name")

    landmark = tuple(dict.fromkeys((fields["email"][0], fields["resume"][0])))
    return FormSpec(
        vendor=vendor,
        landmark=landmark,
        open_form=open_form,
        fields=fields,
        required=frozenset(required),
        files=frozenset(files),
        submit=submit,
    )


def inspect_form(
    url: str, *, vendor: str = "", log: RunLog | None = None
) -> tuple[FormSpec | None, list[FormQuestion]]:
    """Read one posting's page and derive its spec and questions. Read-only.

    One page view, two enumeration passes at most: most boards render the
    posting with the form collapsed or on a second URL, so when the first pass
    finds an apply-like control but no resume input, opening the form is a
    navigation, not an application -- the same line ``probe`` draws.

    Raises ``BrowserUnavailable`` when there is no Playwright, and lets
    navigation errors propagate: the caller owns the difference between
    "checked, no form" (final) and "could not check" (retry).
    """
    log = log or RunLog()
    opened: tuple[str, ...] = ()
    with driver.session(log=log) as page:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        controls = page.evaluate(_ENUMERATE_JS)
        buttons = page.evaluate(_BUTTONS_JS)
        if _resume_control(controls) is None:
            openers = _openers(buttons)
            href = next(
                (str(b.get("href") or "") for b in openers if b.get("href")), ""
            )
            selectors = tuple(dict.fromkeys(b["selector"] for b in openers))
            if href.startswith(("http://", "https://")):
                # The open-form control is a link -- and live it is routinely
                # target=_blank (DigitalHire), so a click opens a tab this
                # page object never reads. Following the href is the same
                # navigation the click promised, on the page we are watching.
                log.step(f"following apply link {href[:120]}")
                page.goto(href, wait_until="domcontentloaded")
                opened = selectors[:1]
            elif selectors and driver.click(page, selectors, log=log, what="open form"):
                opened = selectors
            if opened:
                # The form is a React tree more often than not, and its
                # controls do not exist at navigation time -- the post-click
                # sleep lesson from the first probe runs.
                page.wait_for_timeout(2500)
                controls = page.evaluate(_ENUMERATE_JS)
                buttons = page.evaluate(_BUTTONS_JS)
    spec = derive_spec(controls, buttons, open_form=opened, vendor=vendor or DERIVED_VENDOR)
    return spec, questions_from_raw(controls, url)
