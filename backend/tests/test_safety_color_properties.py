"""
Property-based tests for safety check color coding.

Verifies that the safety check color mapping is deterministic and correct:
  - "safe" status → "green" color
  - "caution" status → "yellow" color
  - "contraindicated" status → "red" color

The mapping function must be pure (same input always produces same output)
and must reject unknown statuses.

# Feature: clinical-enhancements, Property 19: Safety check color coding
"""

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from utils.safety_checks import (
    SAFETY_STATUS_COLOR_MAP,
    VALID_COLORS,
    VALID_STATUSES,
    build_safety_check,
    get_safety_color,
)


# ─── Strategies ──────────────────────────────────────────────────────────────

# Strategy for generating valid safety statuses
valid_status_st = st.sampled_from(sorted(VALID_STATUSES))

# Strategy for generating safety check descriptions
check_description_st = st.text(min_size=1, max_size=100).filter(lambda s: s.strip())

# Strategy for generating invalid statuses (strings not in the valid set)
invalid_status_st = st.text(min_size=1, max_size=50).filter(
    lambda s: s.strip() and s not in VALID_STATUSES
)

# Strategy for generating a complete safety alert with valid status
safety_alert_st = st.fixed_dictionaries({
    "check": check_description_st,
    "status": valid_status_st,
})

# Strategy for generating lists of safety alerts (simulating multiple alerts)
safety_alerts_list_st = st.lists(safety_alert_st, min_size=1, max_size=10)


# ─── Property 19: Safety check color coding ──────────────────────────────────
# **Validates: Requirements 8.6**


class TestSafetyCheckColorCoding:
    """Property 19: Safety check color coding.

    For any safety alert, the Patient Context Pane SHALL display a color-coded
    bullet where: green indicates "safe", yellow indicates "caution", and red
    indicates "contraindicated". The mapping SHALL be deterministic based on
    the alert severity/status.

    **Validates: Requirements 8.6**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(status=valid_status_st)
    def test_color_mapping_is_deterministic(self, status: str):
        """The same status always produces the same color (pure function).

        **Validates: Requirements 8.6**
        """
        color1 = get_safety_color(status)
        color2 = get_safety_color(status)
        assert color1 == color2, (
            f"Non-deterministic mapping: get_safety_color({status!r}) "
            f"returned {color1!r} then {color2!r}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(status=valid_status_st)
    def test_safe_maps_to_green_caution_to_yellow_contraindicated_to_red(
        self, status: str
    ):
        """Each status maps to its specified color per requirement 8.6.

        - "safe" → "green"
        - "caution" → "yellow"
        - "contraindicated" → "red"

        **Validates: Requirements 8.6**
        """
        color = get_safety_color(status)
        expected = SAFETY_STATUS_COLOR_MAP[status]
        assert color == expected, (
            f"Status {status!r} should map to {expected!r}, got {color!r}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(status=valid_status_st)
    def test_color_output_is_always_valid(self, status: str):
        """The output color is always one of the valid colors (green, yellow, red).

        **Validates: Requirements 8.6**
        """
        color = get_safety_color(status)
        assert color in VALID_COLORS, (
            f"get_safety_color({status!r}) returned {color!r}, "
            f"which is not in valid colors: {sorted(VALID_COLORS)}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(alert=safety_alert_st)
    def test_build_safety_check_produces_correct_structure(self, alert: dict):
        """build_safety_check produces a dict with check, status, and correct color.

        **Validates: Requirements 8.6**
        """
        result = build_safety_check(alert["check"], alert["status"])

        # Structure must have all three keys
        assert "check" in result, "Missing 'check' key in safety check dict"
        assert "status" in result, "Missing 'status' key in safety check dict"
        assert "color" in result, "Missing 'color' key in safety check dict"

        # Values must match input and mapping
        assert result["check"] == alert["check"]
        assert result["status"] == alert["status"]
        assert result["color"] == SAFETY_STATUS_COLOR_MAP[alert["status"]]

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(alerts=safety_alerts_list_st)
    def test_batch_color_mapping_is_consistent(self, alerts: list):
        """For any list of safety alerts, each alert's color is deterministic.

        **Validates: Requirements 8.6**
        """
        for alert in alerts:
            result = build_safety_check(alert["check"], alert["status"])
            expected_color = SAFETY_STATUS_COLOR_MAP[alert["status"]]
            assert result["color"] == expected_color, (
                f"Alert {alert['check']!r} with status {alert['status']!r} "
                f"got color {result['color']!r}, expected {expected_color!r}"
            )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(invalid_status=invalid_status_st)
    def test_invalid_status_raises_value_error(self, invalid_status: str):
        """Unknown statuses are rejected with ValueError.

        **Validates: Requirements 8.6**
        """
        with pytest.raises(ValueError):
            get_safety_color(invalid_status)

    def test_explicit_safe_maps_to_green(self):
        """Explicit check: 'safe' → 'green'.

        **Validates: Requirements 8.6**
        """
        assert get_safety_color("safe") == "green"

    def test_explicit_caution_maps_to_yellow(self):
        """Explicit check: 'caution' → 'yellow'.

        **Validates: Requirements 8.6**
        """
        assert get_safety_color("caution") == "yellow"

    def test_explicit_contraindicated_maps_to_red(self):
        """Explicit check: 'contraindicated' → 'red'.

        **Validates: Requirements 8.6**
        """
        assert get_safety_color("contraindicated") == "red"
