# Creator Studio

A short-form video workspace built for the Briqen Backend & Product Engineering take-home. A marketer can turn a brief into a script, revise it, generate one candidate per scene, approve a cut, and download a rendered MP4. The application preserves approved media during recovery and records one credit charge per completed paid action.

[Two-page take-home](output/pdf/Creator_Studio_Architecture.pdf) · [Architecture and API](docs/architecture.md) · [Deployment and operations](docs/operations.md) · [Briqen and role research](RESEARCH.md)

## Run the local demo

Docker with Compose is the only prerequisite.

```sh
docker compose up --build -d --wait
```

Open [Creator Studio](http://127.0.0.1:8765/) or the [interactive API reference](http://127.0.0.1:8765/docs). The local workspace starts with 100 credits. The fixture provider returns reproducible scripts and media. FFmpeg renders real, silent, vertical H.264 MP4s.

1. Create the prefilled cold brew project. Draft, edit, and save a revision. Split it into a product still and a pour video.
2. Generate and accept the still. Select **Late result after timeout** for the video and click **Retry status** when it appears. The video arrives roughly 14 seconds after submission. **Inspect job** shows the original provider run and its credit hold or charge.
3. Accept the video. Request a separate **Regenerate** if you want to compare another candidate. The cut keeps the approved clip until you replace it.
4. Move the video first and save a six-second segment followed by a four-second still. Open **Export**, render the cut, and download the MP4. Each export has its own immutable manifest.

Use `docker compose down` to stop the stack while retaining the demo database and media. Change `STUDIO_PORT` for another local port.

## Run the checks

```sh
scripts/verify.sh local
scripts/verify.sh s3
```

Each command starts two workers and an isolated PostgreSQL database. The S3 profile adds a private MinIO bucket and exercises workspace access keys, browser session revocation, role enforcement, tenant isolation, and conditional uploads. Both profiles check concurrent retries, expired leases, callback deduplication, wallet reconciliation, and the actual contents and durations of rendered MP4s. The provider contract tests cover a lost submission response without a second paid POST.

## Deploy

The production configuration uses a TLS origin, scoped workspace credentials, PostgreSQL, private S3 storage, and Replicate for script, image, and video generation. Start with the [production runbook](docs/operations.md) and [.env template](.env.production.example). Run the staging canary with your chosen models, bucket, credentials, and webhook origin before serving a workspace.

```text
studio/api.py         HTTP API, editor sessions and media grants
studio/workflow.py    Project, timeline, job and credit transactions
studio/worker.py      Job leases, provider reconciliation and export
studio/replicate.py   Live provider transport and signed callbacks
studio/media.py       Immutable storage and bounded media rendering
studio/migrations/    Ordered, checksummed PostgreSQL migrations
tests/                Reliability, tenant isolation and storage checks
```
