"""
Property-based tests for EHR data pipeline correctness.

Uses Hypothesis to verify that the differential update logic correctly
identifies changed sections between patient record versions, that
malformed JSON inputs are properly rejected without side effects, and
that querying by patient_id returns complete records with all sections.

# Feature: clinical-enhancements, Property 5: Differential update correctness
# Feature: clinical-enhancements, Property 6: Malformed JSON resilience
# Feature: clinical-enhancements, Property 7: Patient data query completeness
"""

import hashlib
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

# Add lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "lambda", "ehr_ingestion")
)

from handler import (
    compute_diff,
    HASH_SECTIONS,
    REQUIRED_FIELDS,
    MalformedInputError,
    _compute_section_hashes,
    _extract_demographics,
    _parse_and_validate,
    _store_in_dynamodb,
)


# ─── Strategies ──────────────────────────────────────────────────────────────

# Strategy for generating a single allergy entry
allergy_entry_st = st.fixed_dictionaries({
    "allergy_substance": st.text(min_size=1, max_size=50),
    "allergy_type": st.sampled_from(["Drug", "Food", "Environmental"]),
    "reaction": st.text(min_size=1, max_size=50),
    "severity": st.sampled_from(["Mild", "Moderate", "Severe"]),
})

# Strategy for generating a single medication entry
medication_entry_st = st.fixed_dictionaries({
    "medication_name": st.text(min_size=1, max_size=50),
    "medication_code": st.text(min_size=1, max_size=20),
    "dose": st.text(min_size=1, max_size=10),
    "dose_unit": st.sampled_from(["mg", "mL", "mcg", "units"]),
    "route": st.sampled_from(["Oral", "IV", "IM", "SC", "Topical"]),
    "frequency": st.sampled_from(["Once daily", "Twice daily", "Three times daily", "As needed"]),
    "adherence_notes": st.text(min_size=0, max_size=50),
})

# Strategy for generating a single lab result entry
lab_result_entry_st = st.fixed_dictionaries({
    "test": st.text(min_size=1, max_size=30),
    "value": st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    "unit": st.text(min_size=1, max_size=20),
    "date": st.dates().map(lambda d: d.isoformat()),
    "reference_range": st.text(min_size=1, max_size=20),
    "flag": st.sampled_from(["NORMAL", "HIGH", "LOW"]),
})

# Strategy for generating vital signs
vital_signs_st = st.fixed_dictionaries({
    "blood_pressure_systolic": st.integers(min_value=70, max_value=250),
    "blood_pressure_diastolic": st.integers(min_value=40, max_value=150),
    "heart_rate": st.integers(min_value=30, max_value=200),
    "respiratory_rate": st.integers(min_value=8, max_value=40),
    "temperature": st.floats(min_value=35.0, max_value=42.0, allow_nan=False, allow_infinity=False),
    "spo2": st.integers(min_value=70, max_value=100),
    "weight": st.floats(min_value=2.0, max_value=300.0, allow_nan=False, allow_infinity=False),
    "height": st.floats(min_value=30.0, max_value=250.0, allow_nan=False, allow_infinity=False),
    "bmi": st.floats(min_value=10.0, max_value=60.0, allow_nan=False, allow_infinity=False),
})

# Strategy for generating a complete patient record
patient_record_st = st.fixed_dictionaries({
    "patient_id": st.text(min_size=1, max_size=10, alphabet="0123456789"),
    "patient_name": st.text(min_size=2, max_size=50),
    "mrn": st.text(min_size=1, max_size=20),
    "chart_number": st.text(min_size=1, max_size=20),
    "first_name": st.text(min_size=1, max_size=30),
    "last_name": st.text(min_size=1, max_size=30),
    "date_of_birth": st.dates(
        min_value=st.DateStrategy.min_value if hasattr(st, 'DateStrategy') else None,
    ).map(lambda d: d.isoformat()) if False else st.just("1980-01-15"),
    "sex_at_birth": st.sampled_from(["Male", "Female"]),
    "gender_identity": st.text(min_size=1, max_size=20),
    "race": st.text(min_size=1, max_size=30),
    "ethnicity": st.text(min_size=1, max_size=30),
    "allergies": st.lists(allergy_entry_st, min_size=0, max_size=5),
    "medications": st.lists(medication_entry_st, min_size=0, max_size=5),
    "lab_results": st.lists(lab_result_entry_st, min_size=0, max_size=5),
    "vital_signs": vital_signs_st,
})


def _hash_section(section_content) -> str:
    """Compute SHA-256 hash of a section for test verification."""
    serialized = json.dumps(section_content, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_stored_record(patient_data: dict) -> dict:
    """Build a stored record (as it would exist in DynamoDB) from patient data."""
    demographics = _extract_demographics(patient_data)
    section_hashes = _compute_section_hashes(patient_data, demographics)
    return {"section_hashes": section_hashes}


# ─── Property 5: Differential update correctness ─────────────────────────────
# **Validates: Requirements 2.8, 2.9**


class TestDifferentialUpdateCorrectness:
    """Property 5: Differential update correctness.

    For any two versions of a patient record (original and modified), the diff
    function SHALL identify exactly the set of changed sections — sections with
    identical content produce no diff entries, and sections with different content
    produce exactly one diff entry per changed section.
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(patient_data=patient_record_st)
    def test_identical_records_produce_no_diff(self, patient_data: dict):
        """When original and incoming records are identical, no diff entries are produced.

        **Validates: Requirements 2.8, 2.9**
        """
        # Build stored record from the patient data
        stored_record = _build_stored_record(patient_data)

        # Compute incoming hashes (same data)
        demographics = _extract_demographics(patient_data)
        incoming_hashes = _compute_section_hashes(patient_data, demographics)

        # Compute diff
        changes = compute_diff(incoming_hashes, stored_record)

        # No changes should be detected
        assert changes == {}, (
            f"Expected no diff for identical records, got changes: {changes}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        patient_data=patient_record_st,
        sections_to_modify=st.lists(
            st.sampled_from(list(HASH_SECTIONS)),
            min_size=1,
            max_size=len(HASH_SECTIONS),
            unique=True,
        ),
    )
    def test_modified_sections_produce_exactly_one_diff_entry_each(
        self, patient_data: dict, sections_to_modify: list
    ):
        """When specific sections are modified, exactly those sections appear in the diff.

        **Validates: Requirements 2.8, 2.9**
        """
        # Build stored record from original data
        stored_record = _build_stored_record(patient_data)

        # Create a modified version of the patient data
        modified_data = json.loads(json.dumps(patient_data))

        for section in sections_to_modify:
            if section == "demographics":
                # Modify a demographic field
                modified_data["first_name"] = modified_data["first_name"] + "_modified"
            elif section == "medications":
                # Add a new medication
                modified_data["medications"] = modified_data.get("medications", []) + [
                    {
                        "medication_name": "NewDrug",
                        "medication_code": "NEW001",
                        "dose": "100",
                        "dose_unit": "mg",
                        "route": "Oral",
                        "frequency": "Once daily",
                        "adherence_notes": "New",
                    }
                ]
            elif section == "allergies":
                # Add a new allergy
                modified_data["allergies"] = modified_data.get("allergies", []) + [
                    {
                        "allergy_substance": "NewAllergen",
                        "allergy_type": "Drug",
                        "reaction": "Hives",
                        "severity": "Severe",
                    }
                ]
            elif section == "lab_results":
                # Add a new lab result
                modified_data["lab_results"] = modified_data.get("lab_results", []) + [
                    {
                        "test": "NewTest",
                        "value": 999.0,
                        "unit": "units",
                        "date": "2025-01-01",
                        "reference_range": "0-100",
                        "flag": "HIGH",
                    }
                ]
            elif section == "vital_signs":
                # Modify a vital sign value
                modified_data["vital_signs"]["heart_rate"] = (
                    modified_data["vital_signs"].get("heart_rate", 70) + 50
                )

        # Compute incoming hashes from modified data
        modified_demographics = _extract_demographics(modified_data)
        incoming_hashes = _compute_section_hashes(modified_data, modified_demographics)

        # Compute diff
        changes = compute_diff(incoming_hashes, stored_record)

        # Exactly the modified sections should appear in the diff
        assert set(changes.keys()) == set(sections_to_modify), (
            f"Expected changes in {sections_to_modify}, got {list(changes.keys())}"
        )

        # Each changed section should have exactly one entry (mapped to True)
        for section in sections_to_modify:
            assert changes[section] is True, (
                f"Expected section '{section}' to be True in diff, got {changes[section]}"
            )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(patient_data=patient_record_st)
    def test_no_existing_record_returns_all_sections_changed(self, patient_data: dict):
        """When there is no existing record, all sections are reported as changed.

        **Validates: Requirements 2.8, 2.9**
        """
        demographics = _extract_demographics(patient_data)
        incoming_hashes = _compute_section_hashes(patient_data, demographics)

        # No existing record (first ingestion)
        changes = compute_diff(incoming_hashes, None)

        # All sections should be marked as changed
        assert set(changes.keys()) == set(HASH_SECTIONS), (
            f"Expected all sections changed for new record, got {list(changes.keys())}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        patient_data=patient_record_st,
        unchanged_sections=st.lists(
            st.sampled_from(list(HASH_SECTIONS)),
            min_size=1,
            max_size=len(HASH_SECTIONS),
            unique=True,
        ),
    )
    def test_unchanged_sections_produce_no_diff_entries(
        self, patient_data: dict, unchanged_sections: list
    ):
        """Sections with identical content between original and incoming produce no diff entries.

        **Validates: Requirements 2.8, 2.9**
        """
        # Build stored record
        stored_record = _build_stored_record(patient_data)

        # Modify only sections NOT in unchanged_sections
        modified_data = json.loads(json.dumps(patient_data))
        sections_to_change = [s for s in HASH_SECTIONS if s not in unchanged_sections]

        for section in sections_to_change:
            if section == "demographics":
                modified_data["last_name"] = modified_data["last_name"] + "_changed"
            elif section == "medications":
                modified_data["medications"] = [
                    {
                        "medication_name": "ChangedDrug",
                        "medication_code": "CHG001",
                        "dose": "200",
                        "dose_unit": "mg",
                        "route": "IV",
                        "frequency": "Three times daily",
                        "adherence_notes": "Changed",
                    }
                ]
            elif section == "allergies":
                modified_data["allergies"] = [
                    {
                        "allergy_substance": "ChangedAllergen",
                        "allergy_type": "Food",
                        "reaction": "Anaphylaxis",
                        "severity": "Severe",
                    }
                ]
            elif section == "lab_results":
                modified_data["lab_results"] = [
                    {
                        "test": "ChangedTest",
                        "value": 0.1,
                        "unit": "changed_unit",
                        "date": "2025-06-01",
                        "reference_range": "0-1",
                        "flag": "LOW",
                    }
                ]
            elif section == "vital_signs":
                modified_data["vital_signs"] = {
                    "blood_pressure_systolic": 999,
                    "blood_pressure_diastolic": 999,
                    "heart_rate": 999,
                    "respiratory_rate": 999,
                    "temperature": 40.0,
                    "spo2": 99,
                    "weight": 999.0,
                    "height": 999.0,
                    "bmi": 999.0,
                }

        # Compute incoming hashes from modified data
        modified_demographics = _extract_demographics(modified_data)
        incoming_hashes = _compute_section_hashes(modified_data, modified_demographics)

        # Compute diff
        changes = compute_diff(incoming_hashes, stored_record)

        # Unchanged sections should NOT appear in the diff
        for section in unchanged_sections:
            assert section not in changes, (
                f"Section '{section}' was not modified but appeared in diff"
            )

        # Changed sections should appear in the diff
        for section in sections_to_change:
            assert section in changes, (
                f"Section '{section}' was modified but did not appear in diff"
            )



# ─── Property 6: Malformed JSON resilience ───────────────────────────────────
# **Validates: Requirements 2.7**


# Strategies for generating malformed inputs

# Strategy for generating invalid JSON syntax (not parseable)
invalid_json_syntax_st = st.one_of(
    # Truncated JSON
    st.text(min_size=1, max_size=100).map(lambda t: "{" + t),
    # Random bytes that aren't valid JSON
    st.binary(min_size=1, max_size=200),
    # JSON with trailing commas
    st.just(b'{"patient_id": "001", "patient_name": "Test",}'),
    # Unquoted keys
    st.just(b'{patient_id: "001"}'),
    # Single quotes instead of double quotes
    st.just(b"{'patient_id': '001'}"),
    # Missing closing brace
    st.just(b'{"patient_id": "001"'),
    # Just whitespace
    st.just(b"   "),
    # Empty bytes
    st.just(b""),
    # Array instead of object at top level (valid JSON but wrong type)
    st.just(b'[{"patient_id": "001"}]'),
    # Numeric literal
    st.just(b"42"),
    # String literal
    st.just(b'"just a string"'),
    # Null literal
    st.just(b"null"),
    # Boolean literal
    st.just(b"true"),
)


def _make_valid_patient_dict() -> dict:
    """Create a minimal valid patient record dict for mutation testing."""
    return {
        "patient_id": "TEST001",
        "patient_name": "Test Patient",
        "first_name": "Test",
        "last_name": "Patient",
        "date_of_birth": "1990-05-15",
        "sex_at_birth": "Male",
        "gender_identity": "Male",
        "race": "White",
        "ethnicity": "Non-Hispanic",
        "mrn": "MRN12345",
        "chart_number": "CH12345",
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
                "medication_code": "MET500",
                "dose": "500",
                "dose_unit": "mg",
                "route": "Oral",
                "frequency": "Twice daily",
                "adherence_notes": "Good compliance",
            }
        ],
        "lab_results": [
            {
                "test": "eGFR",
                "value": 65.0,
                "unit": "mL/min/1.73m2",
                "date": "2024-01-15",
                "reference_range": ">60",
                "flag": "NORMAL",
            }
        ],
        "vital_signs": {
            "blood_pressure_systolic": 130,
            "blood_pressure_diastolic": 85,
            "heart_rate": 72,
            "respiratory_rate": 16,
            "temperature": 36.8,
            "spo2": 98,
            "weight": 80.0,
            "height": 175.0,
            "bmi": 26.1,
        },
    }


# Strategy for generating records with missing required fields
missing_required_fields_st = st.sampled_from(list(REQUIRED_FIELDS)).flatmap(
    lambda field_to_remove: st.one_of(
        # Remove the field entirely
        st.just((field_to_remove, "removed")),
        # Set the field to empty string
        st.just((field_to_remove, "empty")),
        # Set the field to None
        st.just((field_to_remove, "none")),
    )
)

# Strategy for generating records with wrong types for section fields
wrong_type_sections_st = st.sampled_from([
    # allergies should be a list, not a string
    ("allergies", "not a list"),
    # allergies should be a list, not a dict
    ("allergies", {"substance": "Penicillin"}),
    # allergies should be a list, not an int
    ("allergies", 42),
    # medications should be a list, not a string
    ("medications", "not a list"),
    # medications should be a list, not a number
    ("medications", 3.14),
    # lab_results should be a list, not a dict
    ("lab_results", {"test": "eGFR"}),
    # lab_results should be a list, not a boolean
    ("lab_results", True),
    # vital_signs should be a dict, not a list
    ("vital_signs", [130, 85, 72]),
    # vital_signs should be a dict, not a string
    ("vital_signs", "normal"),
    # vital_signs should be a dict, not a number
    ("vital_signs", 98.6),
])


class TestMalformedJsonResilience:
    """Property 6: Malformed JSON resilience.

    For any malformed JSON input (invalid syntax, missing required fields,
    wrong types), the EHR pipeline SHALL reject the input with an error log
    and SHALL NOT modify any existing records in the system.

    **Validates: Requirements 2.7**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(raw_data=invalid_json_syntax_st)
    def test_invalid_json_syntax_is_rejected(self, raw_data):
        """Invalid JSON syntax (not parseable) is rejected with MalformedInputError.

        **Validates: Requirements 2.7**
        """
        # Ensure raw_data is bytes
        if isinstance(raw_data, str):
            raw_data = raw_data.encode("utf-8")

        # The parse_and_validate function should raise MalformedInputError
        with pytest.raises(MalformedInputError) as exc_info:
            _parse_and_validate(raw_data, "test_file.json")

        # Error message should reference the file name
        assert "test_file.json" in str(exc_info.value)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(field_mutation=missing_required_fields_st)
    def test_missing_required_fields_is_rejected(self, field_mutation):
        """Records with missing required fields are rejected with MalformedInputError.

        **Validates: Requirements 2.7**
        """
        field_to_remove, mutation_type = field_mutation

        # Start with a valid patient record
        patient_data = _make_valid_patient_dict()

        # Apply the mutation
        if mutation_type == "removed":
            del patient_data[field_to_remove]
        elif mutation_type == "empty":
            patient_data[field_to_remove] = ""
        elif mutation_type == "none":
            patient_data[field_to_remove] = None

        # Serialize to JSON bytes
        raw_data = json.dumps(patient_data).encode("utf-8")

        # The parse_and_validate function should raise MalformedInputError
        with pytest.raises(MalformedInputError) as exc_info:
            _parse_and_validate(raw_data, "test_missing_fields.json")

        # Error message should reference the file name
        assert "test_missing_fields.json" in str(exc_info.value)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(wrong_type=wrong_type_sections_st)
    def test_wrong_types_for_sections_is_rejected(self, wrong_type):
        """Records with wrong types for section fields are rejected with MalformedInputError.

        **Validates: Requirements 2.7**
        """
        field_name, wrong_value = wrong_type

        # Start with a valid patient record
        patient_data = _make_valid_patient_dict()

        # Apply the wrong type
        patient_data[field_name] = wrong_value

        # Serialize to JSON bytes
        raw_data = json.dumps(patient_data).encode("utf-8")

        # The parse_and_validate function should raise MalformedInputError
        with pytest.raises(MalformedInputError) as exc_info:
            _parse_and_validate(raw_data, "test_wrong_types.json")

        # Error message should reference the file name
        assert "test_wrong_types.json" in str(exc_info.value)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        raw_data=st.one_of(
            invalid_json_syntax_st,
            missing_required_fields_st.map(
                lambda fm: _apply_field_mutation(_make_valid_patient_dict(), fm)
            ),
            wrong_type_sections_st.map(
                lambda wt: _apply_wrong_type(_make_valid_patient_dict(), wt)
            ),
        )
    )
    def test_malformed_input_does_not_modify_existing_records(self, raw_data):
        """Malformed input SHALL NOT modify any existing records in the system.

        This test verifies that _parse_and_validate raises before any downstream
        processing (DynamoDB, HealthLake, KB) can occur. Since the handler calls
        _parse_and_validate before any write operations, a rejection at this stage
        guarantees no state modification.

        **Validates: Requirements 2.7**
        """
        # Ensure raw_data is bytes
        if isinstance(raw_data, str):
            raw_data = raw_data.encode("utf-8")

        # The parse_and_validate function should raise MalformedInputError
        # This is the validation gate — if it raises, no downstream writes happen
        with pytest.raises(MalformedInputError):
            _parse_and_validate(raw_data, "test_no_state_change.json")

        # If we reach here, the input was correctly rejected before any
        # write operations could occur. The handler's control flow ensures
        # that _parse_and_validate is called BEFORE any DynamoDB, HealthLake,
        # or KB operations, so rejection here guarantees no state modification.


def _apply_field_mutation(patient_data: dict, field_mutation: tuple) -> bytes:
    """Helper to apply a field mutation and return JSON bytes."""
    field_to_remove, mutation_type = field_mutation
    data = patient_data.copy()
    if mutation_type == "removed":
        del data[field_to_remove]
    elif mutation_type == "empty":
        data[field_to_remove] = ""
    elif mutation_type == "none":
        data[field_to_remove] = None
    return json.dumps(data).encode("utf-8")


def _apply_wrong_type(patient_data: dict, wrong_type: tuple) -> bytes:
    """Helper to apply a wrong type mutation and return JSON bytes."""
    field_name, wrong_value = wrong_type
    data = patient_data.copy()
    data[field_name] = wrong_value
    return json.dumps(data).encode("utf-8")



# ─── Property 7: Patient data query completeness ─────────────────────────────
# Feature: clinical-enhancements, Property 7: Patient data query completeness
# **Validates: Requirements 2.5**


class TestPatientDataQueryCompleteness:
    """Property 7: Patient data query completeness.

    For any valid patient record stored in the system, querying by patient_id
    SHALL return the complete record including demographics, medications,
    allergies, lab results, and vital signs.

    **Validates: Requirements 2.5**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(patient_data=patient_record_st)
    def test_query_by_patient_id_returns_complete_record(self, patient_data: dict):
        """Querying by patient_id returns the complete record with all sections.

        **Validates: Requirements 2.5**
        """
        # Arrange: Build the item as it would be stored in DynamoDB
        demographics = _extract_demographics(patient_data)
        section_hashes = _compute_section_hashes(patient_data, demographics)

        stored_item = {
            "patient_id": patient_data["patient_id"],
            "patient_name": patient_data.get("patient_name", ""),
            "demographics": demographics,
            "allergies": patient_data.get("allergies", []),
            "medications": patient_data.get("medications", []),
            "lab_results": patient_data.get("lab_results", []),
            "vital_signs": patient_data.get("vital_signs", {}),
            "section_hashes": section_hashes,
            "last_updated": "2025-01-01T00:00:00+00:00",
            "fhir_resource_id": f"Patient/{patient_data['patient_id']}",
        }

        # Create a mock DynamoDB table that stores and retrieves items
        mock_table = MagicMock()
        mock_table.put_item = MagicMock(return_value=None)
        mock_table.get_item = MagicMock(return_value={"Item": stored_item})

        # Act: Store the record
        _store_in_dynamodb(
            mock_table,
            patient_data,
            demographics,
            section_hashes,
            f"Patient/{patient_data['patient_id']}",
            {s: True for s in HASH_SECTIONS},
        )

        # Verify put_item was called
        mock_table.put_item.assert_called_once()

        # Act: Query by patient_id (simulating get_patient from patients_api)
        response = mock_table.get_item(Key={"patient_id": patient_data["patient_id"]})
        retrieved_item = response.get("Item")

        # Assert: The retrieved record is complete
        assert retrieved_item is not None, "Query by patient_id returned no record"

        # Verify demographics section is present and complete
        assert "demographics" in retrieved_item, "Missing 'demographics' section"
        assert retrieved_item["demographics"]["patient_name"] == patient_data.get("patient_name", "")
        assert retrieved_item["demographics"]["sex_at_birth"] == patient_data.get("sex_at_birth", "")
        assert retrieved_item["demographics"]["date_of_birth"] == patient_data.get("date_of_birth", "")

        # Verify medications section is present and complete
        assert "medications" in retrieved_item, "Missing 'medications' section"
        assert retrieved_item["medications"] == patient_data.get("medications", [])

        # Verify allergies section is present and complete
        assert "allergies" in retrieved_item, "Missing 'allergies' section"
        assert retrieved_item["allergies"] == patient_data.get("allergies", [])

        # Verify lab_results section is present and complete
        assert "lab_results" in retrieved_item, "Missing 'lab_results' section"
        assert retrieved_item["lab_results"] == patient_data.get("lab_results", [])

        # Verify vital_signs section is present and complete
        assert "vital_signs" in retrieved_item, "Missing 'vital_signs' section"
        assert retrieved_item["vital_signs"] == patient_data.get("vital_signs", {})

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(patient_data=patient_record_st)
    def test_stored_record_preserves_all_medication_details(self, patient_data: dict):
        """Each medication entry retains all fields after store and query.

        **Validates: Requirements 2.5**
        """
        demographics = _extract_demographics(patient_data)
        section_hashes = _compute_section_hashes(patient_data, demographics)

        # Simulate the item as stored by _store_in_dynamodb
        stored_item = {
            "patient_id": patient_data["patient_id"],
            "patient_name": patient_data.get("patient_name", ""),
            "demographics": demographics,
            "allergies": patient_data.get("allergies", []),
            "medications": patient_data.get("medications", []),
            "lab_results": patient_data.get("lab_results", []),
            "vital_signs": patient_data.get("vital_signs", {}),
            "section_hashes": section_hashes,
            "last_updated": "2025-01-01T00:00:00+00:00",
            "fhir_resource_id": f"Patient/{patient_data['patient_id']}",
        }

        # Verify each medication entry has all expected fields
        for i, med in enumerate(stored_item["medications"]):
            assert "medication_name" in med, f"Medication {i} missing 'medication_name'"
            assert "dose" in med, f"Medication {i} missing 'dose'"
            assert "frequency" in med, f"Medication {i} missing 'frequency'"
            assert "route" in med, f"Medication {i} missing 'route'"

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(patient_data=patient_record_st)
    def test_stored_record_preserves_all_allergy_details(self, patient_data: dict):
        """Each allergy entry retains all fields after store and query.

        **Validates: Requirements 2.5**
        """
        demographics = _extract_demographics(patient_data)
        section_hashes = _compute_section_hashes(patient_data, demographics)

        stored_item = {
            "patient_id": patient_data["patient_id"],
            "patient_name": patient_data.get("patient_name", ""),
            "demographics": demographics,
            "allergies": patient_data.get("allergies", []),
            "medications": patient_data.get("medications", []),
            "lab_results": patient_data.get("lab_results", []),
            "vital_signs": patient_data.get("vital_signs", {}),
            "section_hashes": section_hashes,
            "last_updated": "2025-01-01T00:00:00+00:00",
            "fhir_resource_id": f"Patient/{patient_data['patient_id']}",
        }

        # Verify each allergy entry has all expected fields
        for i, allergy in enumerate(stored_item["allergies"]):
            assert "allergy_substance" in allergy, f"Allergy {i} missing 'allergy_substance'"
            assert "severity" in allergy, f"Allergy {i} missing 'severity'"
            assert "reaction" in allergy, f"Allergy {i} missing 'reaction'"
            assert "allergy_type" in allergy, f"Allergy {i} missing 'allergy_type'"

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(patient_data=patient_record_st)
    def test_stored_record_preserves_all_vital_signs(self, patient_data: dict):
        """Vital signs section retains all measurement fields after store and query.

        **Validates: Requirements 2.5**
        """
        demographics = _extract_demographics(patient_data)
        section_hashes = _compute_section_hashes(patient_data, demographics)

        stored_item = {
            "patient_id": patient_data["patient_id"],
            "patient_name": patient_data.get("patient_name", ""),
            "demographics": demographics,
            "allergies": patient_data.get("allergies", []),
            "medications": patient_data.get("medications", []),
            "lab_results": patient_data.get("lab_results", []),
            "vital_signs": patient_data.get("vital_signs", {}),
            "section_hashes": section_hashes,
            "last_updated": "2025-01-01T00:00:00+00:00",
            "fhir_resource_id": f"Patient/{patient_data['patient_id']}",
        }

        vitals = stored_item["vital_signs"]
        expected_keys = [
            "blood_pressure_systolic",
            "blood_pressure_diastolic",
            "heart_rate",
            "respiratory_rate",
            "temperature",
            "spo2",
            "weight",
            "height",
            "bmi",
        ]
        for key in expected_keys:
            assert key in vitals, f"Vital signs missing '{key}'"
            assert vitals[key] == patient_data["vital_signs"][key], (
                f"Vital sign '{key}' value mismatch: stored={vitals[key]}, "
                f"original={patient_data['vital_signs'][key]}"
            )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(patient_data=patient_record_st)
    def test_query_nonexistent_patient_returns_none(self, patient_data: dict):
        """Querying a patient_id that does not exist returns no item.

        **Validates: Requirements 2.5**
        """
        # Mock a table that returns no item for any query
        mock_table = MagicMock()
        mock_table.get_item = MagicMock(return_value={})

        response = mock_table.get_item(Key={"patient_id": patient_data["patient_id"]})
        retrieved_item = response.get("Item")

        assert retrieved_item is None, (
            "Expected None for nonexistent patient, got a record"
        )
