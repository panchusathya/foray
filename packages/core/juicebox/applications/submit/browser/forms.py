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
    landmark=("form._form_hidden", "[class*='ashby-application-form']", "form"),
    open_form=("button:has-text('Apply for this Job')", "a:has-text('Apply')"),
    fields={
        # Ashby's board is React-rendered and its inputs are labelled rather
        # than named, so these lean on the accessible name.
        "full_name": ("input[aria-label='Name']", "input[name='_systemfield_name']"),
        "email": ("input[aria-label='Email']", "input[name='_systemfield_email']"),
        "phone": ("input[aria-label='Phone']", "input[name='_systemfield_phone']"),
        "resume": ("input[type='file']",),
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

#: What ``ats-probe`` can check, by vendor. Workday's second form sits behind a
#: login, so only the account step is probeable without an account.
PROBEABLE: dict[str, FormSpec] = {
    "greenhouse": GREENHOUSE,
    "lever": LEVER,
    "ashby": ASHBY,
    "workday": WORKDAY_ACCOUNT,
}
