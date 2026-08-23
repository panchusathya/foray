"""Transcripts and scripted extractor output for the voice intake tests.

Two calls. ``COMPLETE_*`` is the cooperative one that closes the gate — the case
the two-to-three-minute target is written for. ``SPARSE_*`` is the hurried
caller who answers four things and refuses a fifth, which must leave the gate
open so the agent keeps going.

The transcripts are deliberately messy where the taxonomy has to do work: "Sr.
ML Engineer", "fin tech", "SF", "Series B". If those arrive at the database in
that form they cannot be scored against a parsed job description, which is the
entire point of reusing ``ParsedQuery``.
"""

from __future__ import annotations

COMPLETE_TRANSCRIPT = """\
Agent: Hi, this is the intake line for Acme Talent. I'm an AI assistant, and this
call is recorded so a recruiter can read it later — is that OK with you?
Candidate: Yeah, that's fine.
Agent: Great, this should take two or three minutes. What kind of role are you
looking for next?
Candidate: I'm after a Sr. ML Engineer role. Senior, staying hands-on — I don't
want to end up managing people.
Agent: Got it. Where are you based, and would you want to be in an office?
Candidate: I'm in SF. I'd do hybrid, a couple of days in the office is fine, but
fully remote isn't what I want.
Agent: What do you want to be hired to actually do day to day?
Candidate: PyTorch, distributed training, that's the core of it. I've done a lot
of k8s too but I don't need to keep doing it.
Agent: Are you employed at the moment?
Candidate: Yeah, I'm at a fin tech company, and I'm actively looking. I've been
here three years and the ML work got deprioritised.
Agent: How soon would you want to move?
Candidate: Within a month if the right thing came up.
Agent: What are you targeting on compensation?
Candidate: I'm on 210 base right now, I'd want 240 base to move.
Agent: And do you have the right to work in the US?
Candidate: I'm a green card holder, so no sponsorship needed.
Agent: Are you interviewing anywhere else right now?
Candidate: One place, a Series B company, I'm at the onsite stage. Should hear
back in about two weeks.
Agent: That's everything I needed. Since we have a minute — is there a project
you're proudest of?
Candidate: We cut training time on our fraud model from eleven hours to forty
minutes by rewriting the data loader and moving to multi-node. That saved the
team about a hundred grand a year in compute.
Agent: Anything you'd rather we didn't repeat to a company?
Candidate: Please don't tell anyone I'm looking — my manager doesn't know.
Agent: Understood. And is it OK for us to put your profile in front of companies
we're hiring for?
Candidate: Yes, go ahead.
Agent: Perfect, thank you.
"""

# What a good extractor returns for the call above. Values are intentionally
# left un-normalised ("Sr. ML Engineer", "fin tech", "SF", "Series B") so the
# tests prove the taxonomy pass happens on our side of the model.
COMPLETE_PAYLOAD: dict = {
    "spec": {
        "job_titles": ["Sr. ML Engineer"],
        "must_have_skills": ["PyTorch", "distributed training"],
        "nice_to_have_skills": ["k8s"],
        "seniority": ["senior"],
        "locations": ["SF"],
        "remote_ok": False,
        "industries": ["fin tech"],
        "company_stages": [],
        "company_sizes": [],
        "target_companies": [],
        "excluded_companies": [],
        "negative_keywords": [],
        "freeform_criteria": ["does not want to manage people"],
    },
    "employment_status": "employed_searching",
    "reason_for_looking": "The ML work at their current company got deprioritised.",
    "timeline": "under_1_month",
    "urgency_driver": "",
    "target_base": 240000,
    "target_total": None,
    "comp_currency": "USD",
    "comp_flexibility_notes": "",
    "equity_importance": "",
    "work_authorization": "green card",
    "willing_to_relocate": None,
    "active_processes": [
        {
            "company": "confidential",
            "stage": "onsite",
            "expected_decision": "about two weeks",
        }
    ],
    "offers_in_hand": [],
    "highlights": [
        {
            "title": "Cut fraud model training time 11h -> 40min",
            "detail": (
                "We cut training time on our fraud model from eleven hours to forty "
                "minutes by rewriting the data loader and moving to multi-node. That "
                "saved the team about a hundred grand a year in compute."
            ),
            "skills": ["distributed training", "pytorch"],
        }
    ],
    "confidential_items": ["Do not tell anyone they are looking; their manager does not know."],
    "ok_to_share_with_clients": True,
    "declined": [],
    "low_confidence": [],
}


SPARSE_TRANSCRIPT = """\
Agent: Hi, this is the intake line for Acme Talent. I'm an AI assistant and this
call is recorded — is that OK?
Candidate: Fine, but I've only got a minute.
Agent: No problem. What kind of role are you looking for?
Candidate: Backend engineering, staff level.
Agent: Where are you based?
Candidate: Austin.
Agent: What are you targeting on compensation?
Candidate: I'd rather not get into numbers on a first call.
Agent: That's completely fine. How soon would you want to move?
Candidate: Sorry, I have to jump. Can we pick this up another time?
Agent: Of course. Thanks for the time.
"""

SPARSE_PAYLOAD: dict = {
    "spec": {
        "job_titles": ["backend engineer"],
        "must_have_skills": [],
        "nice_to_have_skills": [],
        "seniority": ["staff"],
        "locations": ["Austin"],
        "remote_ok": None,
        "industries": [],
        "company_stages": [],
        "company_sizes": [],
        "target_companies": [],
        "excluded_companies": [],
        "negative_keywords": [],
        "freeform_criteria": [],
    },
    "employment_status": "",
    "reason_for_looking": "",
    "timeline": "",
    "urgency_driver": "",
    "target_base": None,
    "target_total": None,
    "comp_currency": "",
    "comp_flexibility_notes": "",
    "equity_importance": "",
    "work_authorization": "",
    "willing_to_relocate": None,
    "active_processes": [],
    "offers_in_hand": [],
    "highlights": [],
    "confidential_items": [],
    "ok_to_share_with_clients": None,
    "declined": ["compensation"],
    "low_confidence": [],
}
