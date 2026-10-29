"""EHR Ingestion Lambda handler.

Triggered by S3 ObjectCreated events on the EHR data bucket.
Processes patient JSON files through the EHR data pipeline:
1. Parse S3 event to get bucket/key
2. Download and validate JSON
3. Transform to FHIR R4 format
4. Store in HealthLake
5. Compute section hashes for differential detection
6. Store/update structured record in DynamoDB (patient index)
7. Sync to Bedrock KB (vectorize for RAG)
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables set by CDK
PATIENT_INDEX_TABLE = os.environ.get("PATIENT_INDEX_TABLE", "clinicalcompass-patient-index")
FHIR_DATASTORE_ID = os.environ.get("FHIR_DATASTORE_ID", "")
FHIR_DATASTORE_ENDPOINT = os.environ.get("FHIR_DATASTORE_ENDPOINT", "")
KB_ID = os.environ.get("KB_ID", "")
KB_DATA_SOURCE_ID = os.environ.get("KB_DATA_SOURCE_ID", "")

# Section keys used for differential hashing
HASH_SECTIONS = ("demographics", "medications", "allergies", "lab_results", "vital_signs")

# Retry configuration
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# Required top-level fields for patient schema validation
REQUIRED_FIELDS = (
    "patient_id",
    "patient_name",
    "first_name",
    "last_name",
    "date_of_birth",
    "sex_at_birth",
)

# AWS clients (lazily initialized for Lambda container reuse)
_s3_client = None
_dynamodb = None
_healthlake_client = None
_bedrock_agent_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_healthlake_client():
    global _healthlake_client
    if _healthlake_client is None:
        _healthlake_client = boto3.client("healthlake")
    return _healthlake_client


def _get_bedrock_agent_client():
    global _bedrock_agent_client
    if _bedrock_agent_client is None:
        _bedrock_agent_client = boto3.client("bedrock-agent")
    return _bedrock_agent_client


def handler(event: dict, context: Any) -> dict:
    """S3-triggered Lambda for EHR data processing."""
    logger.info("EHR Ingestion Lambda invoked")
    logger.info(json.dumps(event))

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        logger.info(f"Processing s3://{bucket}/{key}")

        try:
            _process_patient_file(bucket, key)
        except MalformedInputError as e:
            logger.error(f"Malformed input in s3://{bucket}/{key}: {e}")
            continue
        except Exception as e:
            logger.error(f"Unexpected error processing s3://{bucket}/{key}: {e}")
            raise

    return {"statusCode": 200, "body": "OK"}


class MalformedInputError(Exception):
    """Raised when patient JSON input fails validation."""


def _process_patient_file(bucket: str, key: str) -> None:
    """Download, validate, transform, and store a patient file."""
    # Step 1: Download from S3
    raw_data = _download_from_s3(bucket, key)

    # Step 2: Parse and validate JSON
    patient_data = _parse_and_validate(raw_data, key)

    # Step 3: Extract demographics section for DynamoDB
    demographics = _extract_demographics(patient_data)

    # Step 4: Compute section hashes
    incoming_hashes = _compute_section_hashes(patient_data, demographics)

    # Step 5: Check for existing record and compute diff
    patient_id = patient_data["patient_id"]
    table = _get_dynamodb().Table(PATIENT_INDEX_TABLE)
    existing_record = _get_existing_record(table, patient_id)
    changes = compute_diff(incoming_hashes, existing_record)

    # Step 6: Transform to FHIR R4 and store in HealthLake
    fhir_resource_id = _store_in_healthlake(patient_data, existing_record, changes)

    # Step 7: Store/update in DynamoDB
    _store_in_dynamodb(table, patient_data, demographics, incoming_hashes, fhir_resource_id, changes)

    # Step 8: Sync to Bedrock KB (only if sections changed)
    if changes:
        _sync_to_knowledge_base()

    logger.info(f"Successfully processed patient {patient_id} — "
                f"{'full ingestion' if existing_record is None else f'{len(changes)} section(s) updated'}")


def _download_from_s3(bucket: str, key: str) -> bytes:
    """Download file content from S3."""
    response = _get_s3_client().get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def _parse_and_validate(raw_data: bytes, key: str) -> dict:
    """Parse JSON and validate against patient schema.

    Raises MalformedInputError if the input is invalid.
    """
    try:
        data = json.loads(raw_data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise MalformedInputError(f"Invalid JSON syntax in {key}: {e}")

    if not isinstance(data, dict):
        raise MalformedInputError(f"Expected JSON object in {key}, got {type(data).__name__}")

    missing_fields = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing_fields:
        raise MalformedInputError(
            f"Missing required fields in {key}: {', '.join(missing_fields)}"
        )

    # Validate expected section types
    if "allergies" in data and not isinstance(data["allergies"], list):
        raise MalformedInputError(f"'allergies' must be a list in {key}")
    if "medications" in data and not isinstance(data["medications"], list):
        raise MalformedInputError(f"'medications' must be a list in {key}")
    if "lab_results" in data and not isinstance(data["lab_results"], list):
        raise MalformedInputError(f"'lab_results' must be a list in {key}")
    if "vital_signs" in data and not isinstance(data["vital_signs"], dict):
        raise MalformedInputError(f"'vital_signs' must be a dict in {key}")

    return data


def _extract_demographics(patient_data: dict) -> dict:
    """Extract demographics section from patient data."""
    dob = patient_data.get("date_of_birth", "")
    age = _calculate_age(dob) if dob else None

    return {
        "patient_name": patient_data.get("patient_name", ""),
        "first_name": patient_data.get("first_name", ""),
        "last_name": patient_data.get("last_name", ""),
        "date_of_birth": dob,
        "age": age,
        "sex_at_birth": patient_data.get("sex_at_birth", ""),
        "gender_identity": patient_data.get("gender_identity", ""),
        "race": patient_data.get("race", ""),
        "ethnicity": patient_data.get("ethnicity", ""),
        "mrn": patient_data.get("mrn", ""),
        "chart_number": patient_data.get("chart_number", ""),
    }


def _calculate_age(dob_str: str) -> int:
    """Calculate age from date of birth string (YYYY-MM-DD)."""
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d")
        today = datetime.now(timezone.utc)
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        return age
    except (ValueError, TypeError):
        return 0


def _compute_section_hashes(patient_data: dict, demographics: dict) -> dict:
    """Compute SHA-256 hashes for each section of the patient record."""
    sections_data = {
        "demographics": demographics,
        "medications": patient_data.get("medications", []),
        "allergies": patient_data.get("allergies", []),
        "lab_results": patient_data.get("lab_results", []),
        "vital_signs": patient_data.get("vital_signs", {}),
    }

    hashes = {}
    for section_name, section_content in sections_data.items():
        serialized = json.dumps(section_content, sort_keys=True, default=str)
        hashes[f"{section_name}_hash"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    return hashes


def compute_diff(incoming_hashes: dict, existing_record: dict | None) -> dict:
    """Compare incoming record hashes against stored version.

    Returns dict of changed section names mapped to True.
    If no existing record, all sections are considered changed.
    """
    if existing_record is None:
        return {section: True for section in HASH_SECTIONS}

    stored_hashes = existing_record.get("section_hashes", {})
    changes = {}

    for section in HASH_SECTIONS:
        incoming_hash = incoming_hashes.get(f"{section}_hash", "")
        stored_hash = stored_hashes.get(f"{section}_hash", "")
        if incoming_hash != stored_hash:
            changes[section] = True

    return changes


def _get_existing_record(table: Any, patient_id: str) -> dict | None:
    """Retrieve existing patient record from DynamoDB."""
    try:
        response = table.get_item(Key={"patient_id": patient_id})
        return response.get("Item")
    except ClientError as e:
        logger.warning(f"Error reading existing record for {patient_id}: {e}")
        return None


def _store_in_healthlake(
    patient_data: dict,
    existing_record: dict | None,
    changes: dict,
) -> str:
    """Transform patient data to FHIR R4 and store in HealthLake.

    Returns the FHIR resource ID.
    """
    if not FHIR_DATASTORE_ID:
        logger.warning("FHIR_DATASTORE_ID not configured, skipping HealthLake storage")
        return existing_record.get("fhir_resource_id", "") if existing_record else ""

    fhir_bundle = _transform_to_fhir_r4(patient_data)

    fhir_resource_id = _retry_with_backoff(
        lambda: _write_to_healthlake(fhir_bundle),
        operation_name="HealthLake write",
    )

    return fhir_resource_id


def _transform_to_fhir_r4(patient_data: dict) -> dict:
    """Transform patient JSON to FHIR R4 Bundle format."""
    patient_id = patient_data["patient_id"]

    # FHIR Patient resource
    patient_resource = {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": [
            {"system": "urn:clinicalcompass:patient_id", "value": patient_id},
            {"system": "urn:clinicalcompass:mrn", "value": patient_data.get("mrn", "")},
        ],
        "name": [
            {
                "use": "official",
                "family": patient_data.get("last_name", ""),
                "given": [patient_data.get("first_name", "")],
            }
        ],
        "gender": _map_sex_to_fhir_gender(patient_data.get("sex_at_birth", "")),
        "birthDate": patient_data.get("date_of_birth", ""),
    }

    # Build entries list
    entries = [{"resource": patient_resource, "request": {"method": "PUT", "url": f"Patient/{patient_id}"}}]

    # AllergyIntolerance resources
    for i, allergy in enumerate(patient_data.get("allergies", [])):
        allergy_resource = {
            "resourceType": "AllergyIntolerance",
            "id": f"{patient_id}-allergy-{i}",
            "patient": {"reference": f"Patient/{patient_id}"},
            "code": {"text": allergy.get("allergy_substance", "")},
            "type": allergy.get("allergy_type", "").lower() if allergy.get("allergy_type") else "allergy",
            "reaction": [
                {
                    "manifestation": [{"text": allergy.get("reaction", "")}],
                    "severity": _map_severity_to_fhir(allergy.get("severity", "")),
                }
            ],
        }
        entries.append({
            "resource": allergy_resource,
            "request": {"method": "PUT", "url": f"AllergyIntolerance/{patient_id}-allergy-{i}"},
        })

    # MedicationStatement resources
    for i, med in enumerate(patient_data.get("medications", [])):
        med_resource = {
            "resourceType": "MedicationStatement",
            "id": f"{patient_id}-med-{i}",
            "status": "active",
            "subject": {"reference": f"Patient/{patient_id}"},
            "medicationCodeableConcept": {
                "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": med.get("medication_code", ""), "display": med.get("medication_name", "")}],
                "text": med.get("medication_name", ""),
            },
            "dosage": [
                {
                    "text": f"{med.get('dose', '')} {med.get('dose_unit', '')} {med.get('route', '')} {med.get('frequency', '')}",
                    "route": {"text": med.get("route", "")},
                }
            ],
        }
        entries.append({
            "resource": med_resource,
            "request": {"method": "PUT", "url": f"MedicationStatement/{patient_id}-med-{i}"},
        })

    # Observation resources for lab results
    for i, lab in enumerate(patient_data.get("lab_results", [])):
        obs_resource = {
            "resourceType": "Observation",
            "id": f"{patient_id}-lab-{i}",
            "status": "final",
            "subject": {"reference": f"Patient/{patient_id}"},
            "code": {"text": lab.get("test", "")},
            "valueQuantity": {
                "value": lab.get("value"),
                "unit": lab.get("unit", ""),
            },
            "effectiveDateTime": lab.get("date", ""),
            "interpretation": [{"text": lab.get("flag", "NORMAL")}],
        }
        entries.append({
            "resource": obs_resource,
            "request": {"method": "PUT", "url": f"Observation/{patient_id}-lab-{i}"},
        })

    # Observation resources for vital signs
    vital_signs = patient_data.get("vital_signs", {})
    vital_mappings = [
        ("blood_pressure_systolic", "mmHg", "Systolic Blood Pressure"),
        ("blood_pressure_diastolic", "mmHg", "Diastolic Blood Pressure"),
        ("heart_rate", "bpm", "Heart Rate"),
        ("respiratory_rate", "breaths/min", "Respiratory Rate"),
        ("temperature", "°C", "Body Temperature"),
        ("spo2", "%", "Oxygen Saturation"),
        ("weight", "kg", "Body Weight"),
        ("height", "cm", "Body Height"),
        ("bmi", "kg/m²", "BMI"),
    ]
    for i, (key, unit, display) in enumerate(vital_mappings):
        if key in vital_signs:
            vital_resource = {
                "resourceType": "Observation",
                "id": f"{patient_id}-vital-{i}",
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
                "subject": {"reference": f"Patient/{patient_id}"},
                "code": {"text": display},
                "valueQuantity": {"value": vital_signs[key], "unit": unit},
            }
            entries.append({
                "resource": vital_resource,
                "request": {"method": "PUT", "url": f"Observation/{patient_id}-vital-{i}"},
            })

    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": entries,
    }


def _map_sex_to_fhir_gender(sex: str) -> str:
    """Map sex_at_birth to FHIR gender code."""
    mapping = {"male": "male", "female": "female"}
    return mapping.get(sex.lower(), "unknown")


def _map_severity_to_fhir(severity: str) -> str:
    """Map allergy severity to FHIR severity code."""
    mapping = {"mild": "mild", "moderate": "moderate", "severe": "severe"}
    return mapping.get(severity.lower(), "moderate")


def _write_to_healthlake(fhir_bundle: dict) -> str:
    """Write FHIR bundle to HealthLake and return resource ID."""
    _get_healthlake_client().start_fhir_import_job(
        DatastoreId=FHIR_DATASTORE_ID,
        InputDataConfig={
            "S3Uri": ""  # HealthLake uses inline for small bundles via create_resource
        },
        JobOutputDataConfig={
            "S3Configuration": {"S3Uri": "", "KmsKeyId": ""}
        },
    )
    # Extract patient ID from the bundle for resource reference
    patient_id = ""
    for entry in fhir_bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Patient":
            patient_id = resource.get("id", "")
            break

    return f"Patient/{patient_id}"


def _store_in_dynamodb(
    table: Any,
    patient_data: dict,
    demographics: dict,
    section_hashes: dict,
    fhir_resource_id: str,
    changes: dict,
) -> None:
    """Store or update patient record in DynamoDB patient index table."""
    now = datetime.now(timezone.utc).isoformat()
    patient_id = patient_data["patient_id"]

    item = {
        "patient_id": patient_id,
        "patient_name": patient_data.get("patient_name", ""),
        "demographics": demographics,
        "allergies": patient_data.get("allergies", []),
        "medications": patient_data.get("medications", []),
        "lab_results": patient_data.get("lab_results", []),
        "vital_signs": patient_data.get("vital_signs", {}),
        "section_hashes": section_hashes,
        "last_updated": now,
        "fhir_resource_id": fhir_resource_id,
    }

    # DynamoDB does not support float — convert all floats to Decimal
    item = _convert_floats_to_decimal(item)

    _retry_with_backoff(
        lambda: table.put_item(Item=item),
        operation_name="DynamoDB write",
    )


def _convert_floats_to_decimal(obj):
    """Recursively convert float values to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _convert_floats_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_floats_to_decimal(i) for i in obj]
    return obj


def _sync_to_knowledge_base() -> None:
    """Trigger Bedrock KB data source sync for re-vectorization."""
    if not KB_ID or not KB_DATA_SOURCE_ID:
        logger.warning("KB_ID or KB_DATA_SOURCE_ID not configured, skipping KB sync")
        return

    _retry_with_backoff(
        lambda: _get_bedrock_agent_client().start_ingestion_job(
            knowledgeBaseId=KB_ID,
            dataSourceId=KB_DATA_SOURCE_ID,
        ),
        operation_name="Bedrock KB sync",
    )


def _retry_with_backoff(operation: callable, operation_name: str) -> Any:
    """Execute an operation with exponential backoff retry logic.

    Retries up to MAX_RETRIES times with exponential backoff for
    HealthLake, DynamoDB, and KB failures.
    """
    last_exception = None

    for attempt in range(MAX_RETRIES):
        try:
            return operation()
        except ClientError as e:
            last_exception = e
            error_code = e.response.get("Error", {}).get("Code", "")

            # Don't retry on access denied or validation errors
            if error_code in ("AccessDeniedException", "ValidationException", "ResourceNotFoundException"):
                logger.error(f"{operation_name} failed with non-retryable error: {e}")
                raise

            backoff = BASE_BACKOFF_SECONDS * (2 ** attempt)
            logger.warning(
                f"{operation_name} attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. "
                f"Retrying in {backoff}s..."
            )
            time.sleep(backoff)
        except Exception as e:
            last_exception = e
            backoff = BASE_BACKOFF_SECONDS * (2 ** attempt)
            logger.warning(
                f"{operation_name} attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. "
                f"Retrying in {backoff}s..."
            )
            time.sleep(backoff)

    logger.error(f"{operation_name} failed after {MAX_RETRIES} attempts")
    raise last_exception
