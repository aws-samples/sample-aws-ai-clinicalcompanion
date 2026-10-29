"""
Property-based tests for citation completeness.

Verifies that all citations in a recommendation response include all required
fields: source_organization, document_title, publication_reference, variant,
rating, and quoted_text. Also verifies that citations with missing or empty
fields are correctly detected as invalid.

# Feature: clinical-enhancements, Property 10: Citation completeness
"""

from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

from utils.citation_validation import (
    REQUIRED_CITATION_FIELDS,
    CitationValidationError,
    group_citations_by_source,
    is_valid_citation,
    validate_citation,
    validate_citations_response,
)


# ─── Strategies ──────────────────────────────────────────────────────────────

# Strategy for generating non-empty strings (valid field values)
non_empty_string_st = st.text(min_size=1, max_size=200).filter(
    lambda s: s.strip()
)

# Strategy for generating source organization names
source_organization_st = st.sampled_from([
    "ACR",
    "ESR",
    "NICE",
    "HAS France",
    "WHO",
    "AHA",
    "Royal College of Radiologists",
    "Canadian Association of Radiologists",
])

# Strategy for generating document titles
document_title_st = st.from_regex(
    r"[A-Z][A-Za-z ]{5,50}: [A-Za-z ]{5,30}", fullmatch=True
)

# Strategy for generating variant/section descriptions
variant_st = st.from_regex(
    r"Variant [1-9]: [A-Za-z ,]{5,60}", fullmatch=True
)

# Strategy for generating appropriateness ratings
rating_st = st.sampled_from([
    "Usually Appropriate (7)",
    "Usually Appropriate (8)",
    "Usually Appropriate (9)",
    "May Be Appropriate (4)",
    "May Be Appropriate (5)",
    "May Be Appropriate (6)",
    "Usually Not Appropriate (1)",
    "Usually Not Appropriate (2)",
    "Usually Not Appropriate (3)",
    "Strong Recommendation",
    "Conditional Recommendation",
    "Grade A",
    "Grade B",
    "Grade C",
])

# Strategy for generating quoted text
quoted_text_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P")),
    min_size=10,
    max_size=300,
).filter(lambda s: s.strip())

# Strategy for generating publication references
publication_reference_st = st.from_regex(
    r"[A-Z][A-Za-z ]+ 20[12][0-9], (Revised|Updated|Original)", fullmatch=True
)


# Strategy for generating a complete valid citation
@st.composite
def valid_citation_st(draw):
    """Generate a complete, valid citation with all required fields."""
    return {
        "source_organization": draw(source_organization_st),
        "document_title": draw(document_title_st),
        "publication_reference": draw(publication_reference_st),
        "variant": draw(variant_st),
        "rating": draw(rating_st),
        "quoted_text": draw(quoted_text_st),
    }


# Strategy for generating a citation with one field missing
@st.composite
def citation_missing_one_field_st(draw):
    """Generate a citation with exactly one required field removed."""
    citation = draw(valid_citation_st())
    field_to_remove = draw(st.sampled_from(sorted(REQUIRED_CITATION_FIELDS)))
    del citation[field_to_remove]
    return {"citation": citation, "missing_field": field_to_remove}


# Strategy for generating a citation with one field set to empty string
@st.composite
def citation_empty_field_st(draw):
    """Generate a citation with exactly one required field set to empty/whitespace."""
    citation = draw(valid_citation_st())
    field_to_empty = draw(st.sampled_from(sorted(REQUIRED_CITATION_FIELDS)))
    empty_value = draw(st.sampled_from(["", " ", "  ", "\t", "\n", "   \n  "]))
    citation[field_to_empty] = empty_value
    return {"citation": citation, "empty_field": field_to_empty}


# Strategy for generating a list of valid citations
valid_citations_list_st = st.lists(valid_citation_st(), min_size=1, max_size=10)


# ─── Property 10: Citation completeness ──────────────────────────────────────
# Feature: clinical-enhancements, Property 10: Citation completeness
# **Validates: Requirements 4.1, 4.2, 4.3, 4.4**


class TestCitationCompleteness:
    """Property 10: Citation completeness.

    For any citation in a recommendation response, the citation SHALL include
    all required fields: source_organization, document_title,
    publication_reference, variant/section applied, appropriateness rating,
    and a direct quote of the recommendation text.

    **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
    """

    # ─── Valid citations pass validation ──────────────────────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_has_all_required_fields(self, citation: dict):
        """A complete citation with all required fields passes validation.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        errors = validate_citation(citation)
        assert errors == [], (
            f"Valid citation should have no errors, got: "
            f"{[(e.field, e.reason) for e in errors]}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_is_valid(self, citation: dict):
        """is_valid_citation returns True for a complete citation.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        assert is_valid_citation(citation) is True, (
            f"Complete citation should be valid: {citation}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_contains_source_organization(self, citation: dict):
        """A valid citation includes source_organization (Requirement 4.1).

        **Validates: Requirements 4.1**
        """
        assert "source_organization" in citation
        assert isinstance(citation["source_organization"], str)
        assert citation["source_organization"].strip() != ""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_contains_document_title(self, citation: dict):
        """A valid citation includes document_title (Requirement 4.1).

        **Validates: Requirements 4.1**
        """
        assert "document_title" in citation
        assert isinstance(citation["document_title"], str)
        assert citation["document_title"].strip() != ""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_contains_publication_reference(self, citation: dict):
        """A valid citation includes publication_reference (Requirement 4.1).

        **Validates: Requirements 4.1**
        """
        assert "publication_reference" in citation
        assert isinstance(citation["publication_reference"], str)
        assert citation["publication_reference"].strip() != ""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_contains_variant(self, citation: dict):
        """A valid citation includes variant/section applied (Requirement 4.2).

        **Validates: Requirements 4.2**
        """
        assert "variant" in citation
        assert isinstance(citation["variant"], str)
        assert citation["variant"].strip() != ""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_contains_rating(self, citation: dict):
        """A valid citation includes appropriateness rating (Requirement 4.3).

        **Validates: Requirements 4.3**
        """
        assert "rating" in citation
        assert isinstance(citation["rating"], str)
        assert citation["rating"].strip() != ""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_valid_citation_contains_quoted_text(self, citation: dict):
        """A valid citation includes a direct quote of recommendation text (Requirement 4.4).

        **Validates: Requirements 4.4**
        """
        assert "quoted_text" in citation
        assert isinstance(citation["quoted_text"], str)
        assert citation["quoted_text"].strip() != ""

    # ─── Missing fields are detected as invalid ───────────────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=citation_missing_one_field_st())
    def test_citation_with_missing_field_is_invalid(self, data: dict):
        """A citation with any required field missing is detected as invalid.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        citation = data["citation"]
        missing_field = data["missing_field"]

        assert is_valid_citation(citation) is False, (
            f"Citation missing '{missing_field}' should be invalid"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=citation_missing_one_field_st())
    def test_missing_field_produces_specific_error(self, data: dict):
        """Validation identifies the specific missing field.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        citation = data["citation"]
        missing_field = data["missing_field"]

        errors = validate_citation(citation)
        error_fields = {e.field for e in errors}

        assert missing_field in error_fields, (
            f"Expected error for missing field '{missing_field}', "
            f"but got errors for: {error_fields}"
        )

    # ─── Empty fields are detected as invalid ─────────────────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=citation_empty_field_st())
    def test_citation_with_empty_field_is_invalid(self, data: dict):
        """A citation with any required field set to empty/whitespace is invalid.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        citation = data["citation"]
        empty_field = data["empty_field"]

        assert is_valid_citation(citation) is False, (
            f"Citation with empty '{empty_field}' should be invalid"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(data=citation_empty_field_st())
    def test_empty_field_produces_specific_error(self, data: dict):
        """Validation identifies the specific empty field.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        citation = data["citation"]
        empty_field = data["empty_field"]

        errors = validate_citation(citation)
        error_fields = {e.field for e in errors}

        assert empty_field in error_fields, (
            f"Expected error for empty field '{empty_field}', "
            f"but got errors for: {error_fields}"
        )

    # ─── Batch validation of citation lists ───────────────────────────────

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citations=valid_citations_list_st)
    def test_list_of_valid_citations_passes_batch_validation(
        self, citations: list
    ):
        """A list of valid citations passes batch validation with no errors.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        result = validate_citations_response(citations)

        assert result["valid"] is True, (
            f"All valid citations should pass batch validation, "
            f"but got {result['invalid_count']} invalid"
        )
        assert result["total"] == len(citations)
        assert result["invalid_count"] == 0
        assert result["errors"] == []

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        valid_citations=valid_citations_list_st,
        bad_data=citation_missing_one_field_st(),
    )
    def test_batch_validation_detects_invalid_citation_in_list(
        self, valid_citations: list, bad_data: dict
    ):
        """Batch validation detects an invalid citation mixed with valid ones.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        # Insert the invalid citation at a random position
        bad_citation = bad_data["citation"]
        mixed = valid_citations + [bad_citation]

        result = validate_citations_response(mixed)

        assert result["valid"] is False, (
            f"Batch with one invalid citation should fail validation"
        )
        assert result["invalid_count"] >= 1

    # ─── All required fields are exactly the expected set ─────────────────

    def test_required_fields_match_design_spec(self):
        """The set of required fields matches the design document specification.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        expected = {
            "source_organization",
            "document_title",
            "publication_reference",
            "variant",
            "rating",
            "quoted_text",
        }
        assert REQUIRED_CITATION_FIELDS == expected, (
            f"Required fields mismatch. Expected {expected}, "
            f"got {REQUIRED_CITATION_FIELDS}"
        )



# ─── Property 11: Citation grouping by source ─────────────────────────────────
# Feature: clinical-enhancements, Property 11: Citation grouping by source
# **Validates: Requirements 4.6**


# Strategy for generating citations from multiple sources
@st.composite
def multi_source_citations_st(draw):
    """Generate a list of citations from at least 2 different sources.

    Citations are generated in an interleaved order (e.g., ACR, ESR, ACR, ESR)
    to test that grouping correctly de-interleaves them.
    """
    # Pick 2-5 distinct sources
    num_sources = draw(st.integers(min_value=2, max_value=5))
    sources = draw(
        st.lists(
            source_organization_st,
            min_size=num_sources,
            max_size=num_sources,
            unique=True,
        )
    )

    # Generate 1-4 citations per source
    citations = []
    for source in sources:
        count = draw(st.integers(min_value=1, max_value=4))
        for _ in range(count):
            citation = draw(valid_citation_st())
            citation["source_organization"] = source
            citations.append(citation)

    # Shuffle to create interleaving
    shuffled = draw(st.permutations(citations))
    return list(shuffled)


class TestCitationGroupingBySource:
    """Property 11: Citation grouping by source.

    For any set of citations from multiple guideline sources, the citations
    SHALL be grouped by source_organization with no interleaving of sources.

    **Validates: Requirements 4.6**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citations=multi_source_citations_st())
    def test_grouped_citations_have_no_interleaving(self, citations: list):
        """After grouping, citations from the same source appear consecutively
        with no interleaving from other sources.

        **Validates: Requirements 4.6**
        """
        grouped = group_citations_by_source(citations)

        # Verify no interleaving: once we leave a source, we never return to it
        seen_sources: set[str] = set()
        current_source = None

        for citation in grouped:
            source = citation["source_organization"]
            if source != current_source:
                assert source not in seen_sources, (
                    f"Source '{source}' appears again after being left. "
                    f"Interleaving detected in grouped citations. "
                    f"Order of sources seen: {[c['source_organization'] for c in grouped]}"
                )
                seen_sources.add(source)
                current_source = source

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citations=multi_source_citations_st())
    def test_grouping_preserves_all_citations(self, citations: list):
        """Grouping does not add or remove any citations — only reorders them.

        **Validates: Requirements 4.6**
        """
        grouped = group_citations_by_source(citations)

        assert len(grouped) == len(citations), (
            f"Grouping changed citation count: "
            f"input={len(citations)}, output={len(grouped)}"
        )

        # Every citation in the input must appear in the output
        for citation in citations:
            assert citation in grouped, (
                f"Citation lost during grouping: {citation['source_organization']} - "
                f"{citation['document_title']}"
            )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citations=multi_source_citations_st())
    def test_grouping_preserves_per_source_order(self, citations: list):
        """Within each source group, the relative order of citations is preserved.

        **Validates: Requirements 4.6**
        """
        grouped = group_citations_by_source(citations)

        # Extract citations per source from original (in order)
        original_by_source: dict[str, list] = {}
        for c in citations:
            src = c["source_organization"]
            original_by_source.setdefault(src, []).append(c)

        # Extract citations per source from grouped (in order)
        grouped_by_source: dict[str, list] = {}
        for c in grouped:
            src = c["source_organization"]
            grouped_by_source.setdefault(src, []).append(c)

        # Per-source order must match
        for source in original_by_source:
            assert original_by_source[source] == grouped_by_source[source], (
                f"Order within source '{source}' was not preserved after grouping"
            )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citations=multi_source_citations_st())
    def test_all_sources_represented_in_grouped_output(self, citations: list):
        """All source organizations from the input appear in the grouped output.

        **Validates: Requirements 4.6**
        """
        grouped = group_citations_by_source(citations)

        input_sources = {c["source_organization"] for c in citations}
        output_sources = {c["source_organization"] for c in grouped}

        assert input_sources == output_sources, (
            f"Source mismatch: input has {input_sources}, "
            f"output has {output_sources}"
        )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(citation=valid_citation_st())
    def test_single_citation_grouping_is_identity(self, citation: dict):
        """Grouping a single citation returns it unchanged.

        **Validates: Requirements 4.6**
        """
        grouped = group_citations_by_source([citation])
        assert grouped == [citation]

    def test_empty_list_grouping_returns_empty(self):
        """Grouping an empty list returns an empty list.

        **Validates: Requirements 4.6**
        """
        assert group_citations_by_source([]) == []
