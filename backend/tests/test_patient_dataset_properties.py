"""
Property-based tests for patient dataset schema completeness and demographic diversity.

Uses Hypothesis to generate arbitrary patient records and verify that all
required fields are present and non-empty, as specified in the design document.

Feature: clinical-enhancements, Property 1: Patient record schema completeness
Feature: clinical-enhancements, Property 2: Dataset demographic diversity
"""

import json
import os
import glob
from datetime import date, datetime

import pytest
from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st


# ─── Constants ───────────────────────────────────────────────────────────────

# Path to patient data files
EHR_DATA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "knowledge-bases",
    "data",
    "ehr",
)

# Required top-level identity fields (Requirement 1.5)
REQUIRED_IDENTITY_FIELDS = [
    "patient_id",
    "patient_name",
    "mrn",
    "chart_number",
    "first_name",
    "last_name",
    "date_of_birth",
    "sex_at_birth",
    "gender_identity",
    "race",
    "ethnicity",
]

# Required medical_history sub-fields (Requirement 1.6)
REQUIRED_MEDICAL_HISTORY_FIELDS = [
    "past_diagnoses",
    "chronic_conditions",
    "past_surgeries",
    "past_hospitalizations",
    "problem_list",
    "family_history",
    "social_history",
    "vaccination_history",
    "smoking_status",
    "alcohol_use",
    "drug_use",
    "occupation",
]

# Required allergy entry fields (Requirement 1.8)
REQUIRED_ALLERGY_FIELDS = [
    "allergy_substance",
    "allergy_type",
    "reaction",
    "severity",
]

# Required medication entry fields (Requirement 1.9)
REQUIRED_MEDICATION_FIELDS = [
    "medication_name",
    "medication_code",
    "dose",
    "dose_unit",
    "route",
    "frequency",
    "adherence_notes",
]

# Required vital_signs sub-fields (Requirement 1.10)
REQUIRED_VITAL_SIGNS_FIELDS = [
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


# ─── Hypothesis Strategies ──────────────────────────────────────────────────

# Strategy for generating non-empty strings
non_empty_string_st = st.text(min_size=1, max_size=100).filter(lambda s: s.strip() != "")

# Strategy for patient_id (NNN format)
patient_id_st = st.integers(min_value=1, max_value=999).map(lambda n: f"{n:03d}")

# Strategy for date of birth (YYYY-MM-DD)
date_of_birth_st = st.dates(
    min_value=st.just(None).map(lambda _: __import__("datetime").date(1920, 1, 1)),
    max_value=st.just(None).map(lambda _: __import__("datetime").date(2022, 12, 31)),
).map(lambda d: d.isoformat())

# Strategy for sex_at_birth
sex_at_birth_st = st.sampled_from(["Male", "Female"])

# Strategy for a chronic condition object
chronic_condition_st = st.fixed_dictionaries({
    "condition": non_empty_string_st,
    "diagnosed": st.dates().map(lambda d: d.isoformat()),
    "status": st.sampled_from(["Active", "Resolved", "In remission"]),
    "notes": non_empty_string_st,
})

# Strategy for a surgery object
surgery_st = st.fixed_dictionaries({
    "procedure": non_empty_string_st,
    "date": st.dates().map(lambda d: d.isoformat()),
    "indication": non_empty_string_st,
})

# Strategy for a hospitalization object
hospitalization_st = st.fixed_dictionaries({
    "reason": non_empty_string_st,
    "date": st.dates().map(lambda d: d.isoformat()),
    "duration_days": st.integers(min_value=1, max_value=60),
})

# Strategy for a family history entry
family_history_st = st.fixed_dictionaries({
    "relation": st.sampled_from(["Father", "Mother", "Sibling", "Grandparent"]),
    "condition": non_empty_string_st,
    "age_at_onset": st.integers(min_value=1, max_value=100),
})

# Strategy for social history object
social_history_st = st.fixed_dictionaries({
    "living_situation": non_empty_string_st,
    "exercise": non_empty_string_st,
    "diet": non_empty_string_st,
})

# Strategy for vaccination entry
vaccination_st = st.fixed_dictionaries({
    "vaccine": non_empty_string_st,
    "date": st.dates().map(lambda d: d.isoformat()),
})

# Strategy for medical_history object
medical_history_st = st.fixed_dictionaries({
    "past_diagnoses": st.lists(non_empty_string_st, min_size=1, max_size=5),
    "chronic_conditions": st.lists(chronic_condition_st, min_size=0, max_size=3),
    "past_surgeries": st.lists(surgery_st, min_size=0, max_size=3),
    "past_hospitalizations": st.lists(hospitalization_st, min_size=0, max_size=3),
    "problem_list": st.lists(non_empty_string_st, min_size=1, max_size=5),
    "family_history": st.lists(family_history_st, min_size=0, max_size=4),
    "social_history": social_history_st,
    "vaccination_history": st.lists(vaccination_st, min_size=0, max_size=5),
    "smoking_status": non_empty_string_st,
    "alcohol_use": non_empty_string_st,
    "drug_use": non_empty_string_st,
    "occupation": non_empty_string_st,
})

# Strategy for an allergy entry
allergy_entry_st = st.fixed_dictionaries({
    "allergy_substance": non_empty_string_st,
    "allergy_type": st.sampled_from(["Drug", "Food", "Environmental"]),
    "reaction": non_empty_string_st,
    "severity": st.sampled_from(["Mild", "Moderate", "Severe"]),
})

# Strategy for a medication entry
medication_entry_st = st.fixed_dictionaries({
    "medication_name": non_empty_string_st,
    "medication_code": non_empty_string_st,
    "dose": non_empty_string_st,
    "dose_unit": st.sampled_from(["mg", "mcg", "mL", "tablet", "unit"]),
    "route": st.sampled_from(["Oral", "IV", "IM", "Subcutaneous", "Topical"]),
    "frequency": non_empty_string_st,
    "adherence_notes": non_empty_string_st,
})

# Strategy for vital_signs object
vital_signs_st = st.fixed_dictionaries({
    "blood_pressure_systolic": st.integers(min_value=70, max_value=220),
    "blood_pressure_diastolic": st.integers(min_value=40, max_value=140),
    "heart_rate": st.integers(min_value=40, max_value=200),
    "respiratory_rate": st.integers(min_value=8, max_value=40),
    "temperature": st.floats(min_value=35.0, max_value=42.0, allow_nan=False),
    "spo2": st.integers(min_value=70, max_value=100),
    "weight": st.floats(min_value=2.0, max_value=250.0, allow_nan=False),
    "height": st.floats(min_value=40.0, max_value=220.0, allow_nan=False),
    "bmi": st.floats(min_value=10.0, max_value=60.0, allow_nan=False),
})


# Strategy for a complete patient record
@st.composite
def patient_record_st(draw):
    """Generate a complete patient record following the schema."""
    return {
        "patient_id": draw(patient_id_st),
        "patient_name": draw(non_empty_string_st),
        "mrn": draw(non_empty_string_st),
        "chart_number": draw(non_empty_string_st),
        "first_name": draw(non_empty_string_st),
        "last_name": draw(non_empty_string_st),
        "date_of_birth": draw(st.dates().map(lambda d: d.isoformat())),
        "sex_at_birth": draw(sex_at_birth_st),
        "gender_identity": draw(non_empty_string_st),
        "race": draw(non_empty_string_st),
        "ethnicity": draw(non_empty_string_st),
        "medical_history": draw(medical_history_st),
        "allergies": draw(st.lists(allergy_entry_st, min_size=1, max_size=5)),
        "medications": draw(st.lists(medication_entry_st, min_size=1, max_size=8)),
        "vital_signs": draw(vital_signs_st),
    }


# ─── Helper Functions ────────────────────────────────────────────────────────


def load_patient_files() -> list[tuple[str, dict]]:
    """Load all patient JSON files from the EHR data directory."""
    pattern = os.path.join(EHR_DATA_DIR, "patient_*.json")
    files = sorted(glob.glob(pattern))
    results = []
    for filepath in files:
        with open(filepath, "r") as f:
            data = json.load(f)
        results.append((os.path.basename(filepath), data))
    return results


def assert_field_present_and_non_empty(record: dict, field: str, context: str = ""):
    """Assert a field is present in the record and is non-empty."""
    prefix = f"[{context}] " if context else ""
    assert field in record, f"{prefix}Missing required field: '{field}'"
    value = record[field]
    if isinstance(value, str):
        assert value.strip() != "", f"{prefix}Field '{field}' is empty string"
    elif isinstance(value, (list, dict)):
        # Lists and dicts are allowed to be empty for some fields (e.g., past_surgeries)
        pass
    elif isinstance(value, (int, float)):
        # Numeric values are always considered non-empty if present
        pass
    else:
        assert value is not None, f"{prefix}Field '{field}' is None"


def assert_identity_fields(record: dict, context: str = ""):
    """Verify all required identity fields are present and non-empty."""
    for field in REQUIRED_IDENTITY_FIELDS:
        assert_field_present_and_non_empty(record, field, context)


def assert_medical_history_fields(medical_history: dict, context: str = ""):
    """Verify all required medical_history sub-fields are present."""
    for field in REQUIRED_MEDICAL_HISTORY_FIELDS:
        assert field in medical_history, (
            f"[{context}] Missing medical_history field: '{field}'"
        )
        value = medical_history[field]
        # String fields must be non-empty
        if isinstance(value, str):
            assert value.strip() != "", (
                f"[{context}] medical_history.{field} is empty string"
            )


def assert_allergy_fields(allergy: dict, context: str = ""):
    """Verify all required allergy entry fields are present and non-empty."""
    for field in REQUIRED_ALLERGY_FIELDS:
        assert_field_present_and_non_empty(allergy, field, context)


def assert_medication_fields(medication: dict, context: str = ""):
    """Verify all required medication entry fields are present and non-empty."""
    for field in REQUIRED_MEDICATION_FIELDS:
        assert_field_present_and_non_empty(medication, field, context)


def assert_vital_signs_fields(vital_signs: dict, context: str = ""):
    """Verify all required vital_signs sub-fields are present and non-empty."""
    for field in REQUIRED_VITAL_SIGNS_FIELDS:
        assert field in vital_signs, (
            f"[{context}] Missing vital_signs field: '{field}'"
        )
        value = vital_signs[field]
        assert isinstance(value, (int, float)), (
            f"[{context}] vital_signs.{field} must be numeric, got {type(value).__name__}"
        )


# ─── Property 1: Patient record schema completeness ─────────────────────────
# Feature: clinical-enhancements, Property 1: Patient record schema completeness


@given(record=patient_record_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_generated_records_have_all_identity_fields(record):
    """
    Property 1 (identity fields): For any patient record, all required
    identity fields SHALL be present and non-empty.

    **Validates: Requirements 1.5**
    """
    assert_identity_fields(record, context=f"patient_id={record.get('patient_id')}")


@given(record=patient_record_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_generated_records_have_all_medical_history_fields(record):
    """
    Property 1 (medical history): For any patient record, all required
    medical_history sub-fields SHALL be present.

    **Validates: Requirements 1.6**
    """
    assert "medical_history" in record, "Missing 'medical_history' field"
    assert_medical_history_fields(
        record["medical_history"],
        context=f"patient_id={record.get('patient_id')}",
    )


@given(record=patient_record_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_generated_records_have_valid_allergy_entries(record):
    """
    Property 1 (allergies): For any patient record, each allergy entry
    SHALL have all required fields present and non-empty.

    **Validates: Requirements 1.8**
    """
    assert "allergies" in record, "Missing 'allergies' field"
    assert len(record["allergies"]) >= 1, "allergies list must have at least one entry"
    for i, allergy in enumerate(record["allergies"]):
        assert_allergy_fields(
            allergy,
            context=f"patient_id={record.get('patient_id')}, allergy[{i}]",
        )


@given(record=patient_record_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_generated_records_have_valid_medication_entries(record):
    """
    Property 1 (medications): For any patient record, each medication entry
    SHALL have all required fields present and non-empty.

    **Validates: Requirements 1.9**
    """
    assert "medications" in record, "Missing 'medications' field"
    assert len(record["medications"]) >= 1, "medications list must have at least one entry"
    for i, med in enumerate(record["medications"]):
        assert_medication_fields(
            med,
            context=f"patient_id={record.get('patient_id')}, medication[{i}]",
        )


@given(record=patient_record_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_generated_records_have_valid_vital_signs(record):
    """
    Property 1 (vital signs): For any patient record, all required
    vital_signs sub-fields SHALL be present and numeric.

    **Validates: Requirements 1.10**
    """
    assert "vital_signs" in record, "Missing 'vital_signs' field"
    assert_vital_signs_fields(
        record["vital_signs"],
        context=f"patient_id={record.get('patient_id')}",
    )


# ─── Property 1: Validate actual dataset files ──────────────────────────────
# Feature: clinical-enhancements, Property 1: Patient record schema completeness


# Load patient files for parametrized tests
_PATIENT_FILES = load_patient_files()


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_1_actual_dataset_identity_fields(filename, record):
    """
    Property 1 (actual dataset - identity): Each patient file in the dataset
    SHALL have all required identity fields present and non-empty.

    **Validates: Requirements 1.5**
    """
    assert_identity_fields(record, context=filename)


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_1_actual_dataset_medical_history_fields(filename, record):
    """
    Property 1 (actual dataset - medical history): Each patient file in the
    dataset SHALL have all required medical_history sub-fields present.

    **Validates: Requirements 1.6**
    """
    assert "medical_history" in record, f"[{filename}] Missing 'medical_history' field"
    assert_medical_history_fields(record["medical_history"], context=filename)


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_1_actual_dataset_allergy_entries(filename, record):
    """
    Property 1 (actual dataset - allergies): Each patient file in the dataset
    SHALL have allergy entries with all required fields present and non-empty.

    **Validates: Requirements 1.8**
    """
    assert "allergies" in record, f"[{filename}] Missing 'allergies' field"
    assert len(record["allergies"]) >= 1, (
        f"[{filename}] allergies list must have at least one entry"
    )
    for i, allergy in enumerate(record["allergies"]):
        assert_allergy_fields(allergy, context=f"{filename}, allergy[{i}]")


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_1_actual_dataset_medication_entries(filename, record):
    """
    Property 1 (actual dataset - medications): Each patient file in the dataset
    SHALL have medication entries with all required fields present and non-empty.

    **Validates: Requirements 1.9**
    """
    assert "medications" in record, f"[{filename}] Missing 'medications' field"
    assert len(record["medications"]) >= 1, (
        f"[{filename}] medications list must have at least one entry"
    )
    for i, med in enumerate(record["medications"]):
        assert_medication_fields(med, context=f"{filename}, medication[{i}]")


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_1_actual_dataset_vital_signs(filename, record):
    """
    Property 1 (actual dataset - vital signs): Each patient file in the dataset
    SHALL have all required vital_signs sub-fields present and numeric.

    **Validates: Requirements 1.10**
    """
    assert "vital_signs" in record, f"[{filename}] Missing 'vital_signs' field"
    assert_vital_signs_fields(record["vital_signs"], context=filename)


# ─── Property 2: Dataset demographic diversity ───────────────────────────────
# Feature: clinical-enhancements, Property 2: Dataset demographic diversity


def compute_age(date_of_birth: str) -> int:
    """Compute age in years from a date_of_birth string (YYYY-MM-DD)."""
    dob = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return age


def classify_age_group(age: int) -> str:
    """Classify age into pediatric, adult, or geriatric group."""
    if 2 <= age <= 17:
        return "pediatric"
    elif 18 <= age <= 64:
        return "adult"
    elif age >= 65:
        return "geriatric"
    return "infant"


def test_property_2_dataset_has_exactly_20_records():
    """
    Property 2 (record count): The patient dataset SHALL contain exactly
    20 synthetic patient records with unique patient_id values.

    **Validates: Requirements 1.1**
    """
    patient_files = load_patient_files()
    assert len(patient_files) == 20, (
        f"Expected exactly 20 patient records, found {len(patient_files)}"
    )
    # Verify unique patient_ids
    patient_ids = [record["patient_id"] for _, record in patient_files]
    assert len(set(patient_ids)) == 20, (
        f"Expected 20 unique patient_ids, found {len(set(patient_ids))} unique values"
    )


def test_property_2_dataset_covers_all_age_groups():
    """
    Property 2 (age groups): The dataset SHALL include patients spanning
    pediatric (ages 2-17), adult (ages 18-64), and geriatric (ages 65+)
    age groups.

    **Validates: Requirements 1.2**
    """
    patient_files = load_patient_files()
    age_groups_found = set()

    for filename, record in patient_files:
        age = compute_age(record["date_of_birth"])
        group = classify_age_group(age)
        if group in ("pediatric", "adult", "geriatric"):
            age_groups_found.add(group)

    required_groups = {"pediatric", "adult", "geriatric"}
    missing_groups = required_groups - age_groups_found
    assert not missing_groups, (
        f"Dataset is missing patients in age group(s): {missing_groups}. "
        f"Found groups: {age_groups_found}"
    )


def test_property_2_dataset_has_at_least_5_racial_backgrounds():
    """
    Property 2 (racial diversity): The dataset SHALL include patients of
    at least 5 distinct racial backgrounds.

    **Validates: Requirements 1.3**
    """
    patient_files = load_patient_files()
    races = set()

    for _, record in patient_files:
        race = record.get("race", "").strip()
        if race:
            races.add(race)

    assert len(races) >= 5, (
        f"Expected at least 5 distinct racial backgrounds, found {len(races)}: {sorted(races)}"
    )


def test_property_2_dataset_has_at_least_3_ethnic_groups():
    """
    Property 2 (ethnic diversity): The dataset SHALL include patients of
    at least 3 distinct ethnic groups.

    **Validates: Requirements 1.3**
    """
    patient_files = load_patient_files()
    ethnicities = set()

    for _, record in patient_files:
        ethnicity = record.get("ethnicity", "").strip()
        if ethnicity:
            ethnicities.add(ethnicity)

    assert len(ethnicities) >= 3, (
        f"Expected at least 3 distinct ethnic groups, found {len(ethnicities)}: {sorted(ethnicities)}"
    )


def test_property_2_dataset_has_both_sexes():
    """
    Property 2 (sex diversity): The dataset SHALL include patients of both
    male and female sex_at_birth values.

    **Validates: Requirements 1.4**
    """
    patient_files = load_patient_files()
    sexes = set()

    for _, record in patient_files:
        sex = record.get("sex_at_birth", "").strip()
        if sex:
            sexes.add(sex)

    assert "Male" in sexes, "Dataset is missing patients with sex_at_birth='Male'"
    assert "Female" in sexes, "Dataset is missing patients with sex_at_birth='Female'"


# ─── Property 2: Hypothesis-based dataset diversity verification ─────────────
# Feature: clinical-enhancements, Property 2: Dataset demographic diversity


# Strategy for generating a diverse dataset of 20 patient records
@st.composite
def diverse_patient_dataset_st(draw):
    """
    Generate a dataset of 20 patient records that satisfies the diversity
    constraints. This strategy ensures the generated dataset has the required
    demographic coverage by construction: forced slots guarantee minimum
    diversity, remaining slots are randomized.
    """
    races = [
        "White", "Black or African American", "Asian",
        "American Indian or Alaska Native", "Native Hawaiian or Pacific Islander",
        "Middle Eastern or North African", "Multiracial",
    ]
    ethnicities = [
        "French", "Nigerian", "Japanese", "Hispanic/Latino",
        "South Asian", "Middle Eastern", "Scandinavian",
    ]
    today = date.today()

    def dob_for_age(age: int) -> date:
        """Compute a DOB that guarantees the given age today."""
        # Match today's month/day so compute_age() resolves to exactly `age`.
        return date(today.year - age, today.month, min(today.day, 28))

    records = []

    # Slot 0: forced pediatric (age 2-17)
    pediatric_age = draw(st.integers(min_value=2, max_value=17))
    # Slot 1: forced adult (age 18-64)
    adult_age = draw(st.integers(min_value=18, max_value=64))
    # Slot 2: forced geriatric (age 65+)
    geriatric_age = draw(st.integers(min_value=65, max_value=90))

    forced_ages = [pediatric_age, adult_age, geriatric_age]

    # Ensure at least 5 distinct races (slots 0-4)
    forced_races = draw(
        st.lists(
            st.sampled_from(races),
            min_size=5,
            max_size=5,
            unique=True,
        )
    )
    # Ensure at least 3 distinct ethnicities (slots 0-2)
    forced_ethnicities = draw(
        st.lists(
            st.sampled_from(ethnicities),
            min_size=3,
            max_size=3,
            unique=True,
        )
    )

    for i in range(20):
        patient_id = f"{i + 1:03d}"
        # Alternate sex to guarantee both present
        sex = "Male" if i % 2 == 0 else "Female"

        # Age assignment
        if i < 3:
            age = forced_ages[i]
        else:
            age = draw(st.integers(min_value=2, max_value=90))

        dob = dob_for_age(age)

        # Race assignment
        if i < 5:
            race = forced_races[i]
        else:
            race = draw(st.sampled_from(races))

        # Ethnicity assignment
        if i < 3:
            ethnicity = forced_ethnicities[i]
        else:
            ethnicity = draw(st.sampled_from(ethnicities))

        records.append({
            "patient_id": patient_id,
            "date_of_birth": dob.isoformat(),
            "sex_at_birth": sex,
            "race": race,
            "ethnicity": ethnicity,
        })

    return records


@given(dataset=diverse_patient_dataset_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_any_valid_dataset_has_all_age_groups(dataset):
    """
    Property 2 (generated dataset - age groups): For any valid patient dataset
    of 20 records, the dataset SHALL contain at least one patient in each age
    group (pediatric 2-17, adult 18-64, geriatric 65+).

    **Validates: Requirements 1.2**
    """
    assert len(dataset) == 20
    age_groups = set()
    for record in dataset:
        age = compute_age(record["date_of_birth"])
        group = classify_age_group(age)
        if group in ("pediatric", "adult", "geriatric"):
            age_groups.add(group)

    assert "pediatric" in age_groups, "Generated dataset missing pediatric patients"
    assert "adult" in age_groups, "Generated dataset missing adult patients"
    assert "geriatric" in age_groups, "Generated dataset missing geriatric patients"


@given(dataset=diverse_patient_dataset_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_any_valid_dataset_has_racial_diversity(dataset):
    """
    Property 2 (generated dataset - racial diversity): For any valid patient
    dataset of 20 records, the dataset SHALL contain at least 5 distinct
    racial backgrounds.

    **Validates: Requirements 1.3**
    """
    assert len(dataset) == 20
    races = {record["race"] for record in dataset}
    assert len(races) >= 5, (
        f"Generated dataset has only {len(races)} distinct races: {sorted(races)}"
    )


@given(dataset=diverse_patient_dataset_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_any_valid_dataset_has_ethnic_diversity(dataset):
    """
    Property 2 (generated dataset - ethnic diversity): For any valid patient
    dataset of 20 records, the dataset SHALL contain at least 3 distinct
    ethnic groups.

    **Validates: Requirements 1.3**
    """
    assert len(dataset) == 20
    ethnicities = {record["ethnicity"] for record in dataset}
    assert len(ethnicities) >= 3, (
        f"Generated dataset has only {len(ethnicities)} distinct ethnicities: {sorted(ethnicities)}"
    )


@given(dataset=diverse_patient_dataset_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_any_valid_dataset_has_both_sexes(dataset):
    """
    Property 2 (generated dataset - sex diversity): For any valid patient
    dataset of 20 records, the dataset SHALL contain both male and female
    sex_at_birth values.

    **Validates: Requirements 1.4**
    """
    assert len(dataset) == 20
    sexes = {record["sex_at_birth"] for record in dataset}
    assert "Male" in sexes, "Generated dataset missing Male sex_at_birth"
    assert "Female" in sexes, "Generated dataset missing Female sex_at_birth"


# ─── Property 3: Conditional pregnancy history ───────────────────────────────
# Feature: clinical-enhancements, Property 3: Conditional pregnancy history


def is_female_of_reproductive_age(record: dict) -> bool:
    """
    Determine if a patient is female of reproductive age (12-55).
    Returns True if sex_at_birth is 'Female' and age is within 12-55.
    """
    if record.get("sex_at_birth") != "Female":
        return False
    dob_str = record.get("date_of_birth", "")
    if not dob_str:
        return False
    age = compute_age(dob_str)
    return 12 <= age <= 55


# Strategy for generating female patients of reproductive age
@st.composite
def female_reproductive_age_patient_st(draw):
    """
    Generate a patient record where sex_at_birth is 'Female' and age is
    within reproductive range (12-55). The medical_history includes
    pregnancy_history to satisfy the property.
    """
    today = date.today()
    # Generate age in reproductive range (use 54 max to avoid boundary issues
    # where the computed age could be 55+1=56 due to DOB calculation)
    age = draw(st.integers(min_value=12, max_value=54))
    # Compute DOB that guarantees the given age (subtract age years, use Jan 1
    # to ensure the birthday has already passed this year)
    dob = date(today.year - age, 1, 1)

    pregnancy_history_st = st.fixed_dictionaries({
        "gravida": st.integers(min_value=0, max_value=10),
        "para": st.integers(min_value=0, max_value=10),
        "last_menstrual_period": st.dates().map(lambda d: d.isoformat()),
        "pregnancy_status": st.sampled_from([
            "Not pregnant", "Pregnant", "Unknown",
        ]),
        "contraception": st.sampled_from([
            "Oral contraceptive pill", "IUD", "None", "Condoms",
            "Implant", "Injection", "Natural family planning",
        ]),
    })

    # Build medical history with pregnancy_history included
    med_history = draw(medical_history_st)
    med_history["pregnancy_history"] = draw(pregnancy_history_st)

    return {
        "patient_id": draw(patient_id_st),
        "patient_name": draw(non_empty_string_st),
        "mrn": draw(non_empty_string_st),
        "chart_number": draw(non_empty_string_st),
        "first_name": draw(non_empty_string_st),
        "last_name": draw(non_empty_string_st),
        "date_of_birth": dob.isoformat(),
        "sex_at_birth": "Female",
        "gender_identity": draw(non_empty_string_st),
        "race": draw(non_empty_string_st),
        "ethnicity": draw(non_empty_string_st),
        "medical_history": med_history,
        "allergies": draw(st.lists(allergy_entry_st, min_size=1, max_size=5)),
        "medications": draw(st.lists(medication_entry_st, min_size=1, max_size=8)),
        "vital_signs": draw(vital_signs_st),
    }


@given(record=female_reproductive_age_patient_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_generated_female_reproductive_age_has_pregnancy_history(record):
    """
    Property 3 (generated): For any patient record where sex_at_birth is
    'Female' and age is within reproductive range (12-55), the medical_history
    SHALL include a pregnancy_history field.

    **Validates: Requirements 1.7**
    """
    # Confirm precondition: patient is female of reproductive age
    assert record["sex_at_birth"] == "Female"
    age = compute_age(record["date_of_birth"])
    assert 12 <= age <= 55, f"Age {age} is outside reproductive range"

    # Verify property: pregnancy_history must be present in medical_history
    medical_history = record.get("medical_history", {})
    assert "pregnancy_history" in medical_history, (
        f"patient_id={record['patient_id']}: Female patient aged {age} "
        f"is missing 'pregnancy_history' in medical_history"
    )
    # Verify pregnancy_history is not None or empty
    pregnancy_history = medical_history["pregnancy_history"]
    assert pregnancy_history is not None, (
        f"patient_id={record['patient_id']}: pregnancy_history is None"
    )
    assert isinstance(pregnancy_history, dict), (
        f"patient_id={record['patient_id']}: pregnancy_history must be a dict, "
        f"got {type(pregnancy_history).__name__}"
    )


# ─── Property 3: Validate actual dataset files ──────────────────────────────
# Feature: clinical-enhancements, Property 3: Conditional pregnancy history


def _get_female_reproductive_age_patients() -> list[tuple[str, dict]]:
    """Filter patient files to only females of reproductive age (12-55)."""
    all_patients = load_patient_files()
    return [
        (filename, record)
        for filename, record in all_patients
        if is_female_of_reproductive_age(record)
    ]


_FEMALE_REPRODUCTIVE_AGE_PATIENTS = _get_female_reproductive_age_patients()


@pytest.mark.parametrize(
    "filename,record",
    _FEMALE_REPRODUCTIVE_AGE_PATIENTS,
    ids=[f[0] for f in _FEMALE_REPRODUCTIVE_AGE_PATIENTS],
)
def test_property_3_actual_dataset_female_reproductive_age_has_pregnancy_history(
    filename, record
):
    """
    Property 3 (actual dataset): Each female patient of reproductive age
    (12-55) in the dataset SHALL have a pregnancy_history field in
    medical_history.

    **Validates: Requirements 1.7**
    """
    age = compute_age(record["date_of_birth"])
    medical_history = record.get("medical_history", {})

    assert "pregnancy_history" in medical_history, (
        f"[{filename}] Female patient aged {age} is missing "
        f"'pregnancy_history' in medical_history"
    )

    pregnancy_history = medical_history["pregnancy_history"]
    assert pregnancy_history is not None, (
        f"[{filename}] pregnancy_history is None"
    )
    assert isinstance(pregnancy_history, dict), (
        f"[{filename}] pregnancy_history must be a dict, "
        f"got {type(pregnancy_history).__name__}"
    )
    # Verify it has meaningful content (not just an empty dict)
    assert len(pregnancy_history) > 0, (
        f"[{filename}] pregnancy_history is an empty dict"
    )


def test_property_3_dataset_has_at_least_one_female_of_reproductive_age():
    """
    Property 3 (precondition check): The dataset SHALL contain at least one
    female patient of reproductive age (12-55) to make Property 3 testable.

    **Validates: Requirements 1.7**
    """
    female_reproductive = _get_female_reproductive_age_patients()
    assert len(female_reproductive) >= 1, (
        "Dataset has no female patients of reproductive age (12-55). "
        "Property 3 cannot be validated without such patients."
    )


# ─── Property 4: Patient file naming convention ──────────────────────────────
# Feature: clinical-enhancements, Property 4: Patient file naming convention


import re


def normalize_name_for_filename(name: str) -> str:
    """
    Normalize a name (first_name or last_name) to the format used in filenames.
    Converts to lowercase, replaces hyphens and spaces with underscores,
    and removes apostrophes.
    """
    return name.lower().replace("-", "_").replace(" ", "_").replace("'", "").replace("\u2019", "")


def expected_filename(record: dict) -> str:
    """
    Compute the expected filename for a patient record based on its fields.
    Pattern: patient_NNN_firstname_lastname.json
    """
    patient_id = record["patient_id"]
    first_name = normalize_name_for_filename(record["first_name"])
    last_name = normalize_name_for_filename(record["last_name"])
    return f"patient_{patient_id}_{first_name}_{last_name}.json"


# Regex pattern for the naming convention
PATIENT_FILENAME_PATTERN = re.compile(
    r"^patient_(\d{3})_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*\.json$"
)


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_4_actual_dataset_filename_matches_pattern(filename, record):
    """
    Property 4 (actual dataset - pattern): For any patient record file in the
    dataset, the filename SHALL match the pattern
    `patient_NNN_firstname_lastname.json` where NNN is a zero-padded 3-digit
    number.

    **Validates: Requirements 1.11**
    """
    assert PATIENT_FILENAME_PATTERN.match(filename), (
        f"Filename '{filename}' does not match expected pattern "
        f"'patient_NNN_firstname_lastname.json'"
    )


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_4_actual_dataset_filename_nnn_matches_patient_id(filename, record):
    """
    Property 4 (actual dataset - patient_id match): For any patient record file
    in the dataset, the NNN in the filename SHALL match the patient_id field
    inside the JSON file.

    **Validates: Requirements 1.11**
    """
    # Extract NNN from filename
    match = PATIENT_FILENAME_PATTERN.match(filename)
    assert match, (
        f"Filename '{filename}' does not match expected pattern"
    )
    filename_nnn = match.group(1)
    record_patient_id = record.get("patient_id", "")

    assert filename_nnn == record_patient_id, (
        f"Filename NNN '{filename_nnn}' does not match patient_id "
        f"'{record_patient_id}' in file '{filename}'"
    )


@pytest.mark.parametrize(
    "filename,record",
    _PATIENT_FILES,
    ids=[f[0] for f in _PATIENT_FILES],
)
def test_property_4_actual_dataset_filename_matches_name_fields(filename, record):
    """
    Property 4 (actual dataset - name match): For any patient record file in
    the dataset, the firstname and lastname parts of the filename SHALL match
    the normalized first_name and last_name fields from the record.

    **Validates: Requirements 1.11**
    """
    expected = expected_filename(record)
    assert filename == expected, (
        f"Filename '{filename}' does not match expected filename '{expected}' "
        f"derived from first_name='{record.get('first_name')}', "
        f"last_name='{record.get('last_name')}', "
        f"patient_id='{record.get('patient_id')}'"
    )


# ─── Property 4: Hypothesis-based naming convention verification ─────────────
# Feature: clinical-enhancements, Property 4: Patient file naming convention


# Strategy for generating names that follow the naming convention
name_part_st = st.from_regex(r"[A-Z][a-z]+", fullmatch=True)

# Strategy for generating first names (may include hyphens for compound names)
first_name_st = st.one_of(
    name_part_st,
    st.tuples(name_part_st, name_part_st).map(lambda t: f"{t[0]}-{t[1]}"),
)

# Strategy for generating last names (may include hyphens or spaces)
last_name_st = st.one_of(
    name_part_st,
    st.tuples(name_part_st, name_part_st).map(lambda t: f"{t[0]}-{t[1]}"),
    st.tuples(name_part_st, name_part_st).map(lambda t: f"{t[0]} {t[1]}"),
)


@st.composite
def patient_with_filename_st(draw):
    """
    Generate a patient record with patient_id, first_name, and last_name,
    along with the expected filename.
    """
    pid = draw(patient_id_st)
    first = draw(first_name_st)
    last = draw(last_name_st)

    record = {
        "patient_id": pid,
        "first_name": first,
        "last_name": last,
    }
    fname = expected_filename(record)
    return record, fname


@given(data=patient_with_filename_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_generated_filenames_match_pattern(data):
    """
    Property 4 (generated): For any patient record with valid patient_id,
    first_name, and last_name, the derived filename SHALL match the pattern
    `patient_NNN_firstname_lastname.json` where NNN is a zero-padded 3-digit
    number.

    **Validates: Requirements 1.11**
    """
    record, filename = data

    # Verify the filename matches the regex pattern
    assert PATIENT_FILENAME_PATTERN.match(filename), (
        f"Generated filename '{filename}' does not match pattern "
        f"'patient_NNN_firstname_lastname.json' for record with "
        f"patient_id='{record['patient_id']}', "
        f"first_name='{record['first_name']}', "
        f"last_name='{record['last_name']}'"
    )

    # Verify NNN in filename matches patient_id
    match = PATIENT_FILENAME_PATTERN.match(filename)
    assert match.group(1) == record["patient_id"], (
        f"NNN in filename '{filename}' does not match patient_id "
        f"'{record['patient_id']}'"
    )


@given(data=patient_with_filename_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_generated_filenames_are_deterministic(data):
    """
    Property 4 (determinism): For any patient record, computing the filename
    twice SHALL produce the same result — the naming convention is deterministic.

    **Validates: Requirements 1.11**
    """
    record, filename = data

    # Compute filename again
    filename_again = expected_filename(record)
    assert filename == filename_again, (
        f"Filename computation is not deterministic: "
        f"'{filename}' != '{filename_again}'"
    )


@given(data=patient_with_filename_st())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_generated_filenames_contain_lowercase_only(data):
    """
    Property 4 (lowercase): For any patient record, the firstname and lastname
    parts of the derived filename SHALL be entirely lowercase with underscores
    (no uppercase, hyphens, or spaces).

    **Validates: Requirements 1.11**
    """
    record, filename = data

    # Remove the 'patient_NNN_' prefix and '.json' suffix to get name parts
    name_part = filename[len("patient_NNN_"):]  # Remove prefix (12 chars)
    name_part = name_part[:-len(".json")]  # Remove .json suffix

    # The name part should only contain lowercase letters, digits, and underscores
    assert re.match(r"^[a-z0-9_]+$", name_part), (
        f"Name part '{name_part}' in filename '{filename}' contains "
        f"characters other than lowercase letters, digits, and underscores"
    )
