"""
Guideline filtering utilities.

Implements the logic for restricting agent retrieval to only the
guideline sources selected by the clinician. When no guidelines are
explicitly selected, all available sources are queried.

Requirements:
- 3.3: When no guidelines are explicitly selected, default to all available sources
- 3.4: When specific guidelines are selected, restrict retrieval to only those sources
"""

from typing import List, Optional, Set


# Default guideline sources available in the system
DEFAULT_GUIDELINE_SOURCES = frozenset([
    "ACR",
    "ESR",
    "NICE",
    "HAS France",
])


def resolve_selected_guidelines(
    available_sources: Set[str],
    selected_sources: Optional[List[str]],
) -> Set[str]:
    """Resolve which guideline sources to query based on user selection.

    When selected_sources is None or empty, returns all available sources
    (Requirement 3.3). When a non-empty selection is provided, returns
    only the intersection of selected and available sources (Requirement 3.4).

    Args:
        available_sources: Set of all guideline sources currently registered
            in the system (from the guideline registry).
        selected_sources: List of source names selected by the clinician,
            or None/empty list for "all sources".

    Returns:
        Set of guideline source names to query.
    """
    if not selected_sources:
        # Requirement 3.3: default to all available sources
        return set(available_sources)

    # Requirement 3.4: restrict to only selected sources
    # Only include sources that are actually available in the system
    return set(available_sources) & set(selected_sources)


def filter_retrieval_results(
    results: List[dict],
    queried_sources: Set[str],
) -> List[dict]:
    """Filter retrieval results to only include results from queried sources.

    Each result is expected to have a 'metadata' dict containing a
    'source_name' field identifying which guideline source it came from.

    Args:
        results: List of retrieval result dicts, each with a 'metadata'
            dict containing 'source_name'.
        queried_sources: Set of source names that were queried.

    Returns:
        Filtered list containing only results whose source_name is in
        the queried_sources set.
    """
    filtered = []
    for result in results:
        metadata = result.get("metadata", {})
        source_name = metadata.get("source_name", "")
        if source_name in queried_sources:
            filtered.append(result)
    return filtered
