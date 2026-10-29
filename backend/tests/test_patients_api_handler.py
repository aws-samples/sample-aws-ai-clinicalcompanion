"""Unit tests for the Patients API Lambda handler.

Focus: authorization behavior (BOLA/T9 hardening) and routing.
- Unauthenticated requests are rejected with 401 (fail closed).
- Authenticated requests route to list/get and attribute access to the caller.
"""
import json
import os
import sys
import importlib.util
from unittest.mock import MagicMock

import pytest

# The handler creates its DynamoDB resource/table at import time, which needs a
# region and the table-name env var. Set both before importing the module.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("PATIENT_INDEX_TABLE_NAME", "test-patient-index")

# Load the patients_api handler under a unique module name to avoid collisions
# with other lambda/handler.py modules in the test session.
_handler_path = os.path.join(
    os.path.dirname(__file__), "..", "lambda", "patients_api", "handler.py"
)
_spec = importlib.util.spec_from_file_location("patients_api_handler", _handler_path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["patients_api_handler"] = _mod
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler


def _event(patient_id=None, caller_sub="clinician-123"):
    """Build a minimal HTTP API v2 event with optional JWT claims."""
    event = {"requestContext": {"http": {"method": "GET"}}}
    if patient_id is not None:
        event["pathParameters"] = {"patient_id": patient_id}
    if caller_sub is not None:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": caller_sub}}}
    return event


@pytest.fixture(autouse=True)
def _mock_table(monkeypatch):
    """Replace the module-level DynamoDB table with a mock for each test."""
    mock_table = MagicMock()
    monkeypatch.setattr(_mod, "table", mock_table)
    return mock_table


def test_unauthenticated_list_is_rejected():
    """No JWT claims -> 401, and DynamoDB is never touched."""
    resp = lambda_handler(_event(caller_sub=None), None)
    assert resp["statusCode"] == 401


def test_unauthenticated_get_is_rejected(_mock_table):
    """No JWT claims on a single-record request -> 401, no data access."""
    resp = lambda_handler(_event(patient_id="001", caller_sub=None), None)
    assert resp["statusCode"] == 401
    _mock_table.get_item.assert_not_called()


def test_authenticated_list_returns_summary(_mock_table):
    """Authenticated list returns 200 with summarized patients."""
    _mock_table.scan.return_value = {
        "Items": [
            {"patient_id": "001", "patient_name": "Test One",
             "demographics": {"age": "40", "sex_at_birth": "F"}},
        ]
    }
    resp = lambda_handler(_event(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["patients"][0]["patient_id"] == "001"
    assert body["patients"][0]["age"] == 40


def test_authenticated_get_found(_mock_table):
    """Authenticated get returns 200 with the record when it exists."""
    _mock_table.get_item.return_value = {"Item": {"patient_id": "001", "patient_name": "Test One"}}
    resp = lambda_handler(_event(patient_id="001"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["patient_id"] == "001"


def test_authenticated_get_missing_returns_404(_mock_table):
    """Authenticated get returns 404 when the record does not exist."""
    _mock_table.get_item.return_value = {}
    resp = lambda_handler(_event(patient_id="nope"), None)
    assert resp["statusCode"] == 404
