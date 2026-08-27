"""Field maps, as data.

A browser adapter is a field map and a step order. Keeping the map *declarative*
rather than buried in each adapter's code buys the one thing this tier actually
needs: ``juicebox ats-probe`` can load the very same map, point a browser at a
real posting, and report which selectors still resolve. When a vendor reshapes
their form -- the failure mode this tier has, and it is not rare -- the fix is
editing one tuple with the probe's output in front of you.

**These maps are the part most likely to be wrong.** Selectors here were written
from the vendors' published form markup, not from a probe run, and until
``ats-probe`` has been pointed at a live posting they should be read as a
starting point. That is survivable because of how ``driver.fill`` fails: a
required field whose selectors all miss ends the run as ``needs_human`` naming
the field, rather than submitting something half-filled. A wrong map costs an
application we did not send; it never costs one we sent wrongly.

Selector order is preference order. Vendors ship several generations of the same
form and the tenant decides which one you get, so each field lists the most
specific hook first and the generic input name last.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FormSpec:
    """One vendor's hosted application form."""

    vendor: str
    #: logical field name -> selectors to try, in order
    fields: dict[str, tuple[str, ...]]
    #: fields whose absence ends the run rather than being skipped
    required: frozenset[str]
    #: the control that sends it
    submit: tuple[str, ...]
    #: proof we are on an application form at all, rather than a listing page,
    #: a cookie wall, or a "this job is closed" page. Checked before filling.
    landmark: tuple[str, ...]
    #: a control that opens the form when the posting renders it collapsed
    open_form: tuple[str, ...] = ()
    #: fields that take a file path rather than text
    files: frozenset[str] = field(default_factory=frozenset)

    def selectors_for(self, key: str) -> tuple[str, ...]:
        return self.fields.get(key, ())

    def to_dict(self) -> dict:
        """JSON-safe form, for the derived specs cached on a posting row.

        Tuples and frozensets do not survive a JSON column; this is the seam
        that keeps the dataclass itself honest while the storage stays plain.
        """
        return {
            "vendor": self.vendor,
            "fields": {key: list(selectors) for key, selectors in self.fields.items()},
            "required": sorted(self.required),
            "submit": list(self.submit),
            "landmark": list(self.landmark),
            "open_form": list(self.open_form),
            "files": sorted(self.files),
        }

    @classmethod
    def from_dict(cls, data: dict) -> FormSpec | None:
        """The cached form back into a spec, or ``None`` for anything unusable.

        Deliberately forgiving: a row written by an older shape of ``to_dict``
        must degrade to "no derived spec" rather than a 500 on every package
        build that touches the posting.
        """
        if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
            return None
        fields = {
            str(key): tuple(str(s) for s in selectors)
            for key, selectors in data["fields"].items()
            if isinstance(selectors, (list, tuple)) and selectors
        }
        submit = tuple(str(s) for s in data.get("submit") or [])
        if not fields or not submit:
            return None
        return cls(
            vendor=str(data.get("vendor") or "derived"),
            fields=fields,
            required=frozenset(str(k) for k in data.get("required") or []),
            submit=submit,
            landmark=tuple(str(s) for s in data.get("landmark") or []),
            open_form=tuple(str(s) for s in data.get("open_form") or []),
            files=frozenset(str(k) for k in data.get("files") or []),
        )


GREENHOUSE = FormSpec(
    vendor="greenhouse",
    landmark=("#application_form", "form#application-form", "[data-ui='application-form']"),
    open_form=("a#apply_button", "button:has-text('Apply')", "a:has-text('Apply for this job')"),
    fields={
        "first_name": ("#first_name", "input[name='job_application[first_name]']"),
        "last_name": ("#last_name", "input[name='job_application[last_name]']"),
        "email": ("#email", "input[name='job_application[email]']", "input[type='email']"),
        "phone": ("#phone", "input[name='job_application[phone]']"),
        "resume": (
            "input#resume[type='file']",
            "input[name='job_application[resume]']",
            "input[type='file']",
        ),
        "cover_letter": (
            "input#cover_letter[type='file']",
            "input[name='job_application[cover_letter]']",
        ),
        "linkedin": (
            "input[name*='urls[LinkedIn]']",
            "input[aria-label*='LinkedIn' i]",
        ),
    },
    required=frozenset({"first_name", "last_name", "email", "resume"}),
    files=frozenset({"resume", "cover_letter"}),
    submit=("#submit_app", "input[type='submit']", "button:has-text('Submit application')"),
)

LEVER = FormSpec(
    vendor="lever",
    landmark=("form.application-form", ".application-page", "form[action*='apply']"),
    open_form=("a.postings-btn", "a:has-text('Apply for this job')", "a[href$='/apply']"),
    fields={
        # Lever takes one name field, not two.
        "full_name": ("input[name='name']", "input#name"),
        "email": ("input[name='email']", "input#email", "input[type='email']"),
        "phone": ("input[name='phone']", "input#phone"),
        "resume": ("input[name='resume']", "input[type='file']"),
        "linkedin": ("input[name='urls[LinkedIn]']", "input[name*='LinkedIn']"),
        "comments": ("textarea[name='comments']",),
    },
    required=frozenset({"full_name", "email", "resume"}),
    files=frozenset({"resume"}),
    submit=(
        "button[type='submit'].template-btn-submit",
        "button:has-text('Submit application')",
        "button[type='submit']",
    ),
)

ASHBY = FormSpec(
    vendor="ashby",
    # Ashby renders no <form> element at all -- the application is a React tree
    # on a client-side /application route. A landmark of "form" therefore never
    # matched, which `hosted_form` reads as "no application form on this page"
    # and refuses the run. The landmark has to be a field we know is there.
    landmark=(
        "input[name='_systemfield_email']",
        "input[name='_systemfield_name']",
        "[class*='ashby-application-form']",
    ),
    open_form=("button:has-text('Apply for this Job')", "a:has-text('Apply')"),
    fields={
        # Ashby's inputs carry `_systemfield_*` names; the accessible name is
        # null on the current board, so aria-label is the fallback, not the
        # first guess.
        "full_name": ("input[name='_systemfield_name']", "input[aria-label='Name']"),
        "email": ("input[name='_systemfield_email']", "input[aria-label='Email']"),
        "phone": ("input[name='_systemfield_phone']", "input[aria-label='Phone']"),
        # The resume input must be addressed by id. Ashby puts an *autofill*
        # file input above the form: a bare `input[type=file]` matches that one
        # first, which parses the CV into the page and leaves the actual resume
        # field empty -- a required-field stall that looks like a wrong answer.
        "resume": (
            "#_systemfield_resume",
            "input[name='_systemfield_resume']",
            "input[type='file']",
        ),
        "linkedin": ("input[aria-label*='LinkedIn' i]",),
    },
    required=frozenset({"full_name", "email", "resume"}),
    files=frozenset({"resume"}),
    submit=("button:has-text('Submit Application')", "button[type='submit']"),
)

WORKDAY_ACCOUNT = FormSpec(
    vendor="workday",
    landmark=('[data-automation-id="createAccountLink"]', '[data-automation-id="email"]'),
    open_form=(
        'a[data-automation-id="adventureButton"]',
        'button[data-automation-id="applyManually"]',
        "a:has-text('Apply')",
    ),
    fields={
        "email": ('input[data-automation-id="email"]', "input[type='email']"),
        "password": ('input[data-automation-id="password"]',),
        "verify_password": ('input[data-automation-id="verifyPassword"]',),
        "terms": ('input[data-automation-id="createAccountCheckbox"]',),
        # The code leg, when a tenant verifies by one-time code rather than a
        # link. Absent on tenants that email a link, which is why it is not
        # required.
        "otp": (
            'input[data-automation-id="verificationCode"]',
            'input[data-automation-id="otp"]',
            "input[autocomplete='one-time-code']",
        ),
    },
    required=frozenset({"email", "password"}),
    submit=('button[data-automation-id="signInSubmitButton"]', "button:has-text('Sign In')"),
)

WORKDAY_APPLICATION = FormSpec(
    vendor="workday",
    landmark=('[data-automation-id="legalNameSection_firstName"]', "input[type='file']"),
    fields={
        "first_name": ('input[data-automation-id="legalNameSection_firstName"]',),
        "last_name": ('input[data-automation-id="legalNameSection_lastName"]',),
        "phone": ('input[data-automation-id="phone-number"]',),
        "resume": (
            'input[data-automation-id="file-upload-input-ref"]',
            "input[type='file']",
        ),
        "linkedin": ('input[data-automation-id="linkedinQuestion"]',),
    },
    required=frozenset({"first_name", "last_name", "resume"}),
    files=frozenset({"resume"}),
    submit=(
        'button[data-automation-id="bottom-navigation-next-button"]',
        'button[data-automation-id="wd-CommandButton_uic_okButton"]',
        "button:has-text('Submit')",
    ),
)

SMARTRECRUITERS = FormSpec(
    vendor="smartrecruiters",
    # Unprobed starting point (see the module docstring): written from the
    # vendor's published markup, to be corrected with `ats-probe` output. The
    # apply flow is a React app on /oneclick-ui/ reached from the posting's
    # "I'm interested" control.
    landmark=(
        "input[name='firstName']",
        "form[class*='application']",
        "[data-test='application-form']",
    ),
    open_form=(
        "a:has-text(\"I'm interested\")",
        "button:has-text(\"I'm interested\")",
        "a:has-text('Apply')",
        "button:has-text('Apply')",
    ),
    fields={
        "first_name": ("input[name='firstName']", "input[id*='first' i]"),
        "last_name": ("input[name='lastName']", "input[id*='last' i]"),
        "email": ("input[name='email']", "input[type='email']"),
        "phone": ("input[name='phoneNumber']", "input[type='tel']"),
        "resume": ("input[type='file']",),
    },
    required=frozenset({"first_name", "last_name", "email", "resume"}),
    files=frozenset({"resume"}),
    submit=(
        "button[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Apply')",
    ),
)

WORKABLE = FormSpec(
    vendor="workable",
    # Unprobed starting point. Workable's hosted form is one page at
    # /<tenant>/j/<id>/apply and stamps stable data-ui hooks on its controls.
    landmark=(
        "form[data-ui='application-form']",
        "input[name='firstname']",
        "[data-ui='application-form']",
    ),
    open_form=("a[data-ui='overview-apply-now']", "a:has-text('Apply')"),
    fields={
        "first_name": ("input[name='firstname']", "input[id='firstname']"),
        "last_name": ("input[name='lastname']", "input[id='lastname']"),
        "email": ("input[name='email']", "input[type='email']"),
        "phone": ("input[name='phone']", "input[type='tel']"),
        # Workable renders a drag-drop uploader; the real input hides inside.
        "resume": ("[data-ui='resume'] input[type='file']", "input[type='file']"),
        "cover_letter": ("textarea[name='cover_letter']",),
    },
    required=frozenset({"first_name", "last_name", "email", "resume"}),
    files=frozenset({"resume"}),
    submit=("button[data-ui='apply-button']", "button[type='submit']"),
)

YC_ACCOUNT = FormSpec(
    vendor="yc",
    # Unprobed starting point. Work at a Startup runs on the unified YC
    # account (account.ycombinator.com); a logged-out Apply lands on its
    # sign-up form. Same two-leg shape as Workday: register, park on the
    # verification mail, and browser/yc.py signs in for the application leg.
    landmark=(
        "input[name='password']",
        "input[id='sign-up-password']",
        "form[action*='sign' i]",
    ),
    open_form=(
        "a:has-text('Sign up')",
        "a:has-text('Create account')",
        "a:has-text('Apply')",
        "button:has-text('Apply')",
    ),
    fields={
        "first_name": ("input[name='first_name']", "input[id*='first' i]"),
        "last_name": ("input[name='last_name']", "input[id*='last' i]"),
        "email": ("input[name='email']", "input[type='email']"),
        "password": ("input[name='password']", "input[type='password']"),
    },
    required=frozenset({"email", "password"}),
    submit=(
        "button[type='submit']",
        "button:has-text('Sign up')",
        "button:has-text('Create account')",
    ),
)

YC_APPLICATION = FormSpec(
    vendor="yc",
    # The signed-in apply on a WaaS job page: an Apply control opens a note
    # box ("why you're a good fit") and Send files it with the WaaS profile.
    # The note is the one free-text field, and it carries the reviewed
    # why_company draft -- never invented here (docgen/answers owns that).
    landmark=(
        "textarea",
        "button:has-text('Send')",
    ),
    open_form=("button:has-text('Apply')", "a:has-text('Apply')"),
    fields={
        "comments": ("textarea",),
    },
    required=frozenset(),
    submit=("button:has-text('Send')", "button[type='submit']"),
)

#: What ``ats-probe`` can check, by vendor. Workday's second form sits behind a
#: login, so only the account step is probeable without an account -- and YC's
#: application leg likewise, so its probeable face is the sign-up form.
PROBEABLE: dict[str, FormSpec] = {
    "greenhouse": GREENHOUSE,
    "lever": LEVER,
    "ashby": ASHBY,
    "workday": WORKDAY_ACCOUNT,
    "smartrecruiters": SMARTRECRUITERS,
    "workable": WORKABLE,
    "yc": YC_ACCOUNT,
}

#: Vendors whose *reachable* page is the application form itself, which is
#: what question discovery (``applications/questions.py``) needs. Workday and
#: YC gate theirs behind an account, so the page a logged-out browser reaches
#: is a registration form -- "discovering" questions there would put
#: "password" in front of the candidate as something the employer asked.
DISCOVERABLE: dict[str, FormSpec] = {
    "greenhouse": GREENHOUSE,
    "lever": LEVER,
    "ashby": ASHBY,
    "smartrecruiters": SMARTRECRUITERS,
    "workable": WORKABLE,
}
