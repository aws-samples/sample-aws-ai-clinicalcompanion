"""Input Validator Module - validates and sanitizes user queries.

Implements prompt injection protection and input length validation for
clinical queries before they are forwarded to the AgentCore invocation.

This module provides:
- Query length validation (max 2000 characters)
- Prompt injection pattern detection
- Query sanitization for safe forwarding
- Logging of all blocked queries with user identity and reason
"""
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Known prompt injection patterns — case-insensitive matching
DEFAULT_INJECTION_PATTERNS: list[str] = [
    r"ignore\s+(all\s+)?(previous\s+)?instructions",
    r"ignore\s+your\s+instructions",
    r"disregard\s+(all\s+)?(previous\s+)?instructions",
    r"forget\s+(all\s+)?(previous\s+)?instructions",
    r"list\s+all\s+patients",
    r"show\s+(all\s+)?other\s+patient",
    r"dump\s+database",
    r"show\s+me\s+the\s+database",
    r"export\s+all\s+(patient\s+)?data",
    r"reveal\s+(your\s+)?(system\s+)?prompt",
    r"show\s+(your\s+)?(system\s+)?prompt",
    r"print\s+(your\s+)?(system\s+)?prompt",
    r"what\s+are\s+your\s+instructions",
    r"act\s+as\s+(a\s+)?different",
    r"you\s+are\s+now\s+(a\s+)?",
    r"pretend\s+you\s+are",
    r"bypass\s+(all\s+)?restrictions",
    r"override\s+(all\s+)?safety",
    r"disable\s+(all\s+)?filters",
    r"jailbreak",
]


@dataclass
class ValidationResult:
    """Result of a query validation check.

    Attributes:
        is_valid: Whether the query passed validation.
        error_message: User-facing error message when validation fails.
        blocked_reason: Internal reason code for blocking (e.g., "length_exceeded", "injection_pattern").
        blocked_pattern: The specific pattern that matched, if applicable.
    """

    is_valid: bool
    error_message: str | None = None
    blocked_reason: str | None = None
    blocked_pattern: str | None = None


def validate_query_length(query: str, max_length: int = 2000) -> ValidationResult:
    """Validate that a query does not exceed the maximum allowed length.

    Args:
        query: The user query string to validate.
        max_length: Maximum allowed character count (default: 2000).

    Returns:
        ValidationResult indicating whether the query length is acceptable.
    """
    if len(query) > max_length:
        return ValidationResult(
            is_valid=False,
            error_message=f"Query exceeds maximum length of {max_length} characters.",
            blocked_reason="length_exceeded",
            blocked_pattern=None,
        )

    return ValidationResult(is_valid=True)


def validate_injection_patterns(
    query: str, patterns: list[str] | None = None
) -> ValidationResult:
    """Check if a query matches any known prompt injection patterns.

    Performs case-insensitive regex matching against a list of known
    injection patterns. The patterns are checked at any position within
    the query string.

    Args:
        query: The user query string to check.
        patterns: List of regex patterns to match against.
                  Defaults to DEFAULT_INJECTION_PATTERNS.

    Returns:
        ValidationResult indicating whether the query is safe.
    """
    if patterns is None:
        patterns = DEFAULT_INJECTION_PATTERNS

    for pattern in patterns:
        if re.search(pattern, query, re.IGNORECASE):
            return ValidationResult(
                is_valid=False,
                error_message="Query blocked for security reasons.",
                blocked_reason="injection_pattern",
                blocked_pattern=pattern,
            )

    return ValidationResult(is_valid=True)


def sanitize_query(query: str) -> str:
    """Sanitize a query for safe forwarding to AgentCore.

    Performs basic sanitization:
    - Strips leading/trailing whitespace
    - Removes null bytes
    - Normalizes excessive whitespace

    Args:
        query: The validated query string to sanitize.

    Returns:
        The sanitized query string ready for forwarding.
    """
    # Remove null bytes
    sanitized = query.replace("\x00", "")

    # Strip leading/trailing whitespace
    sanitized = sanitized.strip()

    # Normalize excessive internal whitespace (collapse multiple spaces/newlines)
    sanitized = re.sub(r"[ \t]+", " ", sanitized)

    return sanitized


def log_blocked_query(
    query: str, result: ValidationResult, user_identity: str | None = None
) -> None:
    """Log a blocked query with user identity and reason.

    Produces a structured JSON log entry for security monitoring.

    Args:
        query: The original query that was blocked.
        result: The ValidationResult explaining why it was blocked.
        user_identity: The Cognito user identity or connection ID.
    """
    log_entry = {
        "event_type": "query_blocked",
        "user_identity": user_identity or "unknown",
        "reason": result.blocked_reason,
        "error_message": result.error_message,
        "blocked_pattern": result.blocked_pattern,
        "query_length": len(query),
        "query_preview": query[:100] if len(query) > 100 else query,
    }
    logger.warning(json.dumps(log_entry))


def validate_query(
    query: str,
    user_identity: str | None = None,
    max_length: int = 2000,
    patterns: list[str] | None = None,
) -> ValidationResult:
    """Run all validation checks on a query and log blocked queries.

    This is the main entry point that orchestrates length validation,
    injection pattern detection, and logging of blocked queries.

    Args:
        query: The user query string to validate.
        user_identity: The Cognito user identity for logging.
        max_length: Maximum allowed character count (default: 2000).
        patterns: Optional list of injection patterns to check.

    Returns:
        ValidationResult from the first failing check, or a passing result.
    """
    # Check length first
    length_result = validate_query_length(query, max_length)
    if not length_result.is_valid:
        log_blocked_query(query, length_result, user_identity)
        return length_result

    # Check injection patterns
    injection_result = validate_injection_patterns(query, patterns)
    if not injection_result.is_valid:
        log_blocked_query(query, injection_result, user_identity)
        return injection_result

    return ValidationResult(is_valid=True)
