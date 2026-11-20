"""Local operator commands. Access keys are returned once on stdout."""

import argparse
import json
import re
import secrets
from uuid import UUID, uuid4

from studio import settings, workflow
from studio.auth import digest
from studio.db import pool


def workspace_id(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", value):
        raise argparse.ArgumentTypeError(
            "Use 2–40 lowercase letters, digits, dashes or underscores"
        )
    return value


def bounded_int(minimum, maximum):
    def parse(value):
        result = int(value)
        if not minimum <= result <= maximum:
            raise argparse.ArgumentTypeError(f"Enter a number from {minimum} to {maximum}")
        return result

    return parse


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    create = commands.add_parser("workspace-create")
    create.add_argument("workspace", type=workspace_id)
    create.add_argument("--name", required=True)
    create.add_argument("--credits", type=bounded_int(0, 100_000_000), default=0)
    create.add_argument("--max-active-jobs", type=bounded_int(1, 100), default=8)

    grant = commands.add_parser("credit-grant")
    grant.add_argument("workspace", type=workspace_id)
    grant.add_argument("amount", type=bounded_int(1, 100_000_000))
    grant.add_argument("--note", required=True)

    issue = commands.add_parser("key-issue")
    issue.add_argument("workspace", type=workspace_id)
    issue.add_argument("--label", required=True)
    issue.add_argument("--role", choices=("editor", "viewer"), default="viewer")
    issue.add_argument("--days", type=bounded_int(1, 365), default=30)

    revoke = commands.add_parser("key-revoke")
    revoke.add_argument("id", type=UUID)

    sessions = commands.add_parser("session-prune")
    sessions.add_argument("--limit", type=bounded_int(1, 10000), default=1000)

    verify = commands.add_parser("credit-audit")
    verify.add_argument("workspace", type=workspace_id)
    return cli


def execute(conn, args):
    if args.command == "workspace-create":
        row = conn.execute(
            """
            INSERT INTO workspaces (id, name, credit_balance, max_active_jobs)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id
            """,
            (args.workspace, args.name.strip()[:120], args.credits, args.max_active_jobs),
        ).fetchone()
        if not row:
            raise ValueError("Workspace already exists")
        workflow.ledger_entry(conn, args.workspace, "opening", args.credits, 0)
        return row
    if args.command == "credit-grant":
        row = conn.execute(
            """
            UPDATE workspaces SET credit_balance = credit_balance + %s
            WHERE id = %s RETURNING credit_balance
            """,
            (args.amount, args.workspace),
        ).fetchone()
        if not row:
            raise ValueError("Workspace does not exist")
        workflow.ledger_entry(conn, args.workspace, "grant", args.amount, 0, note=args.note[:240])
        return row
    if args.command == "key-issue":
        if (
            conn.execute("SELECT 1 FROM workspaces WHERE id = %s", (args.workspace,)).fetchone()
            is None
        ):
            raise ValueError("Workspace does not exist")
        value = f"st_{secrets.token_urlsafe(32)}"
        row = conn.execute(
            """
            INSERT INTO workspace_keys (id, workspace_id, label, role, digest, expires_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp() + %s * interval '1 day')
            RETURNING id, expires_at
            """,
            (
                uuid4(),
                args.workspace,
                args.label.strip()[:120],
                args.role,
                digest(value),
                args.days,
            ),
        ).fetchone()
        return {**row, "key": value}
    if args.command == "key-revoke":
        row = conn.execute(
            """
            UPDATE workspace_keys SET revoked_at = coalesce(revoked_at, clock_timestamp())
            WHERE id = %s RETURNING id, revoked_at
            """,
            (args.id,),
        ).fetchone()
        if not row:
            raise ValueError("Key does not exist")
        conn.execute("DELETE FROM browser_sessions WHERE key_id = %s", (args.id,))
        return row
    if args.command == "session-prune":
        result = conn.execute(
            """
            DELETE FROM browser_sessions WHERE digest IN (
                SELECT digest FROM browser_sessions WHERE expires_at <= clock_timestamp()
                ORDER BY expires_at LIMIT %s
            )
            """,
            (args.limit,),
        )
        return {"deleted": result.rowcount}
    return workflow.audit_credits(conn, args.workspace)


def main():
    cli = parser()
    args = cli.parse_args()
    settings.validate("admin")
    try:
        with pool, pool.connection() as conn:
            result = execute(conn, args)
    except (ValueError, workflow.StudioError) as exc:
        cli.error(str(exc))
    print(json.dumps(result, default=str))
    if args.command == "credit-audit" and not result["consistent"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
