"""
Rate Limiter Module
Enforces per-user query rate limits using DynamoDB atomic counters with TTL.

Implements a sliding-window rate limiter that:
- Tracks query counts per Cognito user identity
- Uses DynamoDB atomic counters for thread-safe increments
- Leverages TTL for automatic window expiry
- Fails closed on DynamoDB errors (abuse/cost control > availability)

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5
"""

import boto3
import os
import time
import logging
from dataclasses import dataclass
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# DynamoDB table reference — lazy-initialized on first use for Lambda container reuse.
# This avoids boto3 calls at import time, making the module easier to test.
_table = None


def _get_table():
    """Return the DynamoDB table resource, initializing on first call."""
    global _table
    if _table is None:
        dynamodb = boto3.resource('dynamodb')
        _table = dynamodb.Table(os.environ.get('CONNECTIONS_TABLE_NAME', 'connections'))
    return _table


@dataclass
class RateLimitResult:
    """Result of a rate limit check.

    Attributes:
        allowed: Whether the query is allowed to proceed.
        remaining: Number of queries remaining in the current window.
        retry_after: Seconds until the rate limit window resets (None if allowed).
    """
    allowed: bool
    remaining: int
    retry_after: int | None = None


def _get_window_key(user_id: str, window_minutes: int) -> tuple[str, int]:
    """Compute the rate limit partition key and window expiry timestamp.

    The window is aligned to fixed intervals (e.g., every 60 minutes from epoch)
    so that all queries within the same window share the same counter.

    Args:
        user_id: The Cognito user identity.
        window_minutes: Duration of the rate limit window in minutes.

    Returns:
        Tuple of (partition_key, ttl_timestamp).
    """
    now = int(time.time())
    window_seconds = window_minutes * 60
    window_start = (now // window_seconds) * window_seconds
    window_end = window_start + window_seconds

    partition_key = f"rateLimit#{user_id}#{window_start}"
    # TTL set to window end + 60s buffer for DynamoDB eventual cleanup
    ttl_timestamp = window_end + 60

    return partition_key, ttl_timestamp


def _get_retry_after(window_minutes: int) -> int:
    """Calculate seconds until the current rate limit window expires.

    Args:
        window_minutes: Duration of the rate limit window in minutes.

    Returns:
        Number of seconds until the window resets.
    """
    now = int(time.time())
    window_seconds = window_minutes * 60
    window_start = (now // window_seconds) * window_seconds
    window_end = window_start + window_seconds
    return max(0, window_end - now)


def check_rate_limit(
    user_id: str,
    max_queries: int = 30,
    window_minutes: int = 60,
) -> RateLimitResult:
    """Check if a user is within their rate limit and atomically increment the counter.

    Uses DynamoDB atomic counters (UpdateItem with ADD) to safely track query counts
    across concurrent Lambda invocations. The counter is stored with a TTL attribute
    so DynamoDB automatically cleans up expired windows (Requirement 3.4).

    On DynamoDB errors, fails closed: logs a warning and denies the query
    (Requirement 3.5) so that an attacker cannot bypass the limit by forcing
    errors. Abuse/cost control takes priority over availability here.

    Args:
        user_id: The Cognito user identity to check.
        max_queries: Maximum number of queries allowed per window (default: 30).
        window_minutes: Duration of the rate limit window in minutes (default: 60).

    Returns:
        RateLimitResult with allowed status, remaining queries, and retry_after if blocked.
    """
    partition_key, ttl_timestamp = _get_window_key(user_id, window_minutes)

    try:
        # Atomic increment — creates item if it doesn't exist
        response = _get_table().update_item(
            Key={'connectionId': partition_key},
            UpdateExpression='ADD queryCount :inc SET #ttl = :ttl',
            ExpressionAttributeNames={'#ttl': 'ttl'},
            ExpressionAttributeValues={
                ':inc': 1,
                ':ttl': ttl_timestamp,
            },
            ReturnValues='ALL_NEW',
        )

        current_count = int(response['Attributes']['queryCount'])

        if current_count > max_queries:
            retry_after = _get_retry_after(window_minutes)
            logger.info(
                f"Rate limit exceeded for user {user_id}: "
                f"{current_count}/{max_queries} queries in window"
            )
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after=retry_after,
            )

        remaining = max(0, max_queries - current_count)
        return RateLimitResult(
            allowed=True,
            remaining=remaining,
            retry_after=None,
        )

    except (ClientError, Exception) as e:
        # Fail-closed: deny the query on any DynamoDB error. The rate limiter is
        # an abuse/cost control (denial-of-wallet), so if we cannot verify the
        # count we must not allow unbounded expensive AgentCore/Bedrock calls.
        # A short retry_after lets legitimate clients recover from transient
        # errors without long lockouts.
        logger.warning(
            f"Rate limit check failed for user {user_id}, failing closed: {e}"
        )
        return RateLimitResult(
            allowed=False,
            remaining=0,
            retry_after=_get_retry_after(window_minutes),
        )
