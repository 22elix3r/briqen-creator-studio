import hashlib
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from studio import settings

MIGRATIONS = Path(__file__).with_name("migrations")
EXPECTED_MIGRATION = max(file.name for file in MIGRATIONS.glob("*.sql"))

pool = ConnectionPool(
    settings.DATABASE_URL,
    kwargs={
        "row_factory": dict_row,
        "options": "-c statement_timeout=15000 -c idle_in_transaction_session_timeout=20000",
        "connect_timeout": 5,
    },
    min_size=1,
    max_size=10,
    timeout=4,
    open=False,
)


def migrate():
    settings.validate("migrate")
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(20261121)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name text PRIMARY KEY, sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )
        installed = {
            row["name"]: row["sha256"]
            for row in conn.execute("SELECT name, sha256 FROM schema_migrations").fetchall()
        }
        for filename in sorted(MIGRATIONS.glob("*.sql")):
            migration = filename.read_bytes()
            checksum = hashlib.sha256(migration).hexdigest()
            if filename.name in installed:
                if installed[filename.name] != checksum:
                    raise RuntimeError(f"Applied migration was modified: {filename.name}")
                continue
            conn.execute(migration.decode())
            conn.execute(
                "INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)",
                (filename.name, checksum),
            )
        if settings.MODE == "demo":
            conn.execute(
                """
                WITH demo AS (
                  INSERT INTO workspaces (id, name, credit_balance) VALUES ('demo', 'Local demo', 100)
                  ON CONFLICT DO NOTHING RETURNING id, credit_balance
                )
                INSERT INTO credit_ledger (workspace_id, event, balance_delta, held_delta)
                SELECT id, 'opening', credit_balance, 0 FROM demo
                """
            )


def schema_ready():
    with pool.connection() as conn:
        applied = conn.execute("SELECT max(name) AS name FROM schema_migrations").fetchone()
        return applied["name"] == EXPECTED_MIGRATION


if __name__ == "__main__":
    with pool:
        migrate()
