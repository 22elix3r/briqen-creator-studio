from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from studio import settings

pool = ConnectionPool(
    settings.DATABASE_URL, kwargs={"row_factory": dict_row}, min_size=1, max_size=8, open=False
)


def migrate():
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(20261121)")
        conn.execute(Path(__file__).with_name("schema.sql").read_text())


if __name__ == "__main__":
    with pool:
        migrate()
