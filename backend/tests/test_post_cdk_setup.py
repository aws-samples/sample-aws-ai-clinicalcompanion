"""Unit tests for post_cdk_setup.py — initial data upload and KB sync."""
import sys
import os
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Add deployment scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'deployment' / 'scripts'))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'deployment'))


class TestGetSsmParameter:
    """Tests for get_ssm_parameter helper."""

    def test_returns_value_when_parameter_exists(self):
        from post_cdk_setup import get_ssm_parameter

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            'Parameter': {'Value': 'clinicalcompass-ehr-data-123456'}
        }

        result = get_ssm_parameter(mock_ssm, '/clinicalcompass/ehr-data-bucket')
        assert result == 'clinicalcompass-ehr-data-123456'
        mock_ssm.get_parameter.assert_called_once_with(Name='/clinicalcompass/ehr-data-bucket')

    def test_returns_empty_string_for_placeholder(self):
        from post_cdk_setup import get_ssm_parameter

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            'Parameter': {'Value': 'PLACEHOLDER'}
        }

        result = get_ssm_parameter(mock_ssm, '/clinicalcompass/agent-runtime-arn')
        assert result == ''

    def test_returns_empty_string_when_not_found(self):
        from post_cdk_setup import get_ssm_parameter
        from botocore.exceptions import ClientError

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = ClientError(
            {'Error': {'Code': 'ParameterNotFound', 'Message': 'not found'}},
            'GetParameter'
        )

        result = get_ssm_parameter(mock_ssm, '/clinicalcompass/nonexistent')
        assert result == ''


class TestUploadInitialData:
    """Tests for upload_initial_data — initial patient/guideline upload + KB sync."""

    def test_uploads_ehr_files_to_s3(self, tmp_path):
        from post_cdk_setup import upload_initial_data

        # Set up temp data directory
        ehr_dir = tmp_path / 'knowledge-bases' / 'data' / 'ehr'
        ehr_dir.mkdir(parents=True)
        (ehr_dir / 'patient_001_test.json').write_text('{"patient_id": "001"}')
        (ehr_dir / 'patient_002_test.json').write_text('{"patient_id": "002"}')

        guidelines_dir = tmp_path / 'knowledge-bases' / 'data' / 'guidelines'
        guidelines_dir.mkdir(parents=True)

        # Mock session
        mock_session = MagicMock()
        mock_ssm = MagicMock()
        mock_s3 = MagicMock()
        mock_bedrock = MagicMock()

        mock_session.client.side_effect = lambda svc, **kwargs: {
            'ssm': mock_ssm,
            's3': mock_s3,
            'bedrock-agent': mock_bedrock,
        }[svc]

        # SSM returns bucket names but no KB IDs
        def ssm_get(Name):
            params = {
                '/clinicalcompass/ehr-data-bucket': 'my-ehr-bucket',
                '/clinicalcompass/guidelines-bucket': 'my-guidelines-bucket',
                '/clinicalcompass/kb/cc-ehr-mock/id': 'PLACEHOLDER',
                '/clinicalcompass/kb/cc-global-guidelines/id': 'PLACEHOLDER',
            }
            if Name in params:
                return {'Parameter': {'Value': params[Name]}}
            from botocore.exceptions import ClientError
            raise ClientError(
                {'Error': {'Code': 'ParameterNotFound', 'Message': ''}},
                'GetParameter'
            )

        mock_ssm.get_parameter.side_effect = ssm_get

        with patch('post_cdk_setup.PROJECT_ROOT', tmp_path):
            upload_initial_data(mock_session)

        # Verify S3 uploads for EHR files
        upload_calls = mock_s3.upload_file.call_args_list
        assert len(upload_calls) == 2
        # Files should be uploaded to the EHR bucket
        for c in upload_calls:
            assert c[0][1] == 'my-ehr-bucket'

    def test_uploads_guideline_files_to_s3(self, tmp_path):
        from post_cdk_setup import upload_initial_data

        # Set up temp data directory
        ehr_dir = tmp_path / 'knowledge-bases' / 'data' / 'ehr'
        ehr_dir.mkdir(parents=True)

        guidelines_dir = tmp_path / 'knowledge-bases' / 'data' / 'guidelines'
        guidelines_dir.mkdir(parents=True)
        (guidelines_dir / 'acr_guidelines.md').write_text('# ACR Guidelines')
        (guidelines_dir / 'nice_guidelines.json').write_text('{"source": "NICE"}')

        mock_session = MagicMock()
        mock_ssm = MagicMock()
        mock_s3 = MagicMock()
        mock_bedrock = MagicMock()

        mock_session.client.side_effect = lambda svc, **kwargs: {
            'ssm': mock_ssm,
            's3': mock_s3,
            'bedrock-agent': mock_bedrock,
        }[svc]

        def ssm_get(Name):
            params = {
                '/clinicalcompass/ehr-data-bucket': 'my-ehr-bucket',
                '/clinicalcompass/guidelines-bucket': 'my-guidelines-bucket',
                '/clinicalcompass/kb/cc-ehr-mock/id': 'PLACEHOLDER',
                '/clinicalcompass/kb/cc-global-guidelines/id': 'PLACEHOLDER',
            }
            if Name in params:
                return {'Parameter': {'Value': params[Name]}}
            from botocore.exceptions import ClientError
            raise ClientError(
                {'Error': {'Code': 'ParameterNotFound', 'Message': ''}},
                'GetParameter'
            )

        mock_ssm.get_parameter.side_effect = ssm_get

        with patch('post_cdk_setup.PROJECT_ROOT', tmp_path):
            upload_initial_data(mock_session)

        # Verify S3 uploads for guideline files
        upload_calls = mock_s3.upload_file.call_args_list
        assert len(upload_calls) == 2
        for c in upload_calls:
            assert c[0][1] == 'my-guidelines-bucket'

    def test_triggers_kb_sync_after_upload(self, tmp_path):
        from post_cdk_setup import upload_initial_data

        # Set up temp data directories (empty — focus on KB sync)
        ehr_dir = tmp_path / 'knowledge-bases' / 'data' / 'ehr'
        ehr_dir.mkdir(parents=True)
        guidelines_dir = tmp_path / 'knowledge-bases' / 'data' / 'guidelines'
        guidelines_dir.mkdir(parents=True)

        mock_session = MagicMock()
        mock_ssm = MagicMock()
        mock_s3 = MagicMock()
        mock_bedrock = MagicMock()

        mock_session.client.side_effect = lambda svc, **kwargs: {
            'ssm': mock_ssm,
            's3': mock_s3,
            'bedrock-agent': mock_bedrock,
        }[svc]

        def ssm_get(Name):
            params = {
                '/clinicalcompass/ehr-data-bucket': 'my-ehr-bucket',
                '/clinicalcompass/guidelines-bucket': 'my-guidelines-bucket',
                '/clinicalcompass/kb/cc-ehr-mock/id': 'KB_EHR_123',
                '/clinicalcompass/kb/cc-global-guidelines/id': 'KB_GL_456',
            }
            if Name in params:
                return {'Parameter': {'Value': params[Name]}}
            from botocore.exceptions import ClientError
            raise ClientError(
                {'Error': {'Code': 'ParameterNotFound', 'Message': ''}},
                'GetParameter'
            )

        mock_ssm.get_parameter.side_effect = ssm_get

        # Mock list_data_sources to return a data source
        mock_bedrock.list_data_sources.return_value = {
            'dataSourceSummaries': [{'dataSourceId': 'DS_001'}]
        }
        mock_bedrock.start_ingestion_job.return_value = {
            'ingestionJob': {'ingestionJobId': 'JOB_001'}
        }

        with patch('post_cdk_setup.PROJECT_ROOT', tmp_path):
            upload_initial_data(mock_session)

        # Verify KB sync was triggered for both KBs
        assert mock_bedrock.start_ingestion_job.call_count == 2
        sync_calls = mock_bedrock.start_ingestion_job.call_args_list
        kb_ids_synced = {c[1]['knowledgeBaseId'] for c in sync_calls}
        assert kb_ids_synced == {'KB_EHR_123', 'KB_GL_456'}

    def test_skips_upload_when_ssm_params_missing(self, tmp_path):
        from post_cdk_setup import upload_initial_data
        from botocore.exceptions import ClientError

        ehr_dir = tmp_path / 'knowledge-bases' / 'data' / 'ehr'
        ehr_dir.mkdir(parents=True)
        (ehr_dir / 'patient_001.json').write_text('{}')

        mock_session = MagicMock()
        mock_ssm = MagicMock()
        mock_s3 = MagicMock()
        mock_bedrock = MagicMock()

        mock_session.client.side_effect = lambda svc, **kwargs: {
            'ssm': mock_ssm,
            's3': mock_s3,
            'bedrock-agent': mock_bedrock,
        }[svc]

        # All SSM params not found
        mock_ssm.get_parameter.side_effect = ClientError(
            {'Error': {'Code': 'ParameterNotFound', 'Message': ''}},
            'GetParameter'
        )

        with patch('post_cdk_setup.PROJECT_ROOT', tmp_path):
            upload_initial_data(mock_session)

        # No S3 uploads should happen
        mock_s3.upload_file.assert_not_called()
        # No KB sync should happen
        mock_bedrock.start_ingestion_job.assert_not_called()

    def test_idempotent_rerun_overwrites_existing(self, tmp_path):
        """Re-running upload is safe — S3 PutObject overwrites existing objects."""
        from post_cdk_setup import upload_initial_data

        ehr_dir = tmp_path / 'knowledge-bases' / 'data' / 'ehr'
        ehr_dir.mkdir(parents=True)
        (ehr_dir / 'patient_001_test.json').write_text('{"patient_id": "001"}')

        guidelines_dir = tmp_path / 'knowledge-bases' / 'data' / 'guidelines'
        guidelines_dir.mkdir(parents=True)

        mock_session = MagicMock()
        mock_ssm = MagicMock()
        mock_s3 = MagicMock()
        mock_bedrock = MagicMock()

        mock_session.client.side_effect = lambda svc, **kwargs: {
            'ssm': mock_ssm,
            's3': mock_s3,
            'bedrock-agent': mock_bedrock,
        }[svc]

        def ssm_get(Name):
            params = {
                '/clinicalcompass/ehr-data-bucket': 'my-ehr-bucket',
                '/clinicalcompass/guidelines-bucket': 'my-guidelines-bucket',
                '/clinicalcompass/kb/cc-ehr-mock/id': 'PLACEHOLDER',
                '/clinicalcompass/kb/cc-global-guidelines/id': 'PLACEHOLDER',
            }
            if Name in params:
                return {'Parameter': {'Value': params[Name]}}
            from botocore.exceptions import ClientError
            raise ClientError(
                {'Error': {'Code': 'ParameterNotFound', 'Message': ''}},
                'GetParameter'
            )

        mock_ssm.get_parameter.side_effect = ssm_get

        with patch('post_cdk_setup.PROJECT_ROOT', tmp_path):
            # Run twice — should not raise
            upload_initial_data(mock_session)
            upload_initial_data(mock_session)

        # Each run uploads the same file
        assert mock_s3.upload_file.call_count == 2
