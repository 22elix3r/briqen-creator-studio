# AI-use appendix

**Briqen Creator Studio · Backend & Product Engineering take-home**

I used OpenAI Codex for product research, architecture planning, implementation, debugging, verification, and editing the two-page submission. I used OpenAI image generation for the two fictional Northline Cold Brew assets in the simulated demo.

## Prompts

### 1. Turn the brief into a release plan

> Translate the Briqen brief into a two-page architecture submission and a focused implementation plan. Use a marketer making a short cold brew campaign to trace an AI-assisted draft, a manual revision, a product still, a pour video, and a re-ordered draft export. Read Briqen’s public product material for workflow context and identify where the brief leaves technical choices open. Propose what to ship first, what to defer, and two checks that would convince a reviewer the design works. Identify one assumption about provider behavior that would materially change the design.

**Applied.** The submission follows one two-scene project and prioritizes predictable recovery and a reproducible export. FastAPI, PostgreSQL, and the provider choices are implementation decisions. The architecture sketch and retry pseudocode are in the two-page submission.

### 2. Design durable records and the asynchronous API

> Define the durable records for projects, script revisions, scenes, assets, jobs, and workspace credits. Show which writes belong in the same transaction when the editor requests a paid generation. Design the client response for a request that may take a minute, including its status URL, idempotency key, and what happens if the client resends the same key with different inputs. Explain how an AI draft can complete after a manual edit without replacing that edit. Give workers a way to claim and recover jobs across crashes while keeping provider calls outside database transactions.

**Applied.** The API commits a PostgreSQL job and any credit hold before returning `202 Accepted` with a status URL. Revisions keep their parent IDs, and a provider result advances the current script only when its parent is still current. Workers use expiring leases to resume work.

### 3. Resolve a timeout without double charging

> Walk through a failure where scene 1 already has an accepted image and the provider starts scene 2’s video but loses its acknowledgement. Model the user pressing Retry twice, a worker losing its lease, and the original result arriving late. For each event, identify the job, the amount held or captured, and the media the user can see. Separate a provider with reliable deduplication from one with an ambiguous paid submission. Explain when to poll, when to wait for a verified callback, and when to release the hold. Trace a deliberate regeneration as a separate quoted action and show what changes if a result arrives after expiry.

**Applied.** Retry wakes the same job. The live adapter records a durable submission intent and reconciles an unknown acknowledgement through the original provider run or a verified callback. Publication captures the existing hold once. Failure or expiry releases it. Regeneration creates a new job, and its result stays a candidate until approved.

### 4. Make the cut and export reproducible

> Design the transition from generated media to an editable cut and downloadable MP4. Give each scene separate candidate and approved asset references so a paid variant can be previewed while the approved clip remains exportable. Define what must be validated when a user changes scene order, trim, or duration, including concurrent timeline edits. At export time, capture an immutable manifest that names the exact approved assets and timings the renderer will use. Walk through edits landing during a render and explain how the editor can tell a prior download from the current cut.

**Applied.** Timeline writes check the observed version. FFmpeg renders from the frozen manifest as a vertical H.264 MP4. The editor labels older renders as earlier cuts and makes exporting an updated cut the primary action when the saved timeline changes.

### 5. Plan a credible production deployment

> Review the runnable prototype for the boundaries that matter before operating it for multiple workspaces. Specify editor and viewer permissions, browser session handling, workspace-scoped media access, and what the API and worker should each be allowed to do in a private bucket. Describe how to verify provider callbacks and keep deployments recoverable through a schema upgrade. Design readiness checks and a few operator signals for stuck jobs, overdue credit holds, and delivery failures. Separate the checks that can run with simulated dependencies from those that need real provider models, storage credentials, and a public HTTPS origin.

**Applied.** The hardening pass added workspace isolation, scoped keys and sessions, private S3 media, a Replicate adapter, signed callbacks, migrations, and an operations runbook. The upgrade check preserved the existing demo project and balance. Live model behavior, AWS IAM, and webhook delivery are covered by the staging canary in `docs/operations.md`.

### 6. Turn backend states into an editor the marketer can understand

> Explore three distinct storyboard compositions on desktop and mobile. Make it clear which scene is selected, whether the preview is a candidate or approved asset, how many credits are involved, and why Retry can be safe during confirming. Test whether someone can approve the intended candidate and find the cut timing without losing their place. After selecting a direction, verify how the editor behaves when a user switches projects or clicks New project with unsaved script or timing edits. Include browser navigation and the case where a save finishes after the user has typed something newer.

**Applied.** I selected the warm editorial design. Codex implemented it and changed the approval label to **Accept candidate** after a browser walkthrough. I reported the draft-loss path in `selectProject()`. Codex reproduced it and added a discard confirmation, navigation protection, and safeguards for pending saves.

### 7. Verify the workflow and prepare the handoff

> Build a verification plan around observable outcomes. Use two workers and a deterministic provider to replay a lost acknowledgement, simultaneous retries, lease recovery, a duplicate callback, and expiry. Assert that the wallet and ledger agree, the accepted assets are preserved, and exports retain the inputs they captured. Inspect downloaded files for encoding, duration, and scene order. Run the end-to-end browser flow alongside local and S3-compatible CI. Finish by checking that the two-page submission explains the release boundary and that the appendix identifies which work was AI-assisted, which decisions I made, and what the tests actually exercised.

**Applied.** Codex ran the local and private S3-compatible integration profiles, inspected downloaded MP4s with FFprobe, and walked through the editor in the browser. Both CI jobs passed. Chrome and Firefox both exercised the draft-loss regression. [GitHub Actions run](https://github.com/22elix3r/briqen-creator-studio/actions/runs/44899487977)
