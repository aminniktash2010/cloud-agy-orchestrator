# Cloud Antigravity Orchestrator (`cloud-agy-orchestrator`)

A **100% Cloud-Native, Zero-Local-Compute, $0.00/month Free-Tier** mobile & web application factory for `aminniktash2010`.

## Architecture Overview

```text
[Gemini Chat / Web PWA / GitHub Mobile]
      ↓ (HTTPS Webhook / Google Drive & Gmail Bridge)
[Google Cloud Run: agy-cloud-orchestrator] (~95MB Slim Container)
  • Tier 1: Slim Python/Git OS Layer in Artifact Registry (<100MB / 500MB free limit)
  • Tier 2: Hot 1GB In-Memory RAM Disk (`/workspace/<repo>`) with isolated `.agy_session` per repo
  • Tier 3: Persistent 5GB Free GCS Bucket (`/mnt/cache`) holding Node 20, Antigravity CLI, `stitch-loop`, and `.tar.zst` repo snapshots
  • Tier 4: 5TB Google Drive (`_AGY_Cloud_Vault`) storing live status reports (`latest_build_status.md`) for instant `@Google Drive` Gemini Chat inspection
      ↓ (Pushes generated code to GitHub)
[GitHub Actions Runner (`ubuntu-latest`)]
  • Stage 1 (`verify-and-stage-preview`): Runs `flutter analyze`, `flutter test` (with self-healing webhook on failure), builds `.aab` with auto-incremented `github.run_number`, and publishes a live Web Preview to GitHub Pages.
  • Stage 2 (`deploy-play-developer-test`): Waits at the `play-internal-test` 1-Tap Approval Gate, then deploys the signed `.aab` to Google Play Console's **Internal Developer Test Track** via keyless Workload Identity Federation (`play-upload@project-51459e90-fbb3-471f-9f9.iam.gserviceaccount.com`).
```

---

## Live Endpoints

- **Web Chat PWA:** [https://aminniktash2010.github.io/cloud-agy-orchestrator/](https://aminniktash2010.github.io/cloud-agy-orchestrator/)
- **Cloud Run Service:** `https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app`
- **Health Check:** `https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/health`
- **Agent Chat Endpoint:** `POST https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/chat` (Header: `x-webhook-token: <WEBHOOK_SECRET>`)

---

## Step-by-Step Implementation Guide

### Step 1: Create & Push This Repository
Create a new public repository named `cloud-agy-orchestrator` under `aminniktash2010` and push these files:

```bash
git init
git branch -M main
git add .
git commit -m "feat: initial cloud-agy-orchestrator infrastructure and agent service"
git remote add origin https://github.com/aminniktash2010/cloud-agy-orchestrator.git
git push -u origin main
```

### Step 2: Register Your Existing GCP WIF Variables on the Repo
Your GitHub account is already authorized at the account level for `github-actions-pool`. Register the variables (optional fallback defaults are already baked into the workflow):

```bash
REPO="aminniktash2010/cloud-agy-orchestrator"

gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER \
  --body "projects/202076903469/locations/global/workloadIdentityPools/github-actions-pool/providers/github-provider" \
  --repo "$REPO"

gh variable set GCP_SERVICE_ACCOUNT \
  --body "play-upload@project-51459e90-fbb3-471f-9f9.iam.gserviceaccount.com" \
  --repo "$REPO"
```

### Step 3: Populate Your 3 Secrets in Google Cloud Secret Manager
After the first GitHub Actions run (`deploy-agent-infra.yml`) provisions the infrastructure via Terraform:
1. Open [Google Cloud Secret Manager](https://console.cloud.google.com/security/secret-manager?project=project-51459e90-fbb3-471f-9f9).
2. Add a new version for each of the 3 secrets (and check **"Disable all previous versions"** to stay well under the 6-version free tier limit):
   - **`GEMINI_API_KEY`**: Your Google AI Studio Gemini API Key.
   - **`GITHUB_PAT`**: A Fine-Grained GitHub Personal Access Token for `aminniktash2010` with `Contents: Read & Write` and `Pull requests: Read & Write`.
   - **`WEBHOOK_SECRET`**: Any strong secret passphrase you choose to authenticate your chat requests.

### Step 4: Connect Your 5TB Google Drive (`_AGY_Cloud_Vault`)
1. In your 5TB Google Drive, create a folder named **`_AGY_Cloud_Vault`** and share it (Editor access) with:
   `play-upload@project-51459e90-fbb3-471f-9f9.iam.gserviceaccount.com`
2. Inside `_AGY_Cloud_Vault`, create an empty text/markdown file named **`latest_build_status.md`** (creating it from your personal account ensures it uses your 5TB quota).
3. Copy the file's ID from its Google Drive share link and set `drive_status_file_id` in `terraform/variables.tf`.

### Step 5: Add the Staged Developer Test Workflow to Your Target App Repos
Copy `target-app-template/.github/workflows/build-verify-release.yml` into your app repositories (e.g., `Atari-TV`, `River-Ride`, `PriceGuard-AI`):
1. In each target repository, go to **Settings > Environments > New environment** and create **`play-internal-test`**.
2. Check **Required reviewers** and add **`aminniktash2010`**.
3. Now every change pushed by Antigravity automatically verifies tests, publishes a live web staging preview to GitHub Pages, and sends a 1-tap approval request to your phone before uploading to the Google Play Console Internal Developer Test track!
