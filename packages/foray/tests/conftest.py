"""Test configuration. DB-backed tests skip themselves when Postgres is not
reachable (see ``requires_db`` in test_db.py), so offline unit tests always run.
"""
