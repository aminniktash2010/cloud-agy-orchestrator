import asyncio
import datetime
import json
import os
import shutil
import subprocess
import io
import zipfile
from collections import defaultdict
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = FastAPI(title="Antigravity Cloud Orchestrator (Zero-Local-Compute)")

# Previews directory in persistent 5GB GCS cache (or local fallback)
PREVIEWS_DIR = Path("/mnt/cache/previews")
try:
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    PREVIEWS_DIR = Path("/tmp/previews")
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/preview", StaticFiles(directory=str(PREVIEWS_DIR), html=True), name="previews")

# Enable CORS so your GitHub Pages Chat PWA can call /chat directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DRIVE_STATUS_FILE_ID = os.environ.get("DRIVE_STATUS_FILE_ID", "")
GITHUB_OWNER = "aminniktash2010"
CLOUD_RUN_BASE_URL = "https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app"

ANTIGRAVITY_PROJECTS = [
    {
        "repo": "AMPtech-Hub",
        "platform": "Android Mobile / Hub (SDK 36)",
        "visibility": "Private",
        "description": "AMPtech Hub Android application configured for Google Play Console compliance (compileSdk/targetSdk 36).",
    },
    {
        "repo": "Atari-TV",
        "platform": "Android TV (Kotlin + C++ Stella 2023 NDK & LibretroDroid)",
        "visibility": "Private",
        "description": "10-foot Android TV Atari 2600 Console & ROM Shelf with CRT shader toggle, 5-slot state picker, and Play Console internal track pipeline.",
    },
    {
        "repo": "River-Ride",
        "platform": "Android & Android TV",
        "visibility": "Private",
        "description": "Classic River Raid arcade edition for Android & Android TV with automated Play Console internal test staging.",
    },
    {
        "repo": "price-tracker",
        "platform": "Android (Kotlin / Gradle KTS)",
        "visibility": "Private",
        "description": "PriceGuard / Price Tracker Android app with real-time price monitoring and Play Console WIF deployment.",
    },
    {
        "repo": "media-tracker",
        "platform": "Web + Flutter (iOS/Android/Web) + Vertex AI Cloud Run",
        "visibility": "Public",
        "description": "Curated 125+ cinema & prestige TV tracker with director filmography importer, Flutter cross-platform app, and Vertex AI Gemini curator (`media-tracker-ai`).",
    },
    {
        "repo": "Mamali-IPTV",
        "platform": "Android TV & Mobile (v3.1)",
        "visibility": "Private",
        "description": "Mamali IPTV player for Android TV & mobile devices deployed to Google Play Console.",
    },
    {
        "repo": "SpeakConfidentTV",
        "platform": "React Native / Android TV",
        "visibility": "Private",
        "description": "React Native speech confidence coaching & teleprompter application designed for Android TV.",
    },
    {
        "repo": "Home-Painting-Marketplace-",
        "platform": "Full-Stack Web Marketplace",
        "visibility": "Private",
        "description": "Home Painting Marketplace connecting homeowners and painting contractors with automated quoting.",
    },
    {
        "repo": "gcp-wif-foundation",
        "platform": "Terraform / GCP IAM & Workload Identity Federation",
        "visibility": "Private",
        "description": "Account-wide keyless GCP Workload Identity Federation (`github-actions-pool`) and service account foundation.",
    },
    {
        "repo": "cloud-agy-orchestrator",
        "platform": "Cloud Run + FastAPI + Terraform + GitHub Pages PWA",
        "visibility": "Public",
        "description": "Zero-Local-Compute Antigravity Cloud Orchestrator, Universal Web Preview Engine, and 5TB Google Drive Vault bridge.",
    },
]

HOT_WORKSPACE_ROOT = Path("/workspace")      # Tier 2: In-memory RAM disk (empty_dir)
COLD_CACHE_ROOT = Path("/mnt/cache/repos")   # Tier 3: Persistent 5GB GCS bucket
TOOLS_DIR = Path("/mnt/cache/tools")         # Persistent 5GB GCS tool directory

# Per-repository concurrency lock to prevent race conditions while allowing multi-repo parallelism
REPO_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
TOOLS_LOCK = asyncio.Lock()


class ChatCommand(BaseModel):
    repo: str
    prompt: str
    branch: str = "main"
    use_feature_branch: bool = False


async def ensure_cached_tools():
    """Verifies pre-baked Node.js 20 and Antigravity CLI installation in container image."""
    # Native rootfs execution in /usr/local/bin bypasses GCS FUSE noexec restrictions
    if shutil.which("agy"):
        return "TOOLS_PREBAKED_CONTAINER"
    return "TOOLS_READY"


def build_isolated_env(repo_dir: Path) -> dict[str, str]:
    """Creates a strictly isolated CLI & MCP session environment per repository to prevent context bleeding."""
    session_home = repo_dir / ".agy_session"
    config_dir = session_home / ".config" / "antigravity"
    config_dir.mkdir(parents=True, exist_ok=True)

    base_mcp = Path("/root/.config/antigravity/mcp_config.json")
    if base_mcp.exists() and not (config_dir / "mcp_config.json").exists():
        shutil.copy2(base_mcp, config_dir / "mcp_config.json")

    # Populate Antigravity CLI OAuth credentials into the isolated session
    cli_gemini_dir = session_home / ".gemini" / "antigravity-cli"
    cli_gemini_dir.mkdir(parents=True, exist_ok=True)
    target_token = cli_gemini_dir / "antigravity-oauth-token"

    oauth_cache = Path("/mnt/cache/antigravity-oauth-token")
    if oauth_cache.exists():
        shutil.copy2(oauth_cache, target_token)
    elif os.environ.get("AGY_OAUTH_TOKEN"):
        target_token.write_text(os.environ["AGY_OAUTH_TOKEN"], encoding="utf-8")

    # Also make token accessible to /root/.gemini if present
    root_cli_dir = Path("/root/.gemini/antigravity-cli")
    root_cli_dir.mkdir(parents=True, exist_ok=True)
    if target_token.exists() and not (root_cli_dir / "antigravity-oauth-token").exists():
        shutil.copy2(target_token, root_cli_dir / "antigravity-oauth-token")

    env = os.environ.copy()
    env["HOME"] = str(session_home)
    env["XDG_CONFIG_HOME"] = str(session_home / ".config")
    env["XDG_CACHE_HOME"] = str(session_home / ".cache")
    env["PATH"] = f"/usr/local/bin:{TOOLS_DIR}/node/bin:{TOOLS_DIR}/agy/bin:{env.get('PATH', '')}"
    return env


def sse_event(event_type: str, data: any) -> str:
    """Formats event payload as Server-Sent Event (SSE)."""
    return f"data: {json.dumps({'type': event_type, 'data': data})}\n\n"


def ensure_gitignore_excludes_session(repo_dir: Path):
    """Ensures .agy_session/ is never committed to GitHub while remaining inside the GCS snapshot."""
    gitignore = repo_dir / ".gitignore"
    marker = ".agy_session/"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if marker not in content:
            gitignore.write_text(content.rstrip() + f"\n{marker}\n", encoding="utf-8")
    else:
        gitignore.write_text(f"{marker}\n", encoding="utf-8")


def prepare_cached_repo(repo: str, branch: str) -> tuple[Path, str]:
    """Restores repo from RAM (Warm), GCS Snapshot (Cold), or GitHub Clone (First-ever run)."""
    HOT_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    COLD_CACHE_ROOT.mkdir(parents=True, exist_ok=True)

    repo_dir = HOT_WORKSPACE_ROOT / repo
    archive_path = COLD_CACHE_ROOT / f"{repo}.tar.zst"
    remote_url = f"https://x-access-token:{GITHUB_PAT}@github.com/{GITHUB_OWNER}/{repo}.git"
    cache_status = "COLD_CLONE"

    if (repo_dir / ".git").exists():
        cache_status = "WARM_RAM_HIT"
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir, check=True)
        subprocess.run(["git", "fetch", "--depth=1", "origin", branch], cwd=repo_dir, check=True)
        subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, check=True)
        subprocess.run(["git", "clean", "-fd", "-e", ".agy_session"], cwd=repo_dir, check=True)

    elif archive_path.exists():
        cache_status = "GCS_SNAPSHOT_RESTORE"
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "--use-compress-program=zstd", "-xf", str(archive_path), "-C", str(repo_dir)], check=True)
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir, check=True)
        subprocess.run(["git", "fetch", "--depth=1", "origin", branch], cwd=repo_dir, check=True)
        subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, check=True)

    else:
        subprocess.run(
            ["git", "clone", "--depth=1", "--branch", branch, remote_url, str(repo_dir)],
            check=True
        )

    # Copy pre-cached Stitch skills into repo if not already present
    baked_stitch = TOOLS_DIR / "skills" / ".stitch"
    target_stitch = repo_dir / ".stitch"
    if baked_stitch.exists() and not target_stitch.exists():
        shutil.copytree(baked_stitch, target_stitch, dirs_exist_ok=True)

    ensure_gitignore_excludes_session(repo_dir)
    return repo_dir, cache_status


def save_repo_snapshot(repo: str, repo_dir: Path):
    """Saves compressed snapshot (including isolated .agy_session) back to GCS bucket."""
    archive_path = COLD_CACHE_ROOT / f"{repo}.tar.zst"
    subprocess.run(
        ["tar", "--use-compress-program=zstd", "-cf", str(archive_path), "-C", str(repo_dir), "."],
        check=False
    )


def build_master_portfolio_markdown(latest_activity_md: str = "") -> str:
    """Builds the complete Antigravity Projects Catalog & Status Report for Gemini @Google Drive."""
    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# 🚀 Amin's Antigravity Projects Portfolio & Live Cloud Status",
        f"- **Last Synced:** `{now_str}`",
        f"- **GitHub Owner:** [`{GITHUB_OWNER}`](https://github.com/{GITHUB_OWNER})",
        f"- **Mobile/Web Control Center PWA:** [https://aminniktash2010.github.io/cloud-agy-orchestrator/](https://aminniktash2010.github.io/cloud-agy-orchestrator/)",
        f"- **Cloud Run Orchestrator:** `{CLOUD_RUN_BASE_URL}`",
        "",
        "## 📱 Active Antigravity Projects (10 Repositories)",
        "",
        "| # | Project / Repo | Platform & Stack | Visibility | Live Browser Preview | GitHub Repo |",
        "| :-: | :--- | :--- | :---: | :--- | :--- |",
    ]
    for idx, p in enumerate(ANTIGRAVITY_PROJECTS, start=1):
        repo = p["repo"]
        preview_url = f"{CLOUD_RUN_BASE_URL}/preview/{repo}/"
        gh_url = f"https://github.com/{GITHUB_OWNER}/{repo}"
        lines.append(
            f"| {idx} | **{repo}**<br>_{p['description']}_ | {p['platform']} | `{p['visibility']}` | [Open Preview]({preview_url}) | [`{GITHUB_OWNER}/{repo}`]({gh_url}) |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 💬 How to Command Any Project from Gemini App",
        "1. **Inspect Status in Gemini App:** Ask `@Google Drive What are my Antigravity projects in latest_build_status.md?`",
        "2. **Trigger Code Generation via Web Chat PWA:** Open [https://aminniktash2010.github.io/cloud-agy-orchestrator/](https://aminniktash2010.github.io/cloud-agy-orchestrator/), choose any of the 10 projects above, and enter your prompt.",
        "3. **Preview Before Play Console:** Open `https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/preview/<Project-Name>/` in your browser before approving the `play-internal-test` gate on GitHub Mobile.",
        "",
    ])

    if latest_activity_md:
        lines.extend([
            "---",
            "",
            latest_activity_md.strip(),
            "",
        ])
    return "\n".join(lines)


def sync_status_to_5tb_drive(markdown_report: str = ""):
    """Updates pre-created latest_build_status.md in your 5TB Google Drive (_AGY_Cloud_Vault)."""
    if not DRIVE_STATUS_FILE_ID:
        return "SKIPPED_NO_FILE_ID"
    try:
        full_doc = build_master_portfolio_markdown(markdown_report)
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
        drive_service = build("drive", "v3", credentials=creds)
        temp_report = Path("/tmp/latest_build_status.md")
        temp_report.write_text(full_doc, encoding="utf-8")
        media = MediaFileUpload(str(temp_report), mimetype="text/markdown", resumable=True)
        drive_service.files().update(fileId=DRIVE_STATUS_FILE_ID, media_body=media).execute()
        return "SYNCED_TO_5TB_DRIVE"
    except Exception as e:
        return f"DRIVE_SYNC_WARNING: {e}"


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agy-cloud-orchestrator", "projects_count": len(ANTIGRAVITY_PROJECTS)}


@app.get("/projects")
async def list_projects():
    """Returns all Antigravity projects with their live preview URLs and metadata."""
    enriched = []
    for p in ANTIGRAVITY_PROJECTS:
        repo = p["repo"]
        preview_index = PREVIEWS_DIR / repo / "index.html"
        enriched.append({
            **p,
            "github_url": f"https://github.com/{GITHUB_OWNER}/{repo}",
            "preview_url": f"{CLOUD_RUN_BASE_URL}/preview/{repo}/",
            "preview_ready": preview_index.exists(),
        })
    return {"owner": GITHUB_OWNER, "count": len(enriched), "projects": enriched}


@app.post("/sync-drive")
async def sync_drive_catalog(x_webhook_token: str = Header(None)):
    """Syncs the full 10-project Antigravity portfolio to Google Drive (_AGY_Cloud_Vault/latest_build_status.md)."""
    if WEBHOOK_SECRET and x_webhook_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized webhook token")
    status = sync_status_to_5tb_drive()
    return {"status": status, "drive_file_id": DRIVE_STATUS_FILE_ID, "projects_synced": len(ANTIGRAVITY_PROJECTS)}


@app.post("/upload-preview/{repo}")
async def upload_preview(repo: str, file: UploadFile = File(...), x_webhook_token: str = Header(None)):
    """Receives a zipped web preview from CI and serves it at /preview/{repo}/."""
    if WEBHOOK_SECRET and x_webhook_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized webhook token")

    repo_dir = PREVIEWS_DIR / repo
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        z.extractall(repo_dir)

    preview_url = f"https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/preview/{repo}/"
    drive_sync = sync_status_to_5tb_drive(
        f"## 🌐 Latest Preview Staged\n- **Repository:** `{GITHUB_OWNER}/{repo}`\n- **Live Preview URL:** [{preview_url}]({preview_url})"
    )
    return {"status": "success", "repo": repo, "preview_url": preview_url, "drive_sync": drive_sync}


class TokenSyncRequest(BaseModel):
    token: str


@app.post("/sync-token")
async def sync_token(req: TokenSyncRequest, x_webhook_token: str = Header(None)):
    """Receives and stores Antigravity OAuth credentials onto persistent GCS cache."""
    if WEBHOOK_SECRET and x_webhook_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized webhook token")
    target = Path("/mnt/cache/antigravity-oauth-token")
    target.write_text(req.token, encoding="utf-8")
    return {"status": "success", "message": "Antigravity OAuth token cached in persistent GCS."}


@app.post("/chat")
async def execute_agent_task(cmd: ChatCommand, x_webhook_token: str = Header(None)):
    # Allow passwordless execution from the official Web Chat PWA & Android Play Console app
    if x_webhook_token and WEBHOOK_SECRET and x_webhook_token not in (WEBHOOK_SECRET, "agy-public-client"):
        raise HTTPException(status_code=401, detail="Unauthorized webhook token")

    await ensure_cached_tools()

    # Acquire per-repo lock so overlapping prompts for the same repo queue cleanly
    async with REPO_LOCKS[cmd.repo]:
        try:
            repo_dir, cache_tier = prepare_cached_repo(cmd.repo, cmd.branch)
            isolated_env = build_isolated_env(repo_dir)

            target_branch = cmd.branch
            if cmd.use_feature_branch:
                ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                target_branch = f"agy/task-{ts}"
                subprocess.run(["git", "checkout", "-b", target_branch], cwd=repo_dir, check=True)

            # Execute Antigravity CLI inside isolated per-repo session with auto-approved permissions
            agy_proc = subprocess.run(
                ["agy", "-p", cmd.prompt, "--dangerously-skip-permissions"],
                cwd=repo_dir,
                env=isolated_env,
                capture_output=True,
                text=True,
                timeout=540
            )

            # Stage, commit, and push changes
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
            diff_check = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)

            pushed = False
            commit_hash = ""
            if diff_check.stdout.strip():
                commit_msg = f"feat(agy): {cmd.prompt[:65]}"
                subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    cwd=repo_dir,
                    check=True
                )
                rev_parse = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
                commit_hash = rev_parse.stdout.strip()
                subprocess.run(["git", "push", "-u", "origin", target_branch], cwd=repo_dir, check=True)
                pushed = True

            # Persist updated repo + isolated .agy_session to 5GB GCS snapshot
            save_repo_snapshot(cmd.repo, repo_dir)

            # Sync summary report to 5TB Google Drive for instant Gemini @Google Drive inspection
            report_md = (
                f"# Antigravity Cloud Build Report\n"
                f"- **Timestamp (UTC):** {datetime.datetime.utcnow().isoformat()}\n"
                f"- **Repository:** `{GITHUB_OWNER}/{cmd.repo}`\n"
                f"- **Branch:** `{target_branch}`\n"
                f"- **Commit:** `{commit_hash}`\n"
                f"- **Cache Tier:** `{cache_tier}`\n"
                f"- **Changes Pushed:** `{pushed}`\n\n"
                f"## Prompt\n```\n{cmd.prompt}\n```\n\n"
                f"## Agent Output\n```\n{agy_proc.stdout[-3000:]}\n```\n"
            )
            drive_sync_status = sync_status_to_5tb_drive(report_md)

            return {
                "status": "success",
                "repo": f"{GITHUB_OWNER}/{cmd.repo}",
                "branch": target_branch,
                "commit": commit_hash,
                "cache_tier_used": cache_tier,
                "changes_pushed": pushed,
                "drive_sync": drive_sync_status,
                "agent_output": agy_proc.stdout[-2000:],
            }

        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Command failed: {e.stderr or str(e)}")


@app.post("/chat/stream")
async def execute_agent_task_stream(cmd: ChatCommand, x_webhook_token: str = Header(None)):
    """Real-time SSE streaming endpoint: delivers live Antigravity CLI output & steps to the browser."""
    if x_webhook_token and WEBHOOK_SECRET and x_webhook_token not in (WEBHOOK_SECRET, "agy-public-client"):
        raise HTTPException(status_code=401, detail="Unauthorized webhook token")

    async def event_generator():
        yield sse_event("status", f"Connecting to Cloud Run orchestrator for '{cmd.repo}'...")
        yield sse_event("status", f"Waiting for repository lock on '{cmd.repo}'...")

        async with REPO_LOCKS[cmd.repo]:
            try:
                yield sse_event("status", "Restoring workspace & checking cache tier...")
                repo_dir, cache_tier = prepare_cached_repo(cmd.repo, cmd.branch)
                isolated_env = build_isolated_env(repo_dir)

                target_branch = cmd.branch
                if cmd.use_feature_branch:
                    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                    target_branch = f"agy/task-{ts}"
                    subprocess.run(["git", "checkout", "-b", target_branch], cwd=repo_dir, check=True)
                    yield sse_event("status", f"Created feature branch: {target_branch}")

                yield sse_event("status", f"Running Antigravity CLI [{cache_tier}]...")
                yield sse_event("log", f"====================================================\n")
                yield sse_event("log", f"  ANTIGRAVITY CLOUD CLI SESSION\n")
                yield sse_event("log", f"  Repository: {GITHUB_OWNER}/{cmd.repo}\n")
                yield sse_event("log", f"  Branch:     {target_branch}\n")
                yield sse_event("log", f"  Cache Tier: {cache_tier}\n")
                yield sse_event("log", f"  Task:       {cmd.prompt}\n")
                yield sse_event("log", f"====================================================\n\n")

                cmd_args = ["agy", "-p", cmd.prompt, "--dangerously-skip-permissions"]
                proc = await asyncio.create_subprocess_exec(
                    *cmd_args,
                    cwd=str(repo_dir),
                    env=isolated_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                collected_output = []
                while True:
                    chunk = await proc.stdout.read(512)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    collected_output.append(text)
                    yield sse_event("log", text)

                return_code = await proc.wait()
                full_agent_output = "".join(collected_output)
                yield sse_event("status", f"CLI exited (code {return_code}). Inspecting working tree...")

                # Stage changes
                subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
                diff_check = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)

                pushed = False
                commit_hash = ""
                if diff_check.stdout.strip():
                    commit_msg = f"feat(agy): {cmd.prompt[:65]}"
                    subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=True)
                    rev_parse = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
                    commit_hash = rev_parse.stdout.strip()
                    yield sse_event("status", f"Committed: {commit_hash[:7]} - {commit_msg}")
                    yield sse_event("status", f"Pushing to origin/{target_branch}...")
                    subprocess.run(["git", "push", "-u", "origin", target_branch], cwd=repo_dir, check=True)
                    pushed = True
                    yield sse_event("status", f"Successfully pushed commit {commit_hash[:7]} to GitHub!")
                else:
                    yield sse_event("status", "Working tree clean; no code changes generated.")

                # Persist snapshot to GCS
                yield sse_event("status", "Persisting workspace snapshot to GCS cache...")
                save_repo_snapshot(cmd.repo, repo_dir)

                # Sync build report to Drive
                preview_url = f"{CLOUD_RUN_BASE_URL}/preview/{cmd.repo}/"
                report_md = (
                    f"# Antigravity Cloud Build Report\n"
                    f"- **Timestamp (UTC):** {datetime.datetime.utcnow().isoformat()}\n"
                    f"- **Repository:** `{GITHUB_OWNER}/{cmd.repo}`\n"
                    f"- **Branch:** `{target_branch}`\n"
                    f"- **Commit:** `{commit_hash}`\n"
                    f"- **Cache Tier:** `{cache_tier}`\n"
                    f"- **Changes Pushed:** `{pushed}`\n\n"
                    f"## Prompt\n```\n{cmd.prompt}\n```\n\n"
                    f"## Agent Output\n```\n{full_agent_output[-3000:]}\n```\n"
                )
                drive_sync_status = sync_status_to_5tb_drive(report_md)

                yield sse_event("done", {
                    "status": "success",
                    "repo": cmd.repo,
                    "github_repo": f"{GITHUB_OWNER}/{cmd.repo}",
                    "branch": target_branch,
                    "commit": commit_hash,
                    "changes_pushed": pushed,
                    "cache_tier": cache_tier,
                    "preview_url": preview_url,
                    "drive_sync": drive_sync_status
                })

            except Exception as e:
                yield sse_event("error", str(e))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
