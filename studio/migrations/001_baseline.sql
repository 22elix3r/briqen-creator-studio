CREATE TABLE IF NOT EXISTS workspaces (
    id text PRIMARY KEY,
    credit_balance integer NOT NULL CHECK (credit_balance >= 0),
    held_credits integer NOT NULL DEFAULT 0 CHECK (held_credits BETWEEN 0 AND credit_balance)
);

CREATE TABLE IF NOT EXISTS projects (
    id uuid PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    title text NOT NULL,
    brief text NOT NULL,
    current_revision_id uuid,
    timeline_version integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS script_revisions (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    parent_id uuid REFERENCES script_revisions(id),
    content text NOT NULL,
    source text NOT NULL CHECK (source IN ('editor', 'provider')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS scenes (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    script_revision_id uuid NOT NULL REFERENCES script_revisions(id),
    title text NOT NULL,
    prompt text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('image', 'video')),
    visual_version integer NOT NULL DEFAULT 1,
    position integer NOT NULL CHECK (position >= 0),
    trim_ms integer NOT NULL DEFAULT 0 CHECK (trim_ms >= 0),
    duration_ms integer NOT NULL CHECK (duration_ms BETWEEN 1000 AND 15000),
    accepted_asset_id uuid,
    candidate_asset_id uuid,
    latest_job_id uuid,
    UNIQUE (project_id, position) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    scene_id uuid REFERENCES scenes(id),
    kind text NOT NULL CHECK (kind IN ('script', 'media', 'export')),
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'confirming', 'succeeded', 'failed', 'expired')),
    action_key uuid NOT NULL,
    request jsonb NOT NULL,
    input jsonb NOT NULL,
    result jsonb,
    error text,
    run_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    lease_token uuid,
    lease_until timestamptz,
    wake_seq integer NOT NULL DEFAULT 0,
    provider_notified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((kind = 'media') = (scene_id IS NOT NULL)),
    UNIQUE (workspace_id, action_key)
);

CREATE INDEX IF NOT EXISTS jobs_due ON jobs (run_after, id)
    WHERE state IN ('queued', 'running', 'confirming');
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_media ON jobs (scene_id)
    WHERE kind = 'media' AND state IN ('queued', 'running', 'confirming');

CREATE TABLE IF NOT EXISTS assets (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL UNIQUE REFERENCES jobs(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    kind text NOT NULL CHECK (kind IN ('image', 'video', 'export')),
    storage_key text NOT NULL UNIQUE,
    duration_ms integer CHECK (duration_ms > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS credit_reservations (
    job_id uuid PRIMARY KEY REFERENCES jobs(id),
    workspace_id text NOT NULL REFERENCES workspaces(id),
    amount integer NOT NULL CHECK (amount > 0),
    state text NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'captured', 'released')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS job_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs(id),
    event text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events (job_id, id);

-- This table belongs to the deterministic provider. The Studio never queries it.
CREATE TABLE IF NOT EXISTS provider_runs (
    id uuid PRIMARY KEY,
    input jsonb NOT NULL,
    available_at timestamptz NOT NULL,
    callback_sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
