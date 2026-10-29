"""Unit tests for Guideline Ingestion Lambda handler.

Tests cover:
- Markdown parsing with heading-based sectioning
- JSON structured ingestion
- Unsupported format rejection
- Parse failure error handling
- Content chunking for KB indexing
- Retry logic
- Registry registration
- KB corruption prevention on failure
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch
import importlib.util

import pytest

# Load guideline_ingestion handler with importlib under a unique module name
# to avoid collision with ehr_ingestion/handler.py.
_handler_path = os.path.join(
    os.path.dirname(__file__), "..", "lambda", "guideline_ingestion", "handler.py"
)
_spec = importlib.util.spec_from_file_location("gl_handler", _handler_path)
_gl_module = importlib.util.module_from_spec(_spec)
sys.modules["gl_handler"] = _gl_module
_spec.loader.exec_module(_gl_module)

GuidelineParseError = _gl_module.GuidelineParseError
UnsupportedFormatError = _gl_module.UnsupportedFormatError
_derive_source_name_from_key = _gl_module._derive_source_name_from_key
_get_file_extension = _gl_module._get_file_extension
_split_text_into_chunks = _gl_module._split_text_into_chunks
chunk_content = _gl_module.chunk_content
handler = _gl_module.handler
parse_json_guideline = _gl_module.parse_json_guideline
parse_markdown_guideline = _gl_module.parse_markdown_guideline


# --- Fixtures ---


@pytest.fixture
def sample_markdown_guideline():
    """Sample Markdown guideline document."""
    return """# ACR Appropriateness Criteria: Right Lower Quadrant Pain

## Source Organization

American College of Radiology (ACR)

## Variant 1: Right lower quadrant pain, fever, leukocytosis

### Recommendations

CT abdomen and pelvis with IV contrast is usually appropriate for evaluation
of acute right lower quadrant pain with fever and leukocytosis.

Rating: 9 (Usually Appropriate)

### Alternative Procedures

- Ultrasound: May be appropriate (Rating: 6)
- MRI without IV contrast: May be appropriate (Rating: 5)

## Variant 2: Right lower quadrant pain, no fever

### Recommendations

Ultrasound is usually appropriate as the initial imaging study for right
lower quadrant pain without systemic signs.

Rating: 8 (Usually Appropriate)

## References

1. Smith et al. "Imaging of Acute Abdominal Pain." Radiology 2023.
2. ACR Appropriateness Criteria Committee. Revised 2024.
"""


@pytest.fixture
def sample_json_guideline():
    """Sample JSON guideline document."""
    return json.dumps({
        "source_organization": "NICE",
        "document_title": "NICE CG169: Acute Kidney Injury",
        "sections": [
            {
                "heading": "Assessment",
                "content": "Assess patients for AKI risk factors including CKD, diabetes, and nephrotoxic medications.",
            },
            {
                "heading": "Imaging Recommendations",
                "content": "Consider non-contrast CT for patients with suspected obstruction. Avoid iodinated contrast in patients with eGFR < 30.",
            },
        ],
        "recommendations": [
            {
                "heading": "Contrast Use",
                "text": "Do not use iodinated contrast in patients with eGFR < 30 mL/min/1.73m² unless the benefits clearly outweigh the risks.",
            }
        ],
        "ratings": [
            {"section": "Contrast Use", "rating": "Strong recommendation"},
        ],
    })


@pytest.fixture
def minimal_json_guideline():
    """Minimal valid JSON guideline."""
    return json.dumps({
        "source_organization": "ESR",
        "document_title": "ESR Imaging Guidelines",
    })


# --- File Extension Detection ---


class TestGetFileExtension:
    """Tests for _get_file_extension."""

    def test_markdown_extension(self):
        assert _get_file_extension("guidelines/acr_criteria.md") == ".md"

    def test_json_extension(self):
        assert _get_file_extension("guidelines/nice_cg169.json") == ".json"

    def test_pdf_extension(self):
        assert _get_file_extension("guidelines/document.pdf") == ".pdf"

    def test_uppercase_extension(self):
        assert _get_file_extension("guidelines/DOC.MD") == ".md"

    def test_no_extension(self):
        assert _get_file_extension("guidelines/readme") == ""

    def test_nested_path(self):
        assert _get_file_extension("a/b/c/file.json") == ".json"


# --- Markdown Parsing Tests ---


class TestParseMarkdownGuideline:
    """Tests for parse_markdown_guideline."""

    def test_extracts_title(self, sample_markdown_guideline):
        result = parse_markdown_guideline(sample_markdown_guideline, "acr.md")
        assert result["display_name"] == "ACR Appropriateness Criteria: Right Lower Quadrant Pain"

    def test_extracts_source_org(self, sample_markdown_guideline):
        result = parse_markdown_guideline(sample_markdown_guideline, "acr.md")
        assert result["source_name"] == "Source Organization"

    def test_extracts_sections(self, sample_markdown_guideline):
        result = parse_markdown_guideline(sample_markdown_guideline, "acr.md")
        assert len(result["sections"]) > 0
        headings = [s["heading"] for s in result["sections"]]
        assert "Variant 1: Right lower quadrant pain, fever, leukocytosis" in headings

    def test_extracts_recommendations(self, sample_markdown_guideline):
        result = parse_markdown_guideline(sample_markdown_guideline, "acr.md")
        assert len(result["recommendations"]) > 0

    def test_extracts_ratings(self, sample_markdown_guideline):
        result = parse_markdown_guideline(sample_markdown_guideline, "acr.md")
        assert len(result["ratings"]) > 0

    def test_includes_raw_content(self, sample_markdown_guideline):
        result = parse_markdown_guideline(sample_markdown_guideline, "acr.md")
        assert result["raw_content"] == sample_markdown_guideline

    def test_format_is_markdown(self, sample_markdown_guideline):
        result = parse_markdown_guideline(sample_markdown_guideline, "acr.md")
        assert result["format"] == "markdown"

    def test_empty_content_raises(self):
        with pytest.raises(GuidelineParseError, match="Empty document"):
            parse_markdown_guideline("", "empty.md")

    def test_whitespace_only_raises(self):
        with pytest.raises(GuidelineParseError, match="Empty document"):
            parse_markdown_guideline("   \n\n  ", "blank.md")

    def test_no_title_uses_filename(self):
        content = "Some content without headings\nMore text here."
        result = parse_markdown_guideline(content, "guidelines/my_guideline.md")
        assert result["source_name"] == "My Guideline"
        assert result["display_name"] == "My Guideline"

    def test_derives_source_from_filename_when_no_org(self):
        content = "# Simple Title\n\nSome content without org heading."
        result = parse_markdown_guideline(content, "guidelines/acr_criteria.md")
        assert result["source_name"] == "Acr Criteria"


# --- JSON Parsing Tests ---


class TestParseJsonGuideline:
    """Tests for parse_json_guideline."""

    def test_extracts_source_org(self, sample_json_guideline):
        result = parse_json_guideline(sample_json_guideline, "nice.json")
        assert result["source_name"] == "NICE"

    def test_extracts_title(self, sample_json_guideline):
        result = parse_json_guideline(sample_json_guideline, "nice.json")
        assert result["display_name"] == "NICE CG169: Acute Kidney Injury"

    def test_extracts_sections(self, sample_json_guideline):
        result = parse_json_guideline(sample_json_guideline, "nice.json")
        assert len(result["sections"]) == 2
        assert result["sections"][0]["heading"] == "Assessment"

    def test_extracts_recommendations(self, sample_json_guideline):
        result = parse_json_guideline(sample_json_guideline, "nice.json")
        assert len(result["recommendations"]) == 1
        assert "iodinated contrast" in result["recommendations"][0]["text"]

    def test_extracts_ratings(self, sample_json_guideline):
        result = parse_json_guideline(sample_json_guideline, "nice.json")
        assert len(result["ratings"]) == 1
        assert result["ratings"][0]["rating"] == "Strong recommendation"

    def test_format_is_json(self, sample_json_guideline):
        result = parse_json_guideline(sample_json_guideline, "nice.json")
        assert result["format"] == "json"

    def test_minimal_json_works(self, minimal_json_guideline):
        result = parse_json_guideline(minimal_json_guideline, "esr.json")
        assert result["source_name"] == "ESR"
        assert result["display_name"] == "ESR Imaging Guidelines"
        assert result["sections"] == []
        assert result["recommendations"] == []

    def test_invalid_json_raises(self):
        with pytest.raises(GuidelineParseError, match="Invalid JSON"):
            parse_json_guideline("{not valid json", "bad.json")

    def test_non_dict_json_raises(self):
        with pytest.raises(GuidelineParseError, match="Expected JSON object"):
            parse_json_guideline("[1, 2, 3]", "array.json")

    def test_empty_json_derives_from_filename(self):
        result = parse_json_guideline("{}", "guidelines/has_france.json")
        assert result["source_name"] == "Has France"

    def test_includes_raw_content(self, sample_json_guideline):
        result = parse_json_guideline(sample_json_guideline, "nice.json")
        assert result["raw_content"] == sample_json_guideline


# --- Content Chunking Tests ---


class TestChunkContent:
    """Tests for chunk_content."""

    def test_small_sections_produce_one_chunk_each(self):
        data = {
            "source_name": "ACR",
            "display_name": "ACR Guidelines",
            "sections": [
                {"heading": "Section 1", "content": "Short content."},
                {"heading": "Section 2", "content": "Another short section."},
            ],
            "recommendations": [],
            "ratings": [],
            "raw_content": "",
        }
        chunks = chunk_content(data)
        assert len(chunks) == 2
        assert chunks[0]["metadata"]["source_name"] == "ACR"
        assert chunks[0]["metadata"]["section"] == "Section 1"

    def test_large_section_is_split(self):
        long_content = "A" * 2500  # Exceeds MAX_CHUNK_SIZE
        data = {
            "source_name": "NICE",
            "display_name": "NICE Guidelines",
            "sections": [{"heading": "Big Section", "content": long_content}],
            "recommendations": [],
            "ratings": [],
            "raw_content": "",
        }
        chunks = chunk_content(data)
        assert len(chunks) > 1

    def test_recommendations_added_as_chunks(self):
        data = {
            "source_name": "ESR",
            "display_name": "ESR Guidelines",
            "sections": [],
            "recommendations": [
                {"heading": "Rec 1", "text": "Use MRI for soft tissue."}
            ],
            "ratings": [],
            "raw_content": "",
        }
        chunks = chunk_content(data)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["section"] == "recommendation"

    def test_no_sections_uses_raw_content(self):
        data = {
            "source_name": "WHO",
            "display_name": "WHO Guidelines",
            "sections": [],
            "recommendations": [],
            "ratings": [],
            "raw_content": "Full document text here.",
        }
        chunks = chunk_content(data)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["section"] == "full_document"

    def test_empty_sections_skipped(self):
        data = {
            "source_name": "ACR",
            "display_name": "ACR Guidelines",
            "sections": [
                {"heading": "Empty", "content": ""},
                {"heading": "Has Content", "content": "Real content here."},
            ],
            "recommendations": [],
            "ratings": [],
            "raw_content": "",
        }
        chunks = chunk_content(data)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["section"] == "Has Content"


class TestSplitTextIntoChunks:
    """Tests for _split_text_into_chunks."""

    def test_short_text_returns_single_chunk(self):
        result = _split_text_into_chunks("Short text.")
        assert result == ["Short text."]

    def test_long_text_splits_into_multiple(self):
        text = "Word " * 300  # ~1500 chars
        result = _split_text_into_chunks(text)
        assert len(result) > 1

    def test_all_chunks_within_size_limit(self):
        text = "Sentence. " * 200
        result = _split_text_into_chunks(text)
        for chunk in result:
            # Allow some tolerance for boundary splitting
            assert len(chunk) <= 1100  # MAX_CHUNK_SIZE + small tolerance

    def test_no_empty_chunks(self):
        text = "A" * 2500
        result = _split_text_into_chunks(text)
        assert all(len(c) > 0 for c in result)


# --- Source Name Derivation ---


class TestDeriveSourceNameFromKey:
    """Tests for _derive_source_name_from_key."""

    def test_simple_filename(self):
        assert _derive_source_name_from_key("acr_criteria.md") == "Acr Criteria"

    def test_nested_path(self):
        assert _derive_source_name_from_key("guidelines/nice_cg169.json") == "Nice Cg169"

    def test_hyphens_converted(self):
        assert _derive_source_name_from_key("has-france.md") == "Has France"


# --- Handler Integration Tests ---


class TestHandler:
    """Integration tests for the Lambda handler."""

    @patch("gl_handler._get_bedrock_agent_client")
    @patch("gl_handler._get_dynamodb")
    @patch("gl_handler._get_s3_client")
    def test_processes_markdown_guideline(
        self, mock_s3_fn, mock_dynamo_fn, mock_bedrock_fn, sample_markdown_guideline
    ):
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: sample_markdown_guideline.encode())
        }
        mock_s3_fn.return_value = mock_s3

        mock_table = MagicMock()
        mock_dynamo = MagicMock()
        mock_dynamo.Table.return_value = mock_table
        mock_dynamo_fn.return_value = mock_dynamo

        mock_bedrock = MagicMock()
        mock_bedrock.start_ingestion_job.return_value = {
            "ingestionJob": {"ingestionJobId": "job-123"}
        }
        mock_bedrock_fn.return_value = mock_bedrock

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "guidelines-bucket"},
                        "object": {"key": "guidelines/acr_criteria.md"},
                    }
                }
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_s3.get_object.assert_called_once()
        mock_table.put_item.assert_called_once()

    @patch("gl_handler._get_bedrock_agent_client")
    @patch("gl_handler._get_dynamodb")
    @patch("gl_handler._get_s3_client")
    def test_processes_json_guideline(
        self, mock_s3_fn, mock_dynamo_fn, mock_bedrock_fn, sample_json_guideline
    ):
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: sample_json_guideline.encode())
        }
        mock_s3_fn.return_value = mock_s3

        mock_table = MagicMock()
        mock_dynamo = MagicMock()
        mock_dynamo.Table.return_value = mock_table
        mock_dynamo_fn.return_value = mock_dynamo

        mock_bedrock = MagicMock()
        mock_bedrock.start_ingestion_job.return_value = {
            "ingestionJob": {"ingestionJobId": "job-456"}
        }
        mock_bedrock_fn.return_value = mock_bedrock

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "guidelines-bucket"},
                        "object": {"key": "guidelines/nice_cg169.json"},
                    }
                }
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_table.put_item.assert_called_once()

    @patch("gl_handler._get_s3_client")
    def test_rejects_unsupported_format(self, mock_s3_fn):
        """Unsupported format is logged and skipped without raising."""
        mock_s3 = MagicMock()
        mock_s3_fn.return_value = mock_s3

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "guidelines-bucket"},
                        "object": {"key": "guidelines/document.pdf"},
                    }
                }
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        # S3 should NOT be called for unsupported formats
        mock_s3.get_object.assert_not_called()

    @patch("gl_handler._get_s3_client")
    def test_handles_parse_failure_gracefully(self, mock_s3_fn):
        """Parse failure is logged and skipped without corrupting KB."""
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"{invalid json content")
        }
        mock_s3_fn.return_value = mock_s3

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "guidelines-bucket"},
                        "object": {"key": "guidelines/broken.json"},
                    }
                }
            ]
        }

        # Should not raise — parse failure is logged and skipped
        result = handler(event, None)
        assert result["statusCode"] == 200

    @patch("gl_handler.KNOWLEDGE_BASE_ID", "kb-123")
    @patch("gl_handler.DATA_SOURCE_ID", "ds-456")
    @patch("gl_handler._get_bedrock_agent_client")
    @patch("gl_handler._get_dynamodb")
    @patch("gl_handler._get_s3_client")
    @patch("gl_handler.time.sleep")
    def test_kb_sync_failure_does_not_register(
        self, mock_sleep, mock_s3_fn, mock_dynamo_fn, mock_bedrock_fn, sample_json_guideline
    ):
        """If KB sync fails, guideline is NOT registered in DynamoDB."""
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: sample_json_guideline.encode())
        }
        mock_s3_fn.return_value = mock_s3

        mock_table = MagicMock()
        mock_dynamo = MagicMock()
        mock_dynamo.Table.return_value = mock_table
        mock_dynamo_fn.return_value = mock_dynamo

        # KB sync always fails
        from botocore.exceptions import ClientError

        mock_bedrock = MagicMock()
        mock_bedrock.start_ingestion_job.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "KB unavailable"}},
            "StartIngestionJob",
        )
        mock_bedrock_fn.return_value = mock_bedrock

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "guidelines-bucket"},
                        "object": {"key": "guidelines/nice.json"},
                    }
                }
            ]
        }

        # Should raise after retries exhausted
        with pytest.raises(ClientError):
            handler(event, None)

        # DynamoDB should NOT have been written to
        mock_table.put_item.assert_not_called()

    @patch("gl_handler._get_s3_client")
    def test_multiple_records_processed(self, mock_s3_fn):
        """Handler processes multiple records, skipping unsupported ones."""
        mock_s3 = MagicMock()
        mock_s3_fn.return_value = mock_s3

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "bucket"},
                        "object": {"key": "doc.pdf"},  # Unsupported
                    }
                },
                {
                    "s3": {
                        "bucket": {"name": "bucket"},
                        "object": {"key": "doc.txt"},  # Unsupported
                    }
                },
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        # Neither should trigger S3 download
        mock_s3.get_object.assert_not_called()


# --- Retry Logic Tests ---


class TestRetryWithBackoff:
    """Tests for _retry_with_backoff."""

    @patch("gl_handler.time.sleep")
    def test_succeeds_on_first_try(self, mock_sleep):
        _retry_with_backoff = _gl_module._retry_with_backoff

        result = _retry_with_backoff(lambda: "success", "test op")
        assert result == "success"
        mock_sleep.assert_not_called()

    @patch("gl_handler.time.sleep")
    def test_retries_on_transient_failure(self, mock_sleep):
        _retry_with_backoff = _gl_module._retry_with_backoff
        from botocore.exceptions import ClientError

        call_count = {"n": 0}

        def flaky_op():
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                    "StartIngestionJob",
                )
            return "success"

        result = _retry_with_backoff(flaky_op, "test op")
        assert result == "success"
        assert call_count["n"] == 3

    @patch("gl_handler.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        _retry_with_backoff = _gl_module._retry_with_backoff
        from botocore.exceptions import ClientError

        def always_fails():
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "Server error"}},
                "StartIngestionJob",
            )

        with pytest.raises(ClientError):
            _retry_with_backoff(always_fails, "test op")

    @patch("gl_handler.time.sleep")
    def test_no_retry_on_access_denied(self, mock_sleep):
        _retry_with_backoff = _gl_module._retry_with_backoff
        from botocore.exceptions import ClientError

        def access_denied():
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
                "StartIngestionJob",
            )

        with pytest.raises(ClientError):
            _retry_with_backoff(access_denied, "test op")
        mock_sleep.assert_not_called()


# --- Error Handling Tests (Task 5.2) ---


class TestErrorHandling:
    """Tests for error handling — unsupported formats and parse failures."""

    def test_unsupported_pdf_format(self):
        """PDF files are rejected."""
        _process_guideline_file = _gl_module._process_guideline_file

        with pytest.raises(UnsupportedFormatError, match="not supported"):
            _process_guideline_file("bucket", "guidelines/doc.pdf")

    def test_unsupported_txt_format(self):
        """TXT files are rejected."""
        _process_guideline_file = _gl_module._process_guideline_file

        with pytest.raises(UnsupportedFormatError, match="not supported"):
            _process_guideline_file("bucket", "guidelines/doc.txt")

    def test_unsupported_docx_format(self):
        """DOCX files are rejected."""
        _process_guideline_file = _gl_module._process_guideline_file

        with pytest.raises(UnsupportedFormatError, match="not supported"):
            _process_guideline_file("bucket", "guidelines/doc.docx")

    @patch("gl_handler._get_s3_client")
    def test_malformed_json_logs_error_with_filename(self, mock_s3_fn):
        """Malformed JSON logs error including the file name."""
        _process_guideline_file = _gl_module._process_guideline_file

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"not valid json {{{")
        }
        mock_s3_fn.return_value = mock_s3

        with pytest.raises(GuidelineParseError, match="Invalid JSON.*broken.json"):
            _process_guideline_file("bucket", "guidelines/broken.json")

    @patch("gl_handler._get_s3_client")
    def test_empty_markdown_logs_error(self, mock_s3_fn):
        """Empty Markdown document raises parse error."""
        _process_guideline_file = _gl_module._process_guideline_file

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"")
        }
        mock_s3_fn.return_value = mock_s3

        with pytest.raises(GuidelineParseError, match="Empty document"):
            _process_guideline_file("bucket", "guidelines/empty.md")

    @patch("gl_handler._get_s3_client")
    def test_binary_file_raises_parse_error(self, mock_s3_fn):
        """Binary content that can't be decoded raises parse error."""
        _process_guideline_file = _gl_module._process_guideline_file

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"\xff\xfe\x00\x01\x02")
        }
        mock_s3_fn.return_value = mock_s3

        with pytest.raises(GuidelineParseError, match="not valid UTF-8"):
            _process_guideline_file("bucket", "guidelines/binary.md")
