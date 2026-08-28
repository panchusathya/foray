"""The application lifecycle: propose, prepare, submit, track.

CANDIDATE_AGENT_PLAN §4, §6, §8 and §10 in code. Three axes, three tables,
none overloaded onto each other:

* ``JobMatch`` -- recruiter triage. Never mutated by anything in here.
* ``JobProposal`` -- what the candidate was shown and what they said.
* ``Application`` -- the pursuit itself; the only thing a submission can
  hang off.

``limits.py`` owns every budget as one decision object, the way
``intake.schema.coverage()`` owns the gate -- scattered conditionals at call
sites is how a limit silently stops being enforced. ``state.py`` owns the
status machine.
"""
