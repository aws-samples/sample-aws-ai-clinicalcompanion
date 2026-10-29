"""
Safety check color coding utility.

Provides a deterministic mapping from safety alert status to display color
for the Patient Context Pane. This mirrors the frontend rendering logic
in frontend/js/app.js and ensures consistent color coding across the stack.

Color mapping:
  - "safe" → "green"
  - "caution" → "yellow"
  - "contraindicated" → "red"
"""

# Canonical status-to-color mapping
SAFETY_STATUS_COLOR_MAP: dict[str, str] = {
    "safe": "green",
    "caution": "yellow",
    "contraindicated": "red",
}

# Valid statuses and colors (for validation)
VALID_STATUSES = frozenset(SAFETY_STATUS_COLOR_MAP.keys())
VALID_COLORS = frozenset(SAFETY_STATUS_COLOR_MAP.values())


def get_safety_color(status: str) -> str:
    """Return the display color for a given safety check status.

    This is a pure, deterministic function: the same status always produces
    the same color. Unknown statuses raise ValueError.

    Args:
        status: One of "safe", "caution", or "contraindicated".

    Returns:
        The corresponding color: "green", "yellow", or "red".

    Raises:
        ValueError: If the status is not a recognized safety status.
    """
    color = SAFETY_STATUS_COLOR_MAP.get(status)
    if color is None:
        raise ValueError(
            f"Unknown safety status: {status!r}. "
            f"Valid statuses are: {sorted(VALID_STATUSES)}"
        )
    return color


def build_safety_check(check: str, status: str) -> dict:
    """Build a safety check entry with deterministic color coding.

    Constructs the safety check dict as expected by the Patient Context Pane
    STATE_SNAPSHOT format.

    Args:
        check: Description of the safety check (e.g., "Contrast nephropathy risk").
        status: One of "safe", "caution", or "contraindicated".

    Returns:
        Dict with keys: check, status, color.

    Raises:
        ValueError: If the status is not a recognized safety status.
    """
    color = get_safety_color(status)
    return {
        "check": check,
        "status": status,
        "color": color,
    }
