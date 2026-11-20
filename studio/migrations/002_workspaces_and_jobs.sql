ALTER TABLE workspaces
    ADD COLUMN name text,
    ADD COLUMN max_active_jobs integer NOT NULL DEFAULT 8 CHECK (max_active_jobs BETWEEN 1 AND 100),
    ADD COLUMN created_at timestamptz NOT NULL DEFAULT clock_timestamp();
UPDATE workspaces SET name = CASE WHEN id = 'demo' THEN 'Local demo' ELSE id END;
ALTER TABLE workspaces ALTER COLUMN name SET NOT NULL;

CREATE TABLE workspace_keys (
    id uuid PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    label text NOT NULL,
    role text NOT NULL CHECK (role IN ('editor', 'viewer')),
    digest text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE browser_sessions (
    digest text PRIMARY KEY,
    key_id uuid NOT NULL REFERENCES workspace_keys(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);
CREATE INDEX browser_sessions_expiry ON browser_sessions (expires_at);
CREATE INDEX workspace_keys_workspace ON workspace_keys (workspace_id, created_at DESC);

ALTER TABLE projects ADD UNIQUE (id, workspace_id);
ALTER TABLE scenes ADD UNIQUE (id, project_id);
ALTER TABLE script_revisions ADD UNIQUE (id, project_id);
ALTER TABLE assets ADD UNIQUE (id, project_id);

ALTER TABLE jobs
    ADD UNIQUE (id, workspace_id),
    ADD UNIQUE (id, project_id),
    ADD COLUMN provider text CHECK (provider IN ('fixture', 'replicate')),
    ADD COLUMN provider_run_id text,
    ADD COLUMN provider_submit_started_at timestamptz,
    ADD COLUMN last_retry_at timestamptz,
    ADD COLUMN requested_by uuid REFERENCES workspace_keys(id),
    ADD COLUMN poll_count integer NOT NULL DEFAULT 0,
    ADD FOREIGN KEY (project_id, workspace_id) REFERENCES projects(id, workspace_id),
    ADD FOREIGN KEY (scene_id, project_id) REFERENCES scenes(id, project_id);

UPDATE jobs SET provider = 'fixture', provider_run_id = id::text
WHERE kind IN ('media', 'script');
ALTER TABLE jobs ADD CHECK ((kind = 'export') = (provider IS NULL));

ALTER TABLE assets ADD FOREIGN KEY (job_id, project_id) REFERENCES jobs(id, project_id);
ALTER TABLE credit_reservations
    ADD FOREIGN KEY (job_id, workspace_id) REFERENCES jobs(id, workspace_id);
ALTER TABLE script_revisions
    ADD FOREIGN KEY (parent_id, project_id) REFERENCES script_revisions(id, project_id);
ALTER TABLE projects
    ADD FOREIGN KEY (current_revision_id, id) REFERENCES script_revisions(id, project_id);
ALTER TABLE scenes
    ADD FOREIGN KEY (script_revision_id, project_id) REFERENCES script_revisions(id, project_id),
    ADD FOREIGN KEY (accepted_asset_id, project_id) REFERENCES assets(id, project_id),
    ADD FOREIGN KEY (candidate_asset_id, project_id) REFERENCES assets(id, project_id),
    ADD FOREIGN KEY (latest_job_id, project_id) REFERENCES jobs(id, project_id);

CREATE INDEX projects_workspace_created ON projects (workspace_id, created_at DESC, id DESC);
CREATE INDEX revisions_project_created ON script_revisions (project_id, created_at DESC);
CREATE INDEX jobs_project_created ON jobs (project_id, created_at DESC, id DESC);
CREATE INDEX jobs_workspace_active ON jobs (workspace_id) WHERE state IN ('queued', 'running', 'confirming');
CREATE UNIQUE INDEX jobs_provider_run_unique ON jobs (provider, provider_run_id)
    WHERE provider_run_id IS NOT NULL;
CREATE INDEX provider_runs_due ON provider_runs (available_at) WHERE callback_sent_at IS NULL;

CREATE TABLE credit_ledger (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    job_id uuid,
    event text NOT NULL CHECK (event IN ('opening', 'grant', 'hold', 'capture', 'release')),
    balance_delta integer NOT NULL,
    held_delta integer NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (job_id, workspace_id) REFERENCES jobs(id, workspace_id),
    UNIQUE (job_id, event),
    CHECK ((event IN ('opening', 'grant')) = (job_id IS NULL)),
    CHECK (
        (event = 'opening' AND balance_delta >= 0 AND held_delta BETWEEN 0 AND balance_delta)
        OR (event = 'grant' AND balance_delta > 0 AND held_delta = 0)
        OR (event = 'hold' AND balance_delta = 0 AND held_delta > 0)
        OR (event = 'capture' AND balance_delta < 0 AND held_delta = balance_delta)
        OR (event = 'release' AND balance_delta = 0 AND held_delta < 0)
    )
);
CREATE INDEX credit_ledger_workspace ON credit_ledger (workspace_id, id DESC);
INSERT INTO credit_ledger (workspace_id, event, balance_delta, held_delta, note)
SELECT id, 'opening', credit_balance, held_credits, 'pre-ledger balance' FROM workspaces;

CREATE TABLE provider_deliveries (
    provider text NOT NULL,
    event_id text NOT NULL,
    job_id uuid NOT NULL REFERENCES jobs(id),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (provider, event_id)
);
