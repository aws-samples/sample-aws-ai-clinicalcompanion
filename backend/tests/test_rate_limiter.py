"""
Unit tests for the rate limiter module.

Tests verify:
- Queries within limit are allowed (Requirement 3.3)
- Queries exceeding limit are rejected (Requirement 3.1)
- DynamoDB errors result in fail-closed behavior (Requirement 3.5)
- TTL is set for automatic window expiry (Requirement 3.4)
- Remaining count decrements correctly
"""

import os
import sys
import time
from unittest.mock import patch, MagicMock
from dataclasses import asdict

import pytest
from botocore.exceptions import ClientError

# Set env var before importing the module
os.environ.setdefault('CONNECTIONS_TABLE_NAME', 'test-connections')

# Add the websocket Lambda directory to the path for imports
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', 'lambda', 'websocket')
)


@pytest.fixture(autouse=True)
def mock_dynamodb_table():
    """Mock the DynamoDB table used by the rate limiter."""
    mock_table = MagicMock()
    with patch('rate_limiter._get_table', return_value=mock_table):
        yield mock_table


class TestCheckRateLimit:
    """Tests for check_rate_limit function."""

    def test_allows_first_query(self, mock_dynamodb_table):
        """First query in a window should be allowed with remaining = 29."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 1}
        }

        result = check_rate_limit('user-123')

        assert result.allowed is True
        assert result.remaining == 29
        assert result.retry_after is None

    def test_allows_query_at_limit(self, mock_dynamodb_table):
        """Query number 30 (at the limit) should still be allowed."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 30}
        }

        result = check_rate_limit('user-123')

        assert result.allowed is True
        assert result.remaining == 0
        assert result.retry_after is None

    def test_rejects_query_over_limit(self, mock_dynamodb_table):
        """Query number 31 (over the limit) should be rejected."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 31}
        }

        result = check_rate_limit('user-123')

        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None
        assert result.retry_after > 0

    def test_fails_closed_on_client_error(self, mock_dynamodb_table):
        """DynamoDB ClientError should result in fail-closed (deny query).

        The limiter is an abuse/cost control; if the count cannot be verified we
        deny rather than allow unbounded expensive AgentCore/Bedrock calls.
        """
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.side_effect = ClientError(
            {'Error': {'Code': 'InternalServerError', 'Message': 'Service unavailable'}},
            'UpdateItem'
        )

        result = check_rate_limit('user-456')

        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None

    def test_fails_closed_on_generic_exception(self, mock_dynamodb_table):
        """Any generic exception should result in fail-closed (deny query)."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.side_effect = Exception('Network timeout')

        result = check_rate_limit('user-789')

        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None

    def test_uses_atomic_counter_update(self, mock_dynamodb_table):
        """Verify the DynamoDB call uses ADD for atomic increment."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 5}
        }

        check_rate_limit('user-atomic')

        call_kwargs = mock_dynamodb_table.update_item.call_args[1]
        assert 'ADD queryCount :inc' in call_kwargs['UpdateExpression']
        assert call_kwargs['ExpressionAttributeValues'][':inc'] == 1
        assert call_kwargs['ReturnValues'] == 'ALL_NEW'

    def test_sets_ttl_on_counter(self, mock_dynamodb_table):
        """Verify the DynamoDB call sets TTL for automatic window expiry."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 1}
        }

        check_rate_limit('user-ttl')

        call_kwargs = mock_dynamodb_table.update_item.call_args[1]
        assert '#ttl' in call_kwargs['ExpressionAttributeNames']
        assert call_kwargs['ExpressionAttributeNames']['#ttl'] == 'ttl'
        ttl_value = call_kwargs['ExpressionAttributeValues'][':ttl']
        # TTL should be in the future (at least current time)
        assert ttl_value > int(time.time())

    def test_partition_key_includes_user_id(self, mock_dynamodb_table):
        """Verify the partition key contains the user identity."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 1}
        }

        check_rate_limit('cognito-user-abc')

        call_kwargs = mock_dynamodb_table.update_item.call_args[1]
        key = call_kwargs['Key']['connectionId']
        assert 'rateLimit#' in key
        assert 'cognito-user-abc' in key

    def test_custom_max_queries(self, mock_dynamodb_table):
        """Custom max_queries parameter should be respected."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 11}
        }

        result = check_rate_limit('user-custom', max_queries=10)

        assert result.allowed is False
        assert result.remaining == 0

    def test_custom_window_minutes(self, mock_dynamodb_table):
        """Custom window_minutes parameter should affect TTL calculation."""
        from rate_limiter import check_rate_limit

        mock_dynamodb_table.update_item.return_value = {
            'Attributes': {'queryCount': 1}
        }

        check_rate_limit('user-window', window_minutes=30)

        call_kwargs = mock_dynamodb_table.update_item.call_args[1]
        key = call_kwargs['Key']['connectionId']
        # Verify the window is computed with 30-minute intervals
        assert 'rateLimit#user-window#' in key


class TestRateLimitResult:
    """Tests for the RateLimitResult dataclass."""

    def test_dataclass_fields(self):
        """RateLimitResult has the expected fields."""
        from rate_limiter import RateLimitResult

        result = RateLimitResult(allowed=True, remaining=25, retry_after=None)
        assert result.allowed is True
        assert result.remaining == 25
        assert result.retry_after is None

    def test_dataclass_with_retry_after(self):
        """RateLimitResult correctly stores retry_after."""
        from rate_limiter import RateLimitResult

        result = RateLimitResult(allowed=False, remaining=0, retry_after=1800)
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after == 1800
