"""Bootstrap AWS resources for local --mode aws testing (spec 7.1 scripts/).

Creates the S3 bucket (and prints the follow-up steps that require confirmed,
not-yet-provided values) so a developer can run ``make run-aws`` against a real
account. Deliberately conservative: it creates the bucket if missing and STOPS at
anything that needs a TODO(confirm) value (BDA project ARN, KB role ARN, vector
bucket ARN, model IDs), printing exactly what to supply rather than inventing it.

This is a convenience helper, not part of the agent runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kb_agent.config import get_settings  # noqa: E402
from kb_agent.logging_setup import configure_logging, get_logger  # noqa: E402

logger = get_logger(__name__)


def main() -> int:  # pragma: no cover - requires live AWS
    configure_logging("INFO")
    settings = get_settings()

    if settings.s3_bucket.startswith("REPLACE_ME"):
        print(
            "S3 bucket is a placeholder. Set KB_AGENT_S3_BUCKET in .env before "
            "bootstrapping (OPEN_QUESTIONS Q6/Q8).",
            file=sys.stderr,
        )
        return 1

    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3", region_name=settings.aws_region)
    try:
        s3.head_bucket(Bucket=settings.s3_bucket)
        logger.info("bucket_exists", bucket=settings.s3_bucket)
    except ClientError:
        kwargs = {"Bucket": settings.s3_bucket}
        if settings.aws_region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.aws_region}
        s3.create_bucket(**kwargs)
        logger.info("bucket_created", bucket=settings.s3_bucket)

    # Everything beyond the bucket needs confirmed values we must not invent.
    print(
        "\nBucket is ready. The following still require CONFIRMED values before a "
        "real run (see OPEN_QUESTIONS.md); this script will not invent them:\n"
        "  - BDA project ARN (Q6)\n"
        "  - Embedding + Claude model IDs (Q11)\n"
        "  - KB service role ARN and S3 Vectors bucket/index ARN (Q7/Q8)\n"
        "  - Bedrock Guardrail id + version\n"
        "Set these in .env, then run `make run-aws`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
