"""Read a form's actual questions, so the package can answer what it asks.

The standard answer set (``docgen/answers``) covers the four questions almost
every application shares. But boards let the employer add their own — Ashby
renders UUID-named text inputs and required Yes/No radios, Greenhouse tenants
bolt on anything — and until now those were invisible: the package filled the
standard fields, the custom questions were never seen, and a required one just
stalled the run at submit time with nobody having been asked anything.

This module is the seeing half. ``discover`` navigates the posting the same
way ``ats-probe`` does — read-only, one page view, never fills, never submits —
and returns every control the form would make us answer, with its label,
selector, required flag and options. ``classify`` sorts each into one of four
kinds, and the kind decides who answers it:

* **identity** — name, email, resume: the adapter fills these from the record.
* **standard** — comp, notice, work auth, why-company: ``docgen/answers``
  already builds these.
* **attestation** — EEO self-ID, veteran and disability status, "have you
  worked here before": **never** generated, never asked over SMS. These are
  the candidate's own statements and the only hand that fills them is theirs,
  in the portal. A wrong cover letter is embarrassing; a wrong attestation is
  a different category of problem.
* **custom** — everything else: left blank, named to the candidate, answered
  in the portal.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from . import driver
from .driver import RunLog
from .forms import PROBEABLE

logger = logging.getLogger(__name__)

KIND_IDENTITY = "identity"
KIND_STANDARD = "standard"
KIND_ATTESTATION = "attestation"
KIND_CUSTOM = "custom"

#: Whole labels that are identity when exact. "name" as a *fragment* would
#: swallow "company name you most admire"; as the entire label it is the
#: name field.
_IDENTITY_EXACT = frozenset({"name", "location", "address", "website"})

#: Whole labels that are an option, not a question. A checkbox whose best
#: label resolves to "Yes" cannot be named to a person, so it is skipped the
#: same way an unlabeled control is.
_OPTION_LABELS = frozenset({"yes", "no", "true", "false", "y", "n"})

#: Label fragments that mean the adapter already fills this from the record.
_IDENTITY_MARKERS = (
    "first name", "last name", "full name", "preferred name", "your name",
    "email", "e-mail", "phone", "mobile",
    "resume", "resumé", "curriculum", " cv",
    "cover letter",
    "linkedin",
    "location", "city", "current company", "current employer", "current title",
)

#: Label fragment -> the standard answer key that already covers it.
_STANDARD_MARKERS: tuple[tuple[str, str], ...] = (
    ("salary", "salary_expectation"),
    ("compensation", "salary_expectation"),
    ("comp expectation", "salary_expectation"),
    ("pay expectation", "salary_expectation"),
    ("notice period", "notice_period"),
    ("start date", "notice_period"),
    ("when can you start", "notice_period"),
    ("when could you start", "notice_period"),
    ("available to start", "notice_period"),
    ("sponsorship", "work_authorization"),
    ("authorized to work", "work_authorization"),
    ("authorised to work", "work_authorization"),
    ("eligible to work", "work_authorization"),
    ("require visa", "work_authorization"),
    ("work visa", "work_authorization"),
    ("citizen", "work_authorization"),
    ("why do you want", "why_company"),
    ("why are you interested", "why_company"),
    ("why this role", "why_company"),
    ("interest in this", "why_company"),
)

#: Label fragments that make a question the candidate's own statement.
#: Deliberately broad: misfiling a custom question as an attestation costs a
#: portal visit; misfiling an attestation as custom risks answering it.
_ATTESTATION_MARKERS = (
    "gender", "race", "ethnic", "hispanic", "latino", "latinx",
    "veteran", "disability", "disabilities", "disabled",
    "eeo", "self-identify", "self identify", "self-identification",
    "sexual orientation", "transgender", "pronoun",
    "criminal", "conviction", "felony",
    "previously worked", "previously employed", "ever worked for",
    "ever been employed", "worked here before",
    "18 years", "age of 18", "over 18",
    "background check",
)


@dataclass(frozen=True)
class FormQuestion:
    """One thing the form would make us answer."""

    key: str
    label: str
    kind: str
    #: text | textarea | select | radio | checkbox | file
    field_type: str = "text"
    selector: str = ""
    required: bool = False
    options: tuple[str, ...] = ()
    #: For kind=standard: which existing answer key covers it.
    standard_key: str = ""


def classify(label: str) -> tuple[str, str]:
    """``(kind, standard_key)`` for one label. Pure; the tests live here.

    Attestations are checked first: "gender (voluntary self-identification)"
    must not fall through to custom however the rest of the label reads, and a
    label mentioning both an identity word and an attestation word ("veteran
    status of your... contact"? unlikely, but) errs on the side of attestation.
    """
    bare = (label or "").strip().lower()
    lowered = f" {bare} "
    if not bare:
        return KIND_CUSTOM, ""
    if bare in _IDENTITY_EXACT:
        return KIND_IDENTITY, ""
    for marker in _ATTESTATION_MARKERS:
        if marker in lowered:
            return KIND_ATTESTATION, ""
    for marker, key in _STANDARD_MARKERS:
        if marker in lowered:
            return KIND_STANDARD, key
    for marker in _IDENTITY_MARKERS:
        if marker in lowered:
            return KIND_IDENTITY, ""
    return KIND_CUSTOM, ""


def _clean_label(label: str) -> str:
    """The question out of whatever text the DOM walk swept up.

    A React board's section container answers ``innerText`` with the heading,
    the prompt and every option glued together by newlines -- seen live on
    Ashby, where the ethnicity group's label arrived as the entire gender
    section. The first non-empty line is the question; the rest is furniture.
    """
    for line in (label or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def question_key(label: str) -> str:
    """A stable key for one discovered question.

    Derived from the normalized label rather than the DOM position, so a
    re-discovery after the candidate answered maps back to the same entry and
    their answer survives the merge.
    """
    normalized = re.sub(r"\s+", " ", (label or "").strip().lower())
    digest = hashlib.sha1(normalized.encode()).hexdigest()[:10]
    return f"q_{digest}"


#: Runs in the page. Returns every answerable control with the best label the
#: DOM offers. Radios collapse to one entry per name with their options.
_ENUMERATE_JS = """
() => {
  const seen = new Set();
  const out = [];

  const labelFor = (el) => {
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && lab.innerText.trim()) return lab.innerText.trim();
    }
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const text = labelled.split(/\\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean).map((node) => node.innerText.trim()).join(' ').trim();
      if (text) return text;
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping.innerText.trim()) return wrapping.innerText.trim();
    // Walk up a few levels and take the first text that is not our own value:
    // React boards wrap each field in a div whose first child is the prompt.
    let node = el.parentElement;
    for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
      for (const child of node.children) {
        if (child === el || child.contains(el)) continue;
        const text = (child.innerText || '').trim();
        if (text && text.length < 300) return text;
      }
    }
    return (el.getAttribute('placeholder') || '').trim();
  };

  const isRequired = (el, label) =>
    el.required || el.getAttribute('aria-required') === 'true' ||
    /[*\\u2731\\u2733]/.test(label) || /\\brequired\\b/i.test(label);

  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    // React boards routinely render controls with neither id nor name. An
    // attribute the page itself uses to address the element is the next best
    // handle -- without one the control is real but unfillable, and a derived
    // spec that keeps it would stall the run on a selector that matches nothing.
    const tag = el.tagName.toLowerCase();
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return `${tag}[aria-label="${CSS.escape(aria.trim())}"]`;
    const testid = el.getAttribute('data-testid');
    if (testid) return `${tag}[data-testid="${CSS.escape(testid)}"]`;
    const placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) {
      return `${tag}[placeholder="${CSS.escape(placeholder.trim())}"]`;
    }
    return '';
  };

  const controls = document.querySelectorAll('input, textarea, select');
  const radios = {};
  for (const el of controls) {
    const type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;

    if (type === 'radio') {
      const name = el.name || '';
      if (!name) continue;
      const group = radios[name] = radios[name] || {
        name, options: [], required: false, groupLabel: '',
      };
      const optionLabel = labelFor(el);
      if (optionLabel) group.options.push(optionLabel);
      group.required = group.required || el.required ||
        el.getAttribute('aria-required') === 'true';
      if (!group.groupLabel) {
        const fieldset = el.closest('fieldset');
        const legend = fieldset && fieldset.querySelector('legend');
        if (legend && legend.innerText.trim()) {
          group.groupLabel = legend.innerText.trim();
        } else {
          // The question is usually the first text above the option row.
          let node = el.parentElement;
          for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
            const prev = node.previousElementSibling;
            const text = prev && (prev.innerText || '').trim();
            if (text && text.length < 300 && !group.options.includes(text)) {
              group.groupLabel = text;
              break;
            }
          }
        }
      }
      continue;
    }

    const label = labelFor(el);
    const selector = selectorFor(el);
    if (!selector && !label) continue;
    const dedupe = selector || label;
    if (seen.has(dedupe)) continue;
    seen.add(dedupe);
    const fieldType = type === 'textarea' ? 'textarea'
      : type === 'select' ? 'select'
      : type === 'file' ? 'file'
      : type === 'checkbox' ? 'checkbox'
      : type === 'password' ? 'password'
      : 'text';
    const options = type === 'select'
      ? Array.from(el.options).map((o) => o.label.trim()).filter(Boolean)
      : [];
    out.push({
      label, selector, fieldType, options,
      required: isRequired(el, label),
    });
  }

  for (const group of Object.values(radios)) {
    out.push({
      label: group.groupLabel,
      selector: `input[type="radio"][name="${group.name}"]`,
      fieldType: 'radio',
      options: group.options,
      required: group.required,
    });
  }
  return out;
}
"""


def discover(url: str, *, vendor: str = "") -> list[FormQuestion]:
    """Every answerable control on one posting's form. Read-only.

    Raises ``BrowserUnavailable`` when there is no Playwright — the caller
    decides whether that degrades or fails, because a package build without a
    browser is fine and a probe without one is not.
    """
    from ...ats.detect import detect_target

    spec = PROBEABLE.get(vendor or detect_target(url).vendor)
    log = RunLog()
    raw: list[dict] = []
    with driver.session(log=log) as page:
        page.goto(url, wait_until="domcontentloaded")
        if spec is not None:
            driver.reach_form(page, landmark=spec.landmark, open_form=spec.open_form, log=log)
        raw = page.evaluate(_ENUMERATE_JS)

    return questions_from_raw(raw, url)


def questions_from_raw(raw: list[dict], url: str) -> list[FormQuestion]:
    """The post-processing half of ``discover``, shared with ``derive``.

    Password controls are dropped here rather than surfaced: a password is
    never the employer's question, and letting one through would classify it
    as custom and put "Password" in front of a candidate as something to
    answer over text. The *presence* of one still matters — ``derive`` reads
    it off the raw list as proof of an account wall.
    """
    questions: list[FormQuestion] = []
    seen_keys: set[str] = set()
    for item in raw:
        if str(item.get("fieldType") or "") == "password":
            logger.info("password control on %s skipped", url)
            continue
        label = _clean_label(str(item.get("label") or ""))
        if label.lower() in _OPTION_LABELS:
            # The label walk found an option ("Yes"), not the question. A
            # control we cannot name to a person is one we cannot ask about.
            logger.info("option-labeled %s control on %s skipped", item.get("fieldType"), url)
            continue
        kind, standard_key = classify(label)
        key = standard_key if kind == KIND_STANDARD else question_key(label)
        if not label and kind == KIND_CUSTOM:
            # An unlabeled control we cannot name to a person is one we cannot
            # ask anybody about. Logged so a probe run can chase it.
            logger.info("unlabeled %s control on %s skipped", item.get("fieldType"), url)
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        questions.append(
            FormQuestion(
                key=key,
                label=label,
                kind=kind,
                field_type=str(item.get("fieldType") or "text"),
                selector=str(item.get("selector") or ""),
                required=bool(item.get("required")),
                options=tuple(str(o) for o in (item.get("options") or [])),
                standard_key=standard_key,
            )
        )
    return questions
