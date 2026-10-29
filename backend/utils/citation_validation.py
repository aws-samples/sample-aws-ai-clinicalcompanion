"""
Citation validation utilities for ClinicalCompass.

Provides validation logic for citation structures returned by the Supervisor
Agent. Citations must include all required fields as specified in the design
document and requirements 4.1-4.4.

Required citation fields:
  - source_organization: The guideline source (e.g., "ACR")
  - document_title: Full title of the guideline document
  - publication_reference: Publication reference string
  - variant: The specific variant or section applied
  - rating: Appropriateness rating or recommendation strength
  - quoted_text: Direct quote of the recommendation text
"""

from typing import Any

# Required fields for a valid citation per design document
REQUIRED_CITATION_FIELDS = frozenset([
    "source_organization",
    "document_title",
    "publication_reference",
    "variant",
    "rating",
    "quoted_text",
])


class CitationValidationError(Exception):
    """Raised when a citation fails validation."""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"Citation validation failed for '{field}': {reason}")


def validate_citation(citation: dict[str, Any]) -> list[CitationValidationError]:
    """Validate a citation dictionary for completeness.

    Checks that all required fields are present and contain non-empty strings.

    Args:
        citation: A dictionary representing a single citation.

    Returns:
        A list of CitationValidationError instances. Empty list means valid.
    """
    errors: list[CitationValidationError] = []

    if not isinstance(citation, dict):
        errors.append(
            CitationValidationError("_root", "Citation must be a dictionary")
        )
        return errors

    for field in REQUIRED_CITATION_FIELDS:
        if field not in citation:
            errors.append(
                CitationValidationError(field, "Missing required field")
            )
        elif not isinstance(citation[field], str):
            errors.append(
                CitationValidationError(
                    field, f"Must be a string, got {type(citation[field]).__name__}"
                )
            )
        elif not citation[field].strip():
            errors.append(
                CitationValidationError(field, "Must be a non-empty string")
            )

    return errors


def is_valid_citation(citation: dict[str, Any]) -> bool:
    """Check if a citation is valid (all required fields present and non-empty).

    Args:
        citation: A dictionary representing a single citation.

    Returns:
        True if the citation passes all validation checks, False otherwise.
    """
    return len(validate_citation(citation)) == 0


def group_citations_by_source(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group citations by source_organization with no interleaving.

    Citations from the same source_organization are placed consecutively.
    The order of first appearance of each source is preserved.

    Args:
        citations: List of citation dictionaries.

    Returns:
        A new list of citations grouped by source_organization.
    """
    if not citations:
        return []

    # Preserve order of first appearance of each source
    seen_sources: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}

    for citation in citations:
        source = citation.get("source_organization", "")
        if source not in groups:
            seen_sources.append(source)
            groups[source] = []
        groups[source].append(citation)

    # Flatten groups in order of first appearance
    result: list[dict[str, Any]] = []
    for source in seen_sources:
        result.extend(groups[source])

    return result


def validate_citations_response(citations: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a list of citations from an agent response.

    Args:
        citations: List of citation dictionaries.

    Returns:
        A dict with:
          - "valid": bool indicating if all citations are valid
          - "total": total number of citations
          - "invalid_count": number of invalid citations
          - "errors": list of (index, errors) tuples for invalid citations
    """
    all_errors = []

    for idx, citation in enumerate(citations):
        errors = validate_citation(citation)
        if errors:
            all_errors.append((idx, errors))

    return {
        "valid": len(all_errors) == 0,
        "total": len(citations),
        "invalid_count": len(all_errors),
        "errors": all_errors,
    }
