"""Where backups are kept: this server's folder (settings.BACKUP_ROOT) or Amazon S3
(settings.BACKUP_S3). Each backup records which, so changing a schedule's destination never
loses track of the backups it already made.

pg_dump and pg_restore work on local files, so a dump is always made in the folder first and an
S3 store then moves it up; a rollback from S3 brings the file down into the folder for the
restore and deletes it after.
"""

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from django.conf import settings


def local_root() -> Path:
    return Path(settings.BACKUP_ROOT)


def staging_dir() -> Path:
    """Where dumps are made and S3 backups are brought down to. Inside the folder, so a dump
    never leaves the private place backups live."""
    path = local_root() / ".staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


class LocalStore:
    name = "local"

    def save(self, file: Path, key: str) -> None:
        target = local_root() / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(file, target)

    @contextmanager
    def fetch(self, key: str) -> Iterator[Path]:
        yield local_root() / key

    def exists(self, key: str) -> bool:
        return (local_root() / key).is_file()

    def delete(self, key: str) -> None:
        (local_root() / key).unlink(missing_ok=True)

    def describe(self, key: str) -> str:
        return str(local_root() / key)


class S3Store:
    name = "s3"

    def __init__(self, config: dict):
        self.bucket = config["bucket"]
        self.prefix = config["prefix"]
        self.config = config

    @property
    def client(self):
        return _s3_client(
            self.config["region"],
            self.config["endpoint_url"],
            self.config["access_key_id"],
            self.config["secret_access_key"],
        )

    def _object(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def save(self, file: Path, key: str) -> None:
        # Encrypted at rest whatever the bucket's default: a dump holds client data.
        self.client.upload_file(
            str(file),
            self.bucket,
            self._object(key),
            ExtraArgs={"ServerSideEncryption": "AES256"},
        )
        file.unlink()

    @contextmanager
    def fetch(self, key: str) -> Iterator[Path]:
        local = staging_dir() / Path(key).name
        try:
            self.client.download_file(self.bucket, self._object(key), str(local))
            yield local
        finally:
            local.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._object(key))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._object(key))

    def describe(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._object(key)}"


@cache
def _s3_client(region, endpoint_url, access_key_id, secret_access_key):
    import boto3

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )


def s3_configured() -> bool:
    return bool(settings.BACKUP_S3.get("bucket"))


class NotConfigured(Exception):
    pass


def store(name: str) -> LocalStore | S3Store:
    """The store a backup is kept in, by its `storage` value."""
    if name == "s3":
        if not s3_configured():
            raise NotConfigured(
                "Amazon S3 isn't set up: set AWS_S3_BACKUP_BUCKET (and credentials)."
            )
        return S3Store(settings.BACKUP_S3)
    return LocalStore()
