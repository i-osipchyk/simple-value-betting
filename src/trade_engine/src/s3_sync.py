import logging
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


def save(local_path: Path) -> None:
    """Copy a local file to S3 mirroring its path relative to the data dir. No-op when ENV != aws."""
    if settings.env != "aws":
        return
    import boto3

    s3_key = str(local_path.relative_to(settings.local_data_dir))
    boto3.client("s3", region_name=settings.aws_region).upload_file(
        str(local_path), settings.aws_bucket, s3_key
    )
    logger.info("Uploaded %s → s3://%s/%s", local_path.name, settings.aws_bucket, s3_key)
