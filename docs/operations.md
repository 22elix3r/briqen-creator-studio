# Deploy and operate Creator Studio

The production profile runs an API and independently scalable workers. Plan for a managed PostgreSQL database, a private S3 bucket, a public HTTPS origin, and a Replicate account with approved script, image, and video models. Configure the staging environment before creating customer workspaces.

## Provision dependencies

1. Create a PostgreSQL database with PITR and encrypted connections. Use a schema owner to migrate and a dedicated runtime user. Scope the runtime user to the Studio tables and sequences. It needs reads, inserts, job and wallet updates, and browser session deletion. Keep schema changes and backup permissions with the operator.
2. Create a private S3 bucket. Enable Block Public Access, versioning, and default encryption. Give the API prefix-scoped `GetObject` for signed URLs and give workers `GetObject` and `PutObject` on the same prefix. The worker uses conditional `PutObject`. Keep current objects referenced by the database for as long as the associated projects are retained.
3. Choose Replicate models with a verified input and output schema. A model setting accepts an official `owner/name` or a 64-character version hash. Match each prompt field, supply required model options using the JSON input settings, and pin version hashes where available. Text output can be plain text or an array of text fragments. Media should return a usable PNG, JPEG, WebP, or MP4 URL on Replicate's delivery domain. Use a video length that fits your expected storyboard timing.
4. Retrieve the Replicate webhook signing secret for the account used to create predictions. Store it with the API configuration. Store the Replicate API key with the worker configuration. Keep the Studio signing key the same across the API and workers. The app accepts `_FILE` variants for its secrets when using mounted secret files.

AWS credentials use the SDK credential chain. Use workload credentials where available. The production profile enforces S3, a private Studio signing key of at least 32 bytes, and an HTTPS public origin. [AWS private object storage](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html), [Replicate webhook keys](https://replicate.com/docs/topics/webhooks/verify-webhook/)

## Build and migrate

```sh
cp .env.production.example .env.production
chmod 600 .env.production
docker build -t creator-studio:1.0.0 .
```

Fill the environment file through your normal secret-management workflow. The example uses a single file for Docker Compose; a production orchestrator can inject service-specific credentials and use separate IAM roles. Keep the file and mounted credentials outside build contexts and version control.

Test backup restoration in staging before a production schema change. For the prototype upgrade, drain active jobs and stop the old API and workers before applying the migrations. The first migration adopts the existing tables. The second preserves project, job, media, and wallet IDs and establishes an opening ledger balance for each existing workspace. Keep access to its object storage when upgrading data. Migrating a local media volume to S3 requires copying the referenced files under the new prefix before serving the upgraded database.

```sh
STUDIO_ENV_FILE=/etc/creator-studio/migrate.env \
  docker compose -p creator-prod -f compose.production.yaml run --rm migrate

docker compose --env-file .env.production -p creator-prod -f compose.production.yaml \
  up -d --wait --wait-timeout 90 api worker
```

The migration environment can contain just the database connection for the schema owner. Apply migrations once per release. The migrator records file hashes inside an advisory-lock transaction. A failed migration rolls back. API readiness checks the expected schema version. To roll back after a schema change, restore an isolated database from backup and redeploy a compatible image, or roll forward with a corrective migration. Keep prior images and migrate using a maintenance window for incompatible revisions.

The API binds to loopback by default. Terminate TLS in a reverse proxy or managed gateway, route `STUDIO_PUBLIC_ORIGIN` to the API, and allow `POST /webhooks/replicate/*` from the provider. Apply ingress rate limits to sign-in and write-heavy API routes. Keep webhook delivery capacity separate from user traffic. Redact query strings, bearer headers, and webhook URL parameters in proxy logs. A request ID is returned as `X-Request-ID`. Secure cookies are issued in production. Use exact origin matching in the environment.

## Provision a workspace

Run operator commands from a trusted shell and deliver the resulting key through your team's secret manager. The API stores only key and session digests. Issuing a replacement and revoking the previous key supports rotation. Revocation invalidates its browser sessions.

```sh
docker compose --env-file .env.production -p creator-prod -f compose.production.yaml \
  exec -T api python -m studio.admin workspace-create acme --name "Acme Studio" \
  --credits 100 --max-active-jobs 8

docker compose --env-file .env.production -p creator-prod -f compose.production.yaml \
  exec -T api python -m studio.admin key-issue acme --role editor --label "Campaign team" --days 30
```

Other operator commands are `key-revoke <key-id>`, `credit-grant <workspace> <amount> --note <reference>`, `credit-audit <workspace>`, and `session-prune`. A key tagged `viewer` grants read and download access. Credits are provisioned by operators. Choose prices and provider spend limits for your selected models before granting access.

## Run the release canary

Run both repository checks in [README](../README.md). Then use a dedicated staging workspace and manually exercise a paid script, image, and video job with the exact production models. Confirm the provider status, the signed callback, a single charge on successful publication, and quota and key revocation behavior. Approve two assets, export an MP4, and download it from a browser on the real origin. Confirm an anonymous S3 request is denied and that a viewer can open the project. Check that the credit audit matches the wallet.

The automated provider checks exercise the documented HTTP and callback contract with a controlled transport, while the object storage tests exercise a private S3-compatible service. The live models, IAM policies, TLS origin, and webhook delivery are deployment-specific canary items.

## Monitor and recover

| Signal | What to check |
| --- | --- |
| `GET /livez` | API process responds. |
| `GET /healthz` | API can reach PostgreSQL and sees the expected migration. |
| `studio_queue_oldest_due_seconds` | Sustained increase indicates unavailable or saturated workers. Check image version, lease logs, and CPU. |
| `studio_jobs_active` | Track queued, running, and confirming jobs by kind. Rising confirming counts point to provider acknowledgements or delivery issues. |
| `studio_overdue_credit_holds` | An expired job is still holding credits. Check workers and the credit audit before applying adjustments. |

Set `STUDIO_METRICS_TOKEN` and use that value as a Bearer token to scrape `/metrics`. Keep that route on the operator network. Use request IDs and job IDs to correlate structured API and worker logs. Keep provider credentials, raw webhook payloads, prompts, and signed URLs out of log aggregation.

When a provider request is confirming, first inspect the job and provider history. A known prediction ID can be polled. When the acknowledgement is ambiguous, let its signed callback correlate the ID or let the configured recovery window expire. Retry wakes that job. A customer may choose a fresh paid generation after the old operation ends. Track any upstream charge from an expired job separately from the customer's released hold.

After an outage, restart workers and let expiring leases become claimable. Run `credit-audit` after replay or restore. Reconcile database and bucket snapshots from the same recovery point, retain referenced assets, and clean unreferenced objects only after accounting for live jobs, database recovery retention, and bucket version history. Run `session-prune` periodically. Use the private test storage override only with `scripts/verify.sh s3`.
