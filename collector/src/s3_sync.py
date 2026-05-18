import logging
import shutil
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


def _s3_client():  # type: ignore[no-untyped-def]
    import boto3

    return boto3.client("s3", region_name=settings.aws_region)


def upload_to_s3(local_path: Path, s3_key: str) -> None:
    client = _s3_client()
    client.upload_file(str(local_path), settings.aws_bucket, s3_key)
    logger.info("Uploaded %s → s3://%s/%s", local_path.name, settings.aws_bucket, s3_key)


def save(local_path: Path) -> None:
    """Route a completed parquet file to local archive or S3 depending on ENV."""
    if settings.env == "aws":
        s3_key = f"raw/{local_path.name}"
        upload_to_s3(local_path, s3_key)
    else:
        # File is already written to the shared volume; nothing more to do.
        logger.debug("Local mode: parquet already at %s", local_path)
