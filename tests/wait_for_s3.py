"""Wait for the disposable S3 service to provision its fixture bucket."""

import time

from botocore.exceptions import BotoCoreError, ClientError

from studio import settings
from studio.media import s3_client


def main():
    deadline = time.monotonic() + 45
    while True:
        try:
            s3_client().head_bucket(Bucket=settings.S3_BUCKET)
            return
        except (BotoCoreError, ClientError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


if __name__ == "__main__":
    main()
