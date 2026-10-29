"""Unit tests for EHR Ingestion Lambda handler.

Tests cover:
- JSON validation and malformed input rejection
- Demographics extraction
- Section hash computation
- Differential update logic (compute_diff)
- FHIR R4 transformation
- Retry logic
"""

import hashlib
import json
import sys
import os
from unittest.mock import MagicMock, patch
import importlib.util

import pytest

# Load ehr_ingestion handler with importlib under a unique module name
# to avoid collision with guideline_ingestion/handler.py.
_handler_path = os.path.join(
    os.path.dirname(__file__), "..", "lambda", "ehr_ingestion", "handler.py"
)
_spec = importlib.util.spec_from_file_location("ehr_handler", _handler_path)
_ehr_module = importlib.util.module_from_spec(_spec)
sys.modules["ehr_handler"] = _ehr_module
_spec.loader.exec_module(_ehr_module)

MalformedInputError = _ehr_module.MalformedInputError
_calculate_age = _ehr_module._calculate_age
_compute_section_hashes = _ehr_module._compute_section_hashes
_extract_demographics = _ehr_module._extract_demographics
_map_severity_to_fhir = _ehr_module._map_severity_to_fhir
_map_sex_to_fhir_gender = _ehr_module._map_sex_to_fhir_gender
_parse_and_validate = _ehr_module._parse_and_validate
_retry_with_backoff = _ehr_module._retry_with_backoff
_transform_to_fhir_r4 = _ehr_module._transform_to_fhir_r4
compute_diff = _ehr_module.compute_diff
HASH_SECTIONS = _ehr_module.HASH_SECTIONS


# --- Fixtures ---

@pytest.fixture
def valid_patient_data():
    """Minimal valid patient data for testing."""
    return {
        "patient_id": "001",
        "patient_name": "Jean-Pierre Dubois",
        "mrn": "MRN-2024-00001",
        "chart_number": "CHT-FR-001",
        "first_name": "Jean-Pierre",
        "last_name": "Dubois",
        "date_of_birth": "1956-03-15",
        "sex_at_birth": "Male",
        "gender_identity": "Male",
        "race": "White",
        "ethnicity": "French",
        "allergies": [
            {
                "allergy_substance": "Penicillin",
                "allergy_type": "Drug",
                "reaction": "Rash",
                "severity": "Moderate",
            }
        ],
        "medications": [
            {
                "medication_name": "Metformin",
                "medication_code": "RxNorm:6809",
                "dose": "1000",
                "dose_unit": "mg",
                "route": "Oral",
                "frequency": "Twice daily",
                "adherence_notes": "Good",
            }
        ],
        "vital_signs": {
            "blood_pressure_systolic": 128,
            "blood_pressure_diastolic": 78,
            "heart_rate": 72,
            "respiratory_rate": 16,
            "temperature": 36.8,
            "spo2": 98,
            "weight": 82,
            "height": 175,
            "bmi": 26.8,
        },
        "lab_results": [
            {
                "test": "eGFR",
                "value": 32,
                "unit": "mL/min/1.73m²",
                "date": "2024-02-05",
                "reference_range": ">60",
                "flag": "LOW",
            }
        ],
        "medical_history": {
            "past_diagnoses": ["Type 2 Diabetes"],
            "chronic_conditions": [],
            "past_surgeries": [],
            "past_hospitalizations": [],
            "problem_list": [],
            "family_history": [],
            "social_history": {},
            "vaccination_history": [],
            "smoking_status": "Never",
            "alcohol_use": "None",
            "drug_use": "None",
            "occupation": "Engineer",
        },
    }


# --- Validation Tests ---

class TestParseAndValidate:
    """Tests for _parse_and_validate."""

    def test_valid_json_passes(self, valid_patient_data):
        raw = json.dumps(valid_patient_data).encode()
        result = _parse_and_validate(raw, "test.json")
        assert result["patient_id"] == "001"

    def test_invalid_json_syntax_raises(self):
        with pytest.raises(MalformedInputError, match="Invalid JSON syntax"):
            _parse_and_validate(b"{invalid json", "bad.json")

    def test_non_dict_json_raises(self):
        with pytest.raises(MalformedInputError, match="Expected JSON object"):
            _parse_and_validate(b"[1, 2, 3]", "array.json")

    def test_missing_patient_id_raises(self, valid_patient_data):
        del valid_patient_data["patient_id"]
        raw = json.dumps(valid_patient_data).encode()
        with pytest.raises(MalformedInputError, match="Missing required fields.*patient_id"):
            _parse_and_validate(raw, "missing_id.json")

    def test_missing_multiple_fields_raises(self):
        raw = json.dumps({"some_field": "value"}).encode()
        with pytest.raises(MalformedInputError, match="Missing required fields"):
            _parse_and_validate(raw, "empty.json")

    def test_empty_patient_id_raises(self, valid_patient_data):
        valid_patient_data["patient_id"] = ""
        raw = json.dumps(valid_patient_data).encode()
        with pytest.raises(MalformedInputError, match="Missing required fields.*patient_id"):
            _parse_and_validate(raw, "empty_id.json")

    def test_allergies_not_list_raises(self, valid_patient_data):
        valid_patient_data["allergies"] = "not a list"
        raw = json.dumps(valid_patient_data).encode()
        with pytest.raises(MalformedInputError, match="'allergies' must be a list"):
            _parse_and_validate(raw, "bad_allergies.json")

    def test_medications_not_list_raises(self, valid_patient_data):
        valid_patient_data["medications"] = {"name": "test"}
        raw = json.dumps(valid_patient_data).encode()
        with pytest.raises(MalformedInputError, match="'medications' must be a list"):
            _parse_and_validate(raw, "bad_meds.json")

    def test_vital_signs_not_dict_raises(self, valid_patient_data):
        valid_patient_data["vital_signs"] = [1, 2, 3]
        raw = json.dumps(valid_patient_data).encode()
        with pytest.raises(MalformedInputError, match="'vital_signs' must be a dict"):
            _parse_and_validate(raw, "bad_vitals.json")

    def test_unicode_decode_error_raises(self):
        with pytest.raises(MalformedInputError, match="Invalid JSON syntax"):
            _parse_and_validate(b"\xff\xfe", "binary.json")


# --- Demographics Extraction Tests ---

class TestExtractDemographics:
    """Tests for _extract_demographics."""

    def test_extracts_all_fields(self, valid_patient_data):
        demo = _extract_demographics(valid_patient_data)
        assert demo["patient_name"] == "Jean-Pierre Dubois"
        assert demo["first_name"] == "Jean-Pierre"
        assert demo["last_name"] == "Dubois"
        assert demo["date_of_birth"] == "1956-03-15"
        assert demo["sex_at_birth"] == "Male"
        assert demo["race"] == "White"
        assert demo["ethnicity"] == "French"
        assert demo["mrn"] == "MRN-2024-00001"

    def test_calculates_age(self, valid_patient_data):
        demo = _extract_demographics(valid_patient_data)
        assert demo["age"] is not None
        assert demo["age"] >= 68  # Born 1956


class TestCalculateAge:
    """Tests for _calculate_age."""

    def test_valid_dob(self):
        age = _calculate_age("2000-01-01")
        assert age >= 24

    def test_invalid_dob_returns_zero(self):
        assert _calculate_age("not-a-date") == 0

    def test_empty_dob_returns_zero(self):
        assert _calculate_age("") == 0


# --- Section Hash Tests ---

class TestComputeSectionHashes:
    """Tests for _compute_section_hashes."""

    def test_produces_hashes_for_all_sections(self, valid_patient_data):
        demographics = _extract_demographics(valid_patient_data)
        hashes = _compute_section_hashes(valid_patient_data, demographics)

        for section in HASH_SECTIONS:
            assert f"{section}_hash" in hashes
            assert len(hashes[f"{section}_hash"]) == 64  # SHA-256 hex length

    def test_same_data_produces_same_hash(self, valid_patient_data):
        demographics = _extract_demographics(valid_patient_data)
        hashes1 = _compute_section_hashes(valid_patient_data, demographics)
        hashes2 = _compute_section_hashes(valid_patient_data, demographics)
        assert hashes1 == hashes2

    def test_different_data_produces_different_hash(self, valid_patient_data):
        demographics = _extract_demographics(valid_patient_data)
        hashes1 = _compute_section_hashes(valid_patient_data, demographics)

        modified = valid_patient_data.copy()
        modified["medications"] = []
        hashes2 = _compute_section_hashes(modified, demographics)

        assert hashes1["medications_hash"] != hashes2["medications_hash"]
        assert hashes1["allergies_hash"] == hashes2["allergies_hash"]


# --- Differential Update Tests ---

class TestComputeDiff:
    """Tests for compute_diff."""

    def test_no_existing_record_returns_all_sections(self):
        incoming = {f"{s}_hash": "abc123" for s in HASH_SECTIONS}
        changes = compute_diff(incoming, None)
        assert set(changes.keys()) == set(HASH_SECTIONS)

    def test_identical_hashes_returns_empty(self):
        hashes = {f"{s}_hash": "abc123" for s in HASH_SECTIONS}
        existing = {"section_hashes": hashes}
        changes = compute_diff(hashes, existing)
        assert changes == {}

    def test_one_section_changed(self):
        incoming = {f"{s}_hash": "same" for s in HASH_SECTIONS}
        incoming["medications_hash"] = "different"

        stored = {f"{s}_hash": "same" for s in HASH_SECTIONS}
        existing = {"section_hashes": stored}

        changes = compute_diff(incoming, existing)
        assert changes == {"medications": True}

    def test_multiple_sections_changed(self):
        incoming = {f"{s}_hash": "new" for s in HASH_SECTIONS}
        stored = {f"{s}_hash": "old" for s in HASH_SECTIONS}
        existing = {"section_hashes": stored}

        changes = compute_diff(incoming, existing)
        assert set(changes.keys()) == set(HASH_SECTIONS)

    def test_missing_stored_hashes_treated_as_changed(self):
        incoming = {f"{s}_hash": "abc" for s in HASH_SECTIONS}
        existing = {"section_hashes": {}}  # No stored hashes
        changes = compute_diff(incoming, existing)
        assert set(changes.keys()) == set(HASH_SECTIONS)


# --- FHIR Transformation Tests ---

class TestTransformToFhirR4:
    """Tests for _transform_to_fhir_r4."""

    def test_produces_bundle(self, valid_patient_data):
        bundle = _transform_to_fhir_r4(valid_patient_data)
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "transaction"
        assert len(bundle["entry"]) > 0

    def test_patient_resource_present(self, valid_patient_data):
        bundle = _transform_to_fhir_r4(valid_patient_data)
        patient_entries = [
            e for e in bundle["entry"]
            if e["resource"]["resourceType"] == "Patient"
        ]
        assert len(patient_entries) == 1
        patient = patient_entries[0]["resource"]
        assert patient["id"] == "001"
        assert patient["gender"] == "male"
        assert patient["birthDate"] == "1956-03-15"

    def test_allergy_resources_present(self, valid_patient_data):
        bundle = _transform_to_fhir_r4(valid_patient_data)
        allergy_entries = [
            e for e in bundle["entry"]
            if e["resource"]["resourceType"] == "AllergyIntolerance"
        ]
        assert len(allergy_entries) == 1
        assert allergy_entries[0]["resource"]["code"]["text"] == "Penicillin"

    def test_medication_resources_present(self, valid_patient_data):
        bundle = _transform_to_fhir_r4(valid_patient_data)
        med_entries = [
            e for e in bundle["entry"]
            if e["resource"]["resourceType"] == "MedicationStatement"
        ]
        assert len(med_entries) == 1
        assert "Metformin" in med_entries[0]["resource"]["medicationCodeableConcept"]["text"]

    def test_observation_resources_for_labs(self, valid_patient_data):
        bundle = _transform_to_fhir_r4(valid_patient_data)
        lab_entries = [
            e for e in bundle["entry"]
            if e["resource"]["resourceType"] == "Observation"
            and "lab" in e["resource"]["id"]
        ]
        assert len(lab_entries) == 1
        assert lab_entries[0]["resource"]["code"]["text"] == "eGFR"

    def test_observation_resources_for_vitals(self, valid_patient_data):
        bundle = _transform_to_fhir_r4(valid_patient_data)
        vital_entries = [
            e for e in bundle["entry"]
            if e["resource"]["resourceType"] == "Observation"
            and "vital" in e["resource"]["id"]
        ]
        # 9 vital sign measurements
        assert len(vital_entries) == 9


# --- FHIR Mapping Tests ---

class TestFhirMappings:
    """Tests for FHIR value mappings."""

    def test_sex_to_gender_male(self):
        assert _map_sex_to_fhir_gender("Male") == "male"

    def test_sex_to_gender_female(self):
        assert _map_sex_to_fhir_gender("Female") == "female"

    def test_sex_to_gender_unknown(self):
        assert _map_sex_to_fhir_gender("Other") == "unknown"

    def test_severity_mild(self):
        assert _map_severity_to_fhir("Mild") == "mild"

    def test_severity_moderate(self):
        assert _map_severity_to_fhir("Moderate") == "moderate"

    def test_severity_severe(self):
        assert _map_severity_to_fhir("Severe") == "severe"

    def test_severity_unknown_defaults_moderate(self):
        assert _map_severity_to_fhir("Unknown") == "moderate"


# --- Retry Logic Tests ---

class TestRetryWithBackoff:
    """Tests for _retry_with_backoff."""

    @patch("ehr_handler.time.sleep")
    def test_succeeds_on_first_try(self, mock_sleep):
        result = _retry_with_backoff(lambda: "success", "test op")
        assert result == "success"
        mock_sleep.assert_not_called()

    @patch("ehr_handler.time.sleep")
    def test_retries_on_failure_then_succeeds(self, mock_sleep):
        call_count = {"n": 0}

        def flaky_op():
            call_count["n"] += 1
            if call_count["n"] < 3:
                from botocore.exceptions import ClientError
                raise ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                    "PutItem",
                )
            return "success"

        result = _retry_with_backoff(flaky_op, "test op")
        assert result == "success"
        assert call_count["n"] == 3

    @patch("ehr_handler.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        from botocore.exceptions import ClientError

        def always_fails():
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "Server error"}},
                "PutItem",
            )

        with pytest.raises(ClientError):
            _retry_with_backoff(always_fails, "test op")

    @patch("ehr_handler.time.sleep")
    def test_no_retry_on_access_denied(self, mock_sleep):
        from botocore.exceptions import ClientError

        def access_denied():
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
                "PutItem",
            )

        with pytest.raises(ClientError):
            _retry_with_backoff(access_denied, "test op")
        mock_sleep.assert_not_called()


# --- Handler Integration Test ---

class TestHandler:
    """Integration tests for the handler function."""

    @patch("ehr_handler._get_dynamodb")
    @patch("ehr_handler._get_s3_client")
    @patch("ehr_handler._get_healthlake_client")
    @patch("ehr_handler._get_bedrock_agent_client")
    def test_handler_processes_valid_event(
        self, mock_bedrock_fn, mock_hl_fn, mock_s3_fn, mock_dynamo_fn, valid_patient_data
    ):
        

        # Mock S3 download
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(valid_patient_data).encode())
        }
        mock_s3_fn.return_value = mock_s3

        # Mock DynamoDB
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # No existing record
        mock_dynamo = MagicMock()
        mock_dynamo.Table.return_value = mock_table
        mock_dynamo_fn.return_value = mock_dynamo

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "test-bucket"},
                        "object": {"key": "ehr/patient_001.json"},
                    }
                }
            ]
        }

        result = _ehr_module.handler(event, None)
        assert result["statusCode"] == 200
        mock_s3.get_object.assert_called_once_with(Bucket="test-bucket", Key="ehr/patient_001.json")
        mock_table.put_item.assert_called_once()

    @patch("ehr_handler._get_dynamodb")
    @patch("ehr_handler._get_s3_client")
    def test_handler_skips_malformed_json(self, mock_s3_fn, mock_dynamo_fn):
        

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"{invalid json")
        }
        mock_s3_fn.return_value = mock_s3

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "test-bucket"},
                        "object": {"key": "ehr/bad_file.json"},
                    }
                }
            ]
        }

        # Should not raise — malformed input is logged and skipped
        result = _ehr_module.handler(event, None)
        assert result["statusCode"] == 200

    @patch("ehr_handler._get_dynamodb")
    @patch("ehr_handler._get_s3_client")
    def test_handler_skips_missing_fields(self, mock_s3_fn, mock_dynamo_fn):
        

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({"some": "data"}).encode())
        }
        mock_s3_fn.return_value = mock_s3

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "test-bucket"},
                        "object": {"key": "ehr/incomplete.json"},
                    }
                }
            ]
        }

        result = _ehr_module.handler(event, None)
        assert result["statusCode"] == 200
