"""Pytest configuration for the backend tests."""
import sys
import os

from hypothesis import settings

# Add backend root to path so utils/ imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Some Lambda handlers read these at import time. Tests never call AWS (clients are
# mocked), but boto3 needs a region and the handlers need their table name.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("PATIENT_INDEX_TABLE_NAME", "test-patient-index")

# Property-based tests check correctness, not speed. Hypothesis' default 200ms
# per-example deadline made them flaky on a fresh clone: the first example pays the
# cold-import cost (seen at ~1.8s) and failed with DeadlineExceeded / "Unreliable test
# timings". Disable the deadline for all tests; per-test @settings that don't set
# `deadline` inherit this profile.
settings.register_profile("default", deadline=None)
settings.load_profile("default")
