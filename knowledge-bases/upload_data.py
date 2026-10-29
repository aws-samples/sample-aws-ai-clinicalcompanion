"""
Upload data to S3 buckets for Knowledge Bases
Utility script to upload or update data files
"""

import sys
from pathlib import Path

# Add deployment directory to path for config access
sys.path.insert(0, str(Path(__file__).parent.parent / 'deployment'))
from config import AWS_PROFILE, AWS_REGION, AWS_ACCOUNT_ID

import boto3
import json
import logging
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DataUploader:
    """Upload data files to S3 buckets"""
    
    def __init__(self, profile: str = AWS_PROFILE, region: str = AWS_REGION):
        self.session = boto3.Session(profile_name=profile, region_name=region)
        self.s3 = self.session.client('s3')
        self.bedrock_agent = self.session.client('bedrock-agent')
    
    def upload_directory(self, local_path: Path, bucket_name: str, s3_prefix: str = ""):
        """Upload all files from a directory to S3"""
        if not local_path.exists():
            logger.error(f"Local path does not exist: {local_path}")
            return 0
        
        files_uploaded = 0
        
        for file_path in local_path.rglob('*'):
            if file_path.is_file():
                relative_path = file_path.relative_to(local_path)
                s3_key = f"{s3_prefix}/{relative_path}" if s3_prefix else str(relative_path)
                s3_key = s3_key.replace('\\', '/')  # Windows compatibility
                
                try:
                    self.s3.upload_file(str(file_path), bucket_name, s3_key)
                    logger.info(f"✅ Uploaded: {relative_path} -> s3://{bucket_name}/{s3_key}")
                    files_uploaded += 1
                except Exception as e:
                    logger.error(f"❌ Failed to upload {file_path}: {e}")
        
        logger.info(f"\nTotal files uploaded: {files_uploaded}")
        return files_uploaded
    
    def trigger_ingestion(self, kb_id: str, ds_id: str):
        """Trigger ingestion job after uploading new data"""
        try:
            response = self.bedrock_agent.start_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=ds_id
            )
            
            job_id = response['ingestionJob']['ingestionJobId']
            logger.info(f"✅ Started ingestion job: {job_id}")
            logger.info(f"Monitor status with: aws bedrock-agent get-ingestion-job --knowledge-base-id {kb_id} --data-source-id {ds_id} --ingestion-job-id {job_id}")
            
            return job_id
        except Exception as e:
            logger.error(f"❌ Failed to start ingestion: {e}")
            return None


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Upload data to Knowledge Base S3 buckets')
    parser.add_argument('--data-type', choices=['ehr', 'guidelines', 'regulatory', 'all'],
                       required=True, help='Type of data to upload')
    parser.add_argument('--trigger-ingestion', action='store_true',
                       help='Trigger ingestion job after upload')
    
    args = parser.parse_args()
    
    uploader = DataUploader()
    base_path = Path(__file__).parent / 'data'
    
    # Load Knowledge Base IDs
    kb_ids_file = Path(__file__).parent / 'knowledge_base_ids.json'
    if not kb_ids_file.exists():
        logger.error("❌ knowledge_base_ids.json not found. Run setup_knowledge_bases.py first.")
        return
    
    with open(kb_ids_file) as f:
        kb_ids = json.load(f)
    
    # Upload based on data type
    if args.data_type == 'ehr' or args.data_type == 'all':
        logger.info("\n" + "="*60)
        logger.info("Uploading EHR Mock Data")
        logger.info("="*60)
        
        bucket = kb_ids['ehr_mock']['bucket_name']
        local_path = base_path / 'ehr'
        uploader.upload_directory(local_path, bucket)
        
        if args.trigger_ingestion:
            uploader.trigger_ingestion(
                kb_ids['ehr_mock']['kb_id'],
                kb_ids['ehr_mock']['data_source_id']
            )
    
    if args.data_type == 'guidelines' or args.data_type == 'all':
        logger.info("\n" + "="*60)
        logger.info("Uploading Global Guidelines")
        logger.info("="*60)
        
        bucket = kb_ids['global_guidelines']['bucket_name']
        local_path = base_path / 'guidelines'
        uploader.upload_directory(local_path, bucket)
        
        if args.trigger_ingestion:
            uploader.trigger_ingestion(
                kb_ids['global_guidelines']['kb_id'],
                kb_ids['global_guidelines']['data_source_id']
            )
    
    if args.data_type == 'regulatory' or args.data_type == 'all':
        logger.info("\n" + "="*60)
        logger.info("Uploading Local Regulatory Protocols")
        logger.info("="*60)
        
        bucket = kb_ids['local_regulatory']['bucket_name']
        local_path = base_path / 'regulatory'
        uploader.upload_directory(local_path, bucket)
        
        if args.trigger_ingestion:
            uploader.trigger_ingestion(
                kb_ids['local_regulatory']['kb_id'],
                kb_ids['local_regulatory']['data_source_id']
            )
    
    logger.info("\n✅ Upload complete!")


if __name__ == '__main__':
    main()
