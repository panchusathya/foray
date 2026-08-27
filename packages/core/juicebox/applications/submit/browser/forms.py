"""Field maps, as data.

A browser adapter is a field map and a step order. Public Greenhouse / Lever /
Ashby / SmartRecruiters / Workable maps are gone -- those boards are filled
by the governed browser agent. What remains here is the two-leg Workday and
YC account/application maps, plus ``FormSpec`` so a cached derived spec from
an older row can still deserialize.

``juicebox ats-probe`` still loads these maps for the two-leg vendors.
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
#: Public boards no longer have handwritten maps; the browser agent observes
#: them live.
PROBEABLE: dict[str, FormSpec] = {
    "workday": WORKDAY_ACCOUNT,
    "yc": YC_ACCOUNT,
}

#: Public-form question discovery is the agent preflight. These maps used to
#: name vendors whose logged-out page *was* the application; that path is gone.
DISCOVERABLE: dict[str, FormSpec] = {}
