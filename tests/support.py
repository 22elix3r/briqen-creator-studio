from argparse import Namespace
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from studio import settings
from studio.admin import execute


def db_connection():
    return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)


def workspace(credits=100, max_active_jobs=8):
    slug = "test-" + uuid4().hex
    with db_connection() as conn:
        execute(
            conn,
            Namespace(
                command="workspace-create",
                workspace=slug,
                name="Test workspace",
                credits=credits,
                max_active_jobs=max_active_jobs,
            ),
        )
    return slug


def key(workspace_id, role="editor"):
    with db_connection() as conn:
        return execute(
            conn,
            Namespace(
                command="key-issue",
                workspace=workspace_id,
                label="Integration test",
                role=role,
                days=1,
            ),
        )
