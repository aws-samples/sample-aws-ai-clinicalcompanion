"""
Property-based tests for guideline selection filtering and dynamic discovery.

Verifies that the guideline filtering logic correctly restricts agent
retrieval to only the selected guideline sources, and defaults to all
available sources when no selection is made.

Also verifies that any guideline registered in the guideline registry
(DynamoDB) automatically appears in the list returned by the guidelines
API endpoint — proving the system is data-driven and requires no frontend
code changes to discover new guideline sources.

# Feature: clinical-enhancements, Property 8: Guideline selection filtering
# Feature: clinical-enhancements, Property 9: Dynamic guideline discovery
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

from utils.guideline_filtering import (
    DEFAULT_GUIDELINE_SOURCES,
    filter_retrieval_results,
    resolve_selected_guidelines,
)

# Set required environment variables before importing the guidelines_api handler
os.environ.setdefault("GUIDELINE_REGISTRY_TABLE_NAME", "test-guideline-registry")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

# Load guidelines_api handler with importlib to avoid collision with other
# handler.py modules (ehr_ingestion, guideline_ingestion) in sys.modules.
import importlib.util

_guidelines_api_path = os.path.join(
    os.path.dirname(__file__), "..", "lambda", "guidelines_api", "handler.py"
)
_spec = importlib.util.spec_from_file_location("guidelines_api_handler", _guidelines_api_path)
guidelines_api_handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guidelines_api_handler)
# ─── Strategies ──────────────────────────────────────────────────────────────

# Strategy for generating guideline source names
guideline_source_st = st.sampled_from([
    "ACR",
    "ESR",
    "NICE",
    "HAS France",
    "WHO",
    "AHA",
    "Royal College of Radiologists",
    "Canadian Association of Radiologists",
    "Society of Nuclear Medicine",
    "European Association of Nuclear Medicine",
])

# Strategy for generating a set of available guideline sources (at least 2)
available_sources_st = st.frozensets(
    guideline_source_st, min_size=2, max_size=10
)

# Strategy for generating a non-empty subset of sources from available ones
@st.composite
def non_empty_subset_st(draw):
    """Generate a non-empty subset of available guideline sources."""
    available = draw(available_sources_st)
    available_list = sorted(available)
    # Draw a non-empty subset
    subset = draw(
        st.lists(
            st.sampled_from(available_list),
            min_size=1,
            max_size=len(available_list),
            unique=True,
        )
    )
    return {"available": available, "selected": subset}
# Strategy for generating retrieval results with source metadata
@st.composite
def retrieval_results_st(draw):
    """Generate a list of retrieval results from various guideline sources.

    Each result has metadata with a source_name field identifying which
    guideline source it came from.
    """
    available = draw(available_sources_st)
    available_list = sorted(available)
    num_results = draw(st.integers(min_value=1, max_value=20))

    results = []
    for _ in range(num_results):
        source = draw(st.sampled_from(available_list))
        result = {
            "text": draw(st.text(min_size=10, max_size=100)),
            "score": draw(st.floats(min_value=0.0, max_value=1.0)),
            "metadata": {
                "source_name": source,
                "section": draw(st.text(min_size=3, max_size=50)),
                "chunk_index": draw(st.integers(min_value=0, max_value=10)),
            },
        }
        results.append(result)

    return {"available": available, "results": results}
# Strategy for generating a selection + results scenario
@st.composite
def filtering_scenario_st(draw):
    """Generate a complete filtering scenario with available sources,
    selected subset, and retrieval results from all available sources."""
    available = draw(available_sources_st)
    available_list = sorted(available)

    # Generate a non-empty subset selection
    selected = draw(
        st.lists(
            st.sampled_from(available_list),
            min_size=1,
            max_size=len(available_list),
            unique=True,
        )
    )

    # Generate results from ALL available sources (simulating unfiltered retrieval)
    num_results = draw(st.integers(min_value=2, max_value=15))
    results = []
    for _ in range(num_results):
        source = draw(st.sampled_from(available_list))
        result = {
            "text": draw(st.text(min_size=5, max_size=80)),
            "score": draw(st.floats(min_value=0.0, max_value=1.0)),
            "metadata": {
                "source_name": source,
                "section": "test_section",
                "chunk_index": 0,
            },
        }
        results.append(result)

    return {
        "available": available,
        "selected": selected,
        "results": results,
    }
# ─── Property 8: Guideline selection filtering ───────────────────────────────
# Feature: clinical-enhancements, Property 8: Guideline selection filtering
# **Validates: Requirements 3.3, 3.4**
class TestGuidelineSelectionFiltering:
    """Property 8: Guideline selection filtering.

    For any non-empty subset of available guideline sources selected by the
    user, the agent retrieval SHALL query only the selected sources and SHALL
    NOT return results from unselected sources. When the selection is empty,
    all sources SHALL be queried.

    **Validates: Requirements 3.3, 3.4**
    """

    # ─── Empty selection defaults to all sources (Requirement 3.3) ────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(available=available_sources_st)
    def test_empty_selection_returns_all_available_sources(
        self, available: frozenset
    ):
        """When no guidelines are selected (None), all available sources are queried.

        **Validates: Requirements 3.3**
        """
        resolved = resolve_selected_guidelines(available, None)
        assert resolved == set(available), (
            f"Empty selection (None) should return all available sources "
            f"{set(available)}, got {resolved}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(available=available_sources_st)
    def test_empty_list_selection_returns_all_available_sources(
        self, available: frozenset
    ):
        """When selection is an empty list, all available sources are queried.

        **Validates: Requirements 3.3**
        """
        resolved = resolve_selected_guidelines(available, [])
        assert resolved == set(available), (
            f"Empty list selection should return all available sources "
            f"{set(available)}, got {resolved}"
        )

    # ─── Non-empty selection restricts to selected sources (Req 3.4) ──────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=non_empty_subset_st())
    def test_non_empty_selection_restricts_to_selected_sources(
        self, data: dict
    ):
        """When specific guidelines are selected, only those sources are queried.

        **Validates: Requirements 3.4**
        """
        available = data["available"]
        selected = data["selected"]

        resolved = resolve_selected_guidelines(available, selected)

        # Resolved set must be exactly the selected sources
        assert resolved == set(selected), (
            f"Non-empty selection {selected} should resolve to exactly "
            f"those sources, got {resolved}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=non_empty_subset_st())
    def test_non_empty_selection_excludes_unselected_sources(
        self, data: dict
    ):
        """When specific guidelines are selected, unselected sources are excluded.

        **Validates: Requirements 3.4**
        """
        available = data["available"]
        selected = data["selected"]

        resolved = resolve_selected_guidelines(available, selected)

        # No unselected source should be in the resolved set
        unselected = set(available) - set(selected)
        for source in unselected:
            assert source not in resolved, (
                f"Unselected source '{source}' should NOT be in resolved set "
                f"{resolved} when selection is {selected}"
            )

    # ─── Filtering retrieval results restricts to selected sources ────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(scenario=filtering_scenario_st())
    def test_filtered_results_contain_only_selected_sources(
        self, scenario: dict
    ):
        """Filtered retrieval results contain only results from selected sources.

        **Validates: Requirements 3.4**
        """
        available = scenario["available"]
        selected = scenario["selected"]
        results = scenario["results"]

        # Resolve which sources to query
        queried_sources = resolve_selected_guidelines(available, selected)

        # Filter results
        filtered = filter_retrieval_results(results, queried_sources)

        # Every filtered result must come from a selected source
        for result in filtered:
            source = result["metadata"]["source_name"]
            assert source in queried_sources, (
                f"Filtered result has source '{source}' which is not in "
                f"queried sources {queried_sources}"
            )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(scenario=filtering_scenario_st())
    def test_filtered_results_exclude_unselected_sources(
        self, scenario: dict
    ):
        """Filtered retrieval results do NOT contain results from unselected sources.

        **Validates: Requirements 3.4**
        """
        available = scenario["available"]
        selected = scenario["selected"]
        results = scenario["results"]

        # Resolve which sources to query
        queried_sources = resolve_selected_guidelines(available, selected)
        unselected_sources = set(available) - queried_sources

        # Filter results
        filtered = filter_retrieval_results(results, queried_sources)

        # No filtered result should come from an unselected source
        for result in filtered:
            source = result["metadata"]["source_name"]
            assert source not in unselected_sources, (
                f"Filtered result has source '{source}' which is in "
                f"unselected sources {unselected_sources}"
            )

    # ─── Empty selection means all results pass through ───────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=retrieval_results_st())
    def test_empty_selection_passes_all_results_through(self, data: dict):
        """When selection is empty, all retrieval results pass through the filter.

        **Validates: Requirements 3.3**
        """
        available = data["available"]
        results = data["results"]

        # Resolve with empty selection → all sources
        queried_sources = resolve_selected_guidelines(available, None)

        # Filter results
        filtered = filter_retrieval_results(results, queried_sources)

        # All results should pass through since all sources are queried
        assert len(filtered) == len(results), (
            f"With empty selection, all {len(results)} results should pass "
            f"through, but only {len(filtered)} did"
        )

    # ─── Selection of unavailable sources yields empty result ─────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(available=available_sources_st)
    def test_selection_of_unavailable_sources_yields_empty(
        self, available: frozenset
    ):
        """Selecting sources not in the available set yields an empty query set.

        **Validates: Requirements 3.4**
        """
        # Create a selection of sources that are NOT in available
        unavailable_sources = ["NonExistent Source A", "NonExistent Source B"]

        resolved = resolve_selected_guidelines(available, unavailable_sources)

        assert resolved == set(), (
            f"Selecting unavailable sources {unavailable_sources} should "
            f"resolve to empty set, got {resolved}"
        )

    # ─── Resolved set is always a subset of available sources ─────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        available=available_sources_st,
        selected=st.lists(guideline_source_st, min_size=0, max_size=5),
    )
    def test_resolved_sources_always_subset_of_available(
        self, available: frozenset, selected: list
    ):
        """The resolved source set is always a subset of available sources.

        **Validates: Requirements 3.3, 3.4**
        """
        resolved = resolve_selected_guidelines(available, selected if selected else None)

        assert resolved <= set(available), (
            f"Resolved sources {resolved} must be a subset of available "
            f"sources {set(available)}"
        )

    # ─── Idempotency: resolving twice gives same result ───────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=non_empty_subset_st())
    def test_resolve_is_idempotent(self, data: dict):
        """Resolving the same selection twice produces the same result.

        **Validates: Requirements 3.3, 3.4**
        """
        available = data["available"]
        selected = data["selected"]

        resolved1 = resolve_selected_guidelines(available, selected)
        resolved2 = resolve_selected_guidelines(available, selected)

        assert resolved1 == resolved2, (
            f"resolve_selected_guidelines is not idempotent: "
            f"first call returned {resolved1}, second returned {resolved2}"
        )
# ─── Property 9: Dynamic guideline discovery ─────────────────────────────────
# Feature: clinical-enhancements, Property 9: Dynamic guideline discovery
# **Validates: Requirements 3.6**


# Strategy for generating unique guideline source names (simulating new sources)
_guideline_name_alphabet = st.characters(
    whitelist_categories=("L", "N", "Z"),
    whitelist_characters="-_",
)

dynamic_source_name_st = st.text(
    alphabet=_guideline_name_alphabet,
    min_size=3,
    max_size=40,
).filter(lambda t: t.strip() and len(t.strip()) >= 3)

# Strategy for generating a display name
display_name_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P")),
    min_size=5,
    max_size=80,
).filter(lambda t: t.strip())


# Strategy for generating a guideline registry entry (DynamoDB item)
@st.composite
def registry_entry_st(draw):
    """Generate a single guideline registry entry as stored in DynamoDB."""
    source_name = draw(dynamic_source_name_st)
    display_name = draw(display_name_st)
    status = draw(st.sampled_from(["active", "inactive"]))
    return {
        "source_name": source_name,
        "display_name": display_name,
        "status": status,
    }


# Strategy for generating a set of registry entries (simulating the full registry)
@st.composite
def guideline_registry_st(draw):
    """Generate a list of unique guideline registry entries.

    Simulates the DynamoDB guideline registry table with 1-15 entries,
    each with a unique source_name (the partition key).
    """
    num_entries = draw(st.integers(min_value=1, max_value=15))
    entries = []
    seen_names = set()

    for _ in range(num_entries):
        entry = draw(registry_entry_st())
        # Ensure unique source_name (PK constraint)
        if entry["source_name"] in seen_names:
            continue
        seen_names.add(entry["source_name"])
        entries.append(entry)

    assume(len(entries) >= 1)
    return entries


class TestDynamicGuidelineDiscovery:
    """Property 9: Dynamic guideline discovery.

    For any guideline source registered in the guideline registry, the
    Guideline Selector SHALL include that source in its dropdown options
    without requiring frontend code changes.

    This property proves the system is data-driven: the GET /guidelines
    endpoint reads from DynamoDB, and the frontend fetches this list
    dynamically. Adding a new entry to the registry is sufficient to make
    it discoverable — no code changes needed.

    **Validates: Requirements 3.6**
    """

    # ─── All registered guidelines appear in API response ─────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(registry_entries=guideline_registry_st())
    def test_all_registered_guidelines_appear_in_api_response(
        self, registry_entries: list
    ):
        """Every guideline registered in DynamoDB appears in the GET /guidelines response.

        This proves that adding a new entry to the guideline registry table
        is sufficient for the Guideline Selector to discover it — no frontend
        code changes are required.

        **Validates: Requirements 3.6**
        """
        # Mock the DynamoDB table scan to return our generated registry entries
        mock_table = MagicMock()
        mock_table.scan.return_value = {
            "Items": registry_entries,
        }

        with patch.object(guidelines_api_handler, "table", mock_table):
            response = guidelines_api_handler.list_guidelines()

        # Parse the response body
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        guidelines = body["guidelines"]

        # Extract source_names from the API response
        returned_source_names = {g["source_name"] for g in guidelines}

        # Every registered guideline must appear in the response
        for entry in registry_entries:
            assert entry["source_name"] in returned_source_names, (
                f"Registered guideline '{entry['source_name']}' not found in "
                f"API response. Returned sources: {returned_source_names}"
            )

    # ─── No extra guidelines appear beyond what is registered ─────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(registry_entries=guideline_registry_st())
    def test_api_response_contains_only_registered_guidelines(
        self, registry_entries: list
    ):
        """The API response contains ONLY guidelines from the registry — no hardcoded extras.

        This proves the system is purely data-driven: the dropdown options
        come exclusively from the DynamoDB registry, not from hardcoded lists
        in the frontend or backend code.

        **Validates: Requirements 3.6**
        """
        mock_table = MagicMock()
        mock_table.scan.return_value = {
            "Items": registry_entries,
        }

        with patch.object(guidelines_api_handler, "table", mock_table):
            response = guidelines_api_handler.list_guidelines()

        body = json.loads(response["body"])
        guidelines = body["guidelines"]

        # The number of returned guidelines must match the registry
        registered_names = {e["source_name"] for e in registry_entries}
        returned_names = {g["source_name"] for g in guidelines}

        assert returned_names == registered_names, (
            f"API response sources {returned_names} do not match "
            f"registered sources {registered_names}. "
            f"Extra in response: {returned_names - registered_names}. "
            f"Missing from response: {registered_names - returned_names}."
        )

    # ─── Newly added guideline is immediately discoverable ────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        existing_entries=guideline_registry_st(),
        new_entry=registry_entry_st(),
    )
    def test_newly_registered_guideline_immediately_discoverable(
        self, existing_entries: list, new_entry: dict
    ):
        """A newly registered guideline appears in the next API call without code changes.

        Simulates adding a new guideline source to the registry and verifying
        it appears in the selector on the next fetch — proving no frontend
        deployment or code change is needed.

        **Validates: Requirements 3.6**
        """
        # Ensure the new entry has a unique source_name
        existing_names = {e["source_name"] for e in existing_entries}
        assume(new_entry["source_name"] not in existing_names)

        # Simulate the registry after adding the new entry
        updated_registry = existing_entries + [new_entry]

        mock_table = MagicMock()
        mock_table.scan.return_value = {
            "Items": updated_registry,
        }

        with patch.object(guidelines_api_handler, "table", mock_table):
            response = guidelines_api_handler.list_guidelines()

        body = json.loads(response["body"])
        guidelines = body["guidelines"]

        returned_source_names = {g["source_name"] for g in guidelines}

        # The new guideline must be discoverable
        assert new_entry["source_name"] in returned_source_names, (
            f"Newly registered guideline '{new_entry['source_name']}' not found "
            f"in API response after registration. This means the system requires "
            f"code changes to discover new sources — violating Requirement 3.6."
        )

    # ─── Display name and status are preserved in API response ────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(registry_entries=guideline_registry_st())
    def test_display_name_and_status_preserved_in_response(
        self, registry_entries: list
    ):
        """The API response preserves display_name and status from the registry.

        The frontend uses display_name for the dropdown label and status to
        determine visibility. Both must be faithfully returned from the registry.

        **Validates: Requirements 3.6**
        """
        mock_table = MagicMock()
        mock_table.scan.return_value = {
            "Items": registry_entries,
        }

        with patch.object(guidelines_api_handler, "table", mock_table):
            response = guidelines_api_handler.list_guidelines()

        body = json.loads(response["body"])
        guidelines = body["guidelines"]

        # Build a lookup by source_name for easy comparison
        response_by_name = {g["source_name"]: g for g in guidelines}

        for entry in registry_entries:
            name = entry["source_name"]
            assert name in response_by_name, (
                f"Registered guideline '{name}' missing from response"
            )
            returned = response_by_name[name]

            assert returned["display_name"] == entry["display_name"], (
                f"display_name mismatch for '{name}': "
                f"expected '{entry['display_name']}', got '{returned['display_name']}'"
            )
            assert returned["status"] == entry["status"], (
                f"status mismatch for '{name}': "
                f"expected '{entry['status']}', got '{returned['status']}'"
            )

    # ─── Paginated registry still returns all guidelines ──────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(registry_entries=guideline_registry_st())
    def test_paginated_registry_returns_all_guidelines(
        self, registry_entries: list
    ):
        """Even when DynamoDB paginates results, all registered guidelines are returned.

        The list_guidelines function handles pagination via LastEvaluatedKey.
        This test verifies that paginated results are fully assembled.

        **Validates: Requirements 3.6**
        """
        assume(len(registry_entries) >= 2)

        # Split entries into two pages to simulate DynamoDB pagination
        midpoint = len(registry_entries) // 2
        page1 = registry_entries[:midpoint]
        page2 = registry_entries[midpoint:]

        mock_table = MagicMock()
        # First scan returns page1 with a pagination token
        # Second scan returns page2 without a token (last page)
        mock_table.scan.side_effect = [
            {
                "Items": page1,
                "LastEvaluatedKey": {"source_name": page1[-1]["source_name"]},
            },
            {
                "Items": page2,
            },
        ]

        with patch.object(guidelines_api_handler, "table", mock_table):
            response = guidelines_api_handler.list_guidelines()

        body = json.loads(response["body"])
        guidelines = body["guidelines"]

        returned_source_names = {g["source_name"] for g in guidelines}
        expected_source_names = {e["source_name"] for e in registry_entries}

        assert returned_source_names == expected_source_names, (
            f"Paginated scan did not return all guidelines. "
            f"Missing: {expected_source_names - returned_source_names}"
        )

    # ─── resolve_selected_guidelines discovers dynamically registered sources ─

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(registry_entries=guideline_registry_st())
    def test_resolve_discovers_dynamically_registered_sources(
        self, registry_entries: list
    ):
        """resolve_selected_guidelines works with any set of dynamically registered sources.

        When the available_sources set is populated from the registry (not
        hardcoded), the resolve function correctly includes all registered
        sources when selection is empty — proving the filtering layer is
        also data-driven.

        **Validates: Requirements 3.6**
        """
        # Simulate available_sources being populated from the registry
        available_sources = {e["source_name"] for e in registry_entries}

        # With no selection, all dynamically registered sources are returned
        resolved = resolve_selected_guidelines(available_sources, None)

        assert resolved == available_sources, (
            f"With empty selection, resolve should return all dynamically "
            f"registered sources {available_sources}, got {resolved}"
        )
