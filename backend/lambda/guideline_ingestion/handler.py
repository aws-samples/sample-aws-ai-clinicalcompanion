"""Guideline Ingestion Lambda handler.

Triggered by S3 ObjectCreated events on the guidelines bucket.
Processes guideline documents (Markdown or JSON), extracts structured
recommendation data, chunks content for KB indexing, syncs to the
Bedrock Knowledge Base, and registers the source in the DynamoDB
guideline registry.

Supported formats:
- .md  → Markdown parsing with heading-based sectioning
- .json → Direct structured ingestion

Error handling:
- Unsupported formats are rejected with an error log
- Parse failures log detailed errors (file name + reason)
- Failed ingestion does NOT corrupt existing KB entries
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables set by CDK
GUIDELINE_REGISTRY_TABLE = os.environ.get(
    "GUIDELINE_REGISTRY_TABLE", "clinicalcompass-guideline-registry"
)
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
DATA_SOURCE_ID = os.environ.get("DATA_SOURCE_ID", "")

# Supported file extensions
SUPPORTED_EXTENSIONS = (".md", ".json")

# Chunking configuration
MAX_CHUNK_SIZE = 1000  # characters per chunk for KB indexing
CHUNK_OVERLAP = 100  # overlap between chunks for context continuity

# Retry configuration
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# AWS clients (lazily initialized for Lambda container reuse)
_s3_client = None
_dynamodb = None
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


def _get_bedrock_agent_client():
    global _bedrock_agent_client
    if _bedrock_agent_client is None:
        _bedrock_agent_client = boto3.client("bedrock-agent")
    return _bedrock_agent_client


class GuidelineParseError(Exception):
    """Raised when a guideline document cannot be parsed."""


def handler(event: dict, context: Any) -> dict:
    """S3-triggered Lambda for guideline document processing."""
    logger.info("Guideline Ingestion Lambda invoked")
    logger.info(json.dumps(event))

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        logger.info(f"Processing guideline: s3://{bucket}/{key}")

        try:
            _process_guideline_file(bucket, key)
        except UnsupportedFormatError as e:
            logger.error(f"Unsupported format for s3://{bucket}/{key}: {e}")
            continue
        except GuidelineParseError as e:
            logger.error(
                f"Failed to parse guideline document {key}: {e}"
            )
            continue
        except Exception as e:
            logger.error(f"Unexpected error processing s3://{bucket}/{key}: {e}")
            raise

    return {"statusCode": 200, "body": "Guideline ingestion complete"}


class UnsupportedFormatError(Exception):
    """Raised when a file has an unsupported format."""


def _process_guideline_file(bucket: str, key: str) -> None:
    """Download, parse, chunk, sync to KB, and register a guideline document.

    This function ensures that failed ingestion does NOT corrupt existing
    KB entries by only triggering KB sync after successful parsing and
    only registering in DynamoDB after successful KB sync.
    """
    # Step 1: Validate file extension
    ext = _get_file_extension(key)
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"File extension '{ext}' is not supported. "
            f"Supported formats: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    # Step 2: Download from S3
    content = _download_from_s3(bucket, key)

    # Step 3: Parse based on format
    if ext == ".md":
        guideline_data = parse_markdown_guideline(content, key)
    else:
        guideline_data = parse_json_guideline(content, key)

    # Step 4: Chunk content for KB indexing
    chunks = chunk_content(guideline_data)
    logger.info(f"Generated {len(chunks)} chunks for KB indexing from {key}")

    # Step 5: Sync to Knowledge Base (only after successful parse)
    _sync_to_knowledge_base()

    # Step 6: Register in guideline registry (only after successful KB sync)
    _register_guideline(guideline_data, key, len(chunks))

    logger.info(f"Successfully processed guideline: {key}")


def _get_file_extension(key: str) -> str:
    """Extract the file extension from an S3 key."""
    _, ext = os.path.splitext(key)
    return ext.lower()


def _download_from_s3(bucket: str, key: str) -> str:
    """Download file content from S3 and decode as UTF-8."""
    response = _get_s3_client().get_object(Bucket=bucket, Key=key)
    raw_bytes = response["Body"].read()
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise GuidelineParseError(f"File is not valid UTF-8 text: {e}")


def parse_markdown_guideline(content: str, key: str) -> dict:
    """Parse a Markdown guideline document with heading-based sectioning.

    Extracts:
    - title: from the first H1 heading
    - source_organization: from metadata or second heading
    - sections: list of heading-content pairs
    - recommendations: extracted recommendation blocks
    - ratings: appropriateness ratings found in the document

    Raises GuidelineParseError if the document cannot be parsed.
    """
    if not content or not content.strip():
        raise GuidelineParseError(f"Empty document: {key}")

    lines = content.split("\n")
    title = ""
    source_org = ""
    sections = []
    recommendations = []
    ratings = []

    current_heading = ""
    current_content_lines = []

    for line in lines:
        # Extract H1 title
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            current_heading = title
            continue

        # Extract H2 sections
        if line.startswith("## "):
            # Save previous section
            if current_heading and current_content_lines:
                section_text = "\n".join(current_content_lines).strip()
                sections.append({
                    "heading": current_heading,
                    "content": section_text,
                })
                # Check for recommendations in the section
                _extract_recommendations_from_section(
                    current_heading, section_text, recommendations, ratings
                )

            current_heading = line[3:].strip()
            current_content_lines = []

            # Try to extract source org from specific headings
            if not source_org and _is_org_heading(current_heading):
                source_org = current_heading
            continue

        # Extract H3 subsections (still part of current H2)
        if line.startswith("### "):
            # Subsections are included in the parent section content
            current_content_lines.append(line)
            continue

        current_content_lines.append(line)

    # Save the last section
    if current_heading and current_content_lines:
        section_text = "\n".join(current_content_lines).strip()
        sections.append({
            "heading": current_heading,
            "content": section_text,
        })
        _extract_recommendations_from_section(
            current_heading, section_text, recommendations, ratings
        )

    # Derive source_org from filename if not found in content
    if not source_org:
        source_org = _derive_source_name_from_key(key)

    if not title:
        title = source_org

    return {
        "source_name": source_org,
        "display_name": title,
        "format": "markdown",
        "sections": sections,
        "recommendations": recommendations,
        "ratings": ratings,
        "raw_content": content,
    }


def _is_org_heading(heading: str) -> bool:
    """Check if a heading looks like a source organization name."""
    org_keywords = [
        "source", "organization", "publisher", "author",
        "acr", "esr", "nice", "has", "who", "aha",
    ]
    heading_lower = heading.lower()
    return any(kw in heading_lower for kw in org_keywords)


def _extract_recommendations_from_section(
    heading: str, content: str, recommendations: list, ratings: list
) -> None:
    """Extract recommendations and ratings from a section's content."""
    heading_lower = heading.lower()

    # Look for recommendation patterns
    if any(kw in heading_lower for kw in ["recommendation", "guidance", "variant", "indication"]):
        recommendations.append({
            "heading": heading,
            "text": content[:500],  # First 500 chars as summary
        })

    # Extract rating patterns (e.g., "Usually Appropriate (7)", "Rating: 8")
    rating_patterns = [
        r"(?:usually|may be|rarely)\s+appropriate\s*\(?\d*\)?",
        r"rating[:\s]+(\d+)",
        r"appropriateness[:\s]+(\d+)",
        r"grade[:\s]+([A-D][+-]?)",
        r"strength[:\s]+(strong|moderate|weak|conditional)",
        r"level of evidence[:\s]+([IVX]+|[1-4][a-c]?)",
    ]

    for pattern in rating_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for match in matches:
            ratings.append({
                "section": heading,
                "rating": match if isinstance(match, str) else str(match),
            })


def parse_json_guideline(content: str, key: str) -> dict:
    """Parse a JSON guideline document via direct structured ingestion.

    Expects a JSON object with fields like:
    - source_organization
    - document_title
    - sections (list of {heading, content})
    - recommendations (list)
    - ratings (list)

    Raises GuidelineParseError if the JSON is invalid or missing required data.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise GuidelineParseError(f"Invalid JSON in {key}: {e}")

    if not isinstance(data, dict):
        raise GuidelineParseError(
            f"Expected JSON object in {key}, got {type(data).__name__}"
        )

    source_org = data.get("source_organization", "")
    title = data.get("document_title", "")

    if not source_org and not title:
        # Try to derive from filename
        source_org = _derive_source_name_from_key(key)
        title = source_org

    if not source_org:
        source_org = title
    if not title:
        title = source_org

    # Extract sections — support both flat and nested formats
    sections = data.get("sections", [])
    if not isinstance(sections, list):
        sections = []

    # Extract recommendations
    recommendations = data.get("recommendations", [])
    if not isinstance(recommendations, list):
        recommendations = []

    # Extract ratings
    ratings = data.get("ratings", [])
    if not isinstance(ratings, list):
        ratings = []

    return {
        "source_name": source_org,
        "display_name": title,
        "format": "json",
        "sections": sections,
        "recommendations": recommendations,
        "ratings": ratings,
        "raw_content": content,
    }


def _derive_source_name_from_key(key: str) -> str:
    """Derive a source name from the S3 key (filename without extension)."""
    basename = os.path.basename(key)
    name, _ = os.path.splitext(basename)
    # Convert underscores/hyphens to spaces and title-case
    return name.replace("_", " ").replace("-", " ").title()


def chunk_content(guideline_data: dict) -> list:
    """Chunk guideline content for Knowledge Base indexing.

    Uses heading-based sectioning first, then splits large sections
    into smaller chunks with overlap for context continuity.

    Returns a list of chunk dicts with metadata.
    """
    chunks = []
    source_name = guideline_data["source_name"]
    display_name = guideline_data["display_name"]

    sections = guideline_data.get("sections", [])

    if sections:
        # Chunk by section, splitting large sections
        for section in sections:
            heading = section.get("heading", "")
            content = section.get("content", "")

            if not content:
                continue

            section_chunks = _split_text_into_chunks(content)
            for i, chunk_text in enumerate(section_chunks):
                chunks.append({
                    "text": chunk_text,
                    "metadata": {
                        "source_name": source_name,
                        "display_name": display_name,
                        "section": heading,
                        "chunk_index": i,
                    },
                })
    else:
        # No sections — chunk the raw content directly
        raw_content = guideline_data.get("raw_content", "")
        if raw_content:
            text_chunks = _split_text_into_chunks(raw_content)
            for i, chunk_text in enumerate(text_chunks):
                chunks.append({
                    "text": chunk_text,
                    "metadata": {
                        "source_name": source_name,
                        "display_name": display_name,
                        "section": "full_document",
                        "chunk_index": i,
                    },
                })

    # Add recommendation chunks
    for rec in guideline_data.get("recommendations", []):
        rec_text = rec.get("text", "") if isinstance(rec, dict) else str(rec)
        if rec_text:
            chunks.append({
                "text": rec_text,
                "metadata": {
                    "source_name": source_name,
                    "display_name": display_name,
                    "section": "recommendation",
                    "chunk_index": 0,
                },
            })

    return chunks


def _split_text_into_chunks(text: str) -> list:
    """Split text into chunks of MAX_CHUNK_SIZE with CHUNK_OVERLAP overlap.

    Tries to split at paragraph boundaries first, then sentence boundaries,
    then falls back to character-level splitting.
    """
    if len(text) <= MAX_CHUNK_SIZE:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = start + MAX_CHUNK_SIZE

        if end >= len(text):
            chunks.append(text[start:])
            break

        # Try to find a paragraph break near the end
        paragraph_break = text.rfind("\n\n", start + MAX_CHUNK_SIZE // 2, end)
        if paragraph_break > start:
            end = paragraph_break
        else:
            # Try to find a sentence break
            sentence_break = text.rfind(". ", start + MAX_CHUNK_SIZE // 2, end)
            if sentence_break > start:
                end = sentence_break + 1  # Include the period

        chunks.append(text[start:end].strip())
        start = max(start + 1, end - CHUNK_OVERLAP)

    return [c for c in chunks if c]  # Filter empty chunks


def _sync_to_knowledge_base() -> None:
    """Trigger Bedrock KB data source sync for re-indexing.

    Retries up to MAX_RETRIES times with exponential backoff.
    """
    if not KNOWLEDGE_BASE_ID or not DATA_SOURCE_ID:
        logger.warning(
            "KB sync skipped — KNOWLEDGE_BASE_ID or DATA_SOURCE_ID not configured"
        )
        return

    _retry_with_backoff(
        lambda: _get_bedrock_agent_client().start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
        ),
        operation_name="Bedrock KB sync",
    )


def _register_guideline(guideline_data: dict, s3_key: str, document_count: int) -> None:
    """Register or update a guideline source in the DynamoDB registry.

    Retries up to MAX_RETRIES times with exponential backoff.
    """
    table = _get_dynamodb().Table(GUIDELINE_REGISTRY_TABLE)

    item = {
        "source_name": guideline_data["source_name"],
        "display_name": guideline_data["display_name"],
        "s3_key": s3_key,
        "ingestion_date": datetime.now(timezone.utc).isoformat(),
        "document_count": document_count,
        "status": "active",
    }

    _retry_with_backoff(
        lambda: table.put_item(Item=item),
        operation_name="DynamoDB registry write",
    )

    logger.info(f"Registered guideline source: {guideline_data['source_name']}")


def _retry_with_backoff(operation: callable, operation_name: str) -> Any:
    """Execute an operation with exponential backoff retry logic.

    Retries up to MAX_RETRIES times for transient failures.
    Does not retry on access denied or validation errors.
    """
    last_exception = None

    for attempt in range(MAX_RETRIES):
        try:
            return operation()
        except ClientError as e:
            last_exception = e
            error_code = e.response.get("Error", {}).get("Code", "")

            # Don't retry on non-retryable errors
            if error_code in (
                "AccessDeniedException",
                "ValidationException",
                "ResourceNotFoundException",
            ):
                logger.error(
                    f"{operation_name} failed with non-retryable error: {e}"
                )
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
