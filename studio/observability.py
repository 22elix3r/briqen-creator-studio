import json
import logging
from datetime import UTC, datetime

from studio.db import pool


class JsonFormatter(logging.Formatter):
    def format(self, record):
        attributes = (
            "request_id",
            "route",
            "method",
            "status",
            "duration_ms",
            "job_id",
            "kind",
            "state",
            "error_type",
            "location",
        )
        fields = {key: getattr(record, key) for key in attributes if hasattr(record, key)}
        return json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "event": record.getMessage(),
                **fields,
            },
            default=str,
        )


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def queue_metrics():
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT kind, state, count(*) AS total FROM jobs WHERE state IN ('queued', 'running', 'confirming')
            GROUP BY kind, state
            """
        ).fetchall()
        health = conn.execute(
            """
            SELECT coalesce(extract(epoch FROM clock_timestamp() - min(run_after)), 0) AS due_seconds
            FROM jobs WHERE state IN ('queued', 'running', 'confirming')
                AND run_after <= clock_timestamp()
                AND (lease_until IS NULL OR lease_until <= clock_timestamp())
            """
        ).fetchone()
        overdue = conn.execute(
            """
            SELECT count(*) AS total FROM credit_reservations c JOIN jobs j ON j.id = c.job_id
            WHERE c.state = 'held' AND j.expires_at < clock_timestamp() - interval '1 minute'
            """
        ).fetchone()
    lines = [
        "# TYPE studio_jobs_active gauge",
        *(
            f'studio_jobs_active{{kind="{row["kind"]}",state="{row["state"]}"}} {row["total"]}'
            for row in rows
        ),
        "# TYPE studio_queue_oldest_due_seconds gauge",
        f"studio_queue_oldest_due_seconds {max(0, health['due_seconds'])}",
        "# TYPE studio_overdue_credit_holds gauge",
        f"studio_overdue_credit_holds {overdue['total']}",
    ]
    return "\n".join(lines) + "\n"
