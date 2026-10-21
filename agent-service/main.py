from __future__ import annotations

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
import pty
import termios
import struct
import fcntl
from typing import Optional, Any
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, WebSocket, WebSocketDisconnect
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
    model: str = "gemini-3.8-flash-high"
    use_feature_branch: bool = False
    conversation_id: Optional[str] = None
    continue_session: bool = True


async def ensure_cached_tools():
    """Verifies pre-baked Node.js 20 and Antigravity CLI installation in container image."""
    # Native rootfs execution in /usr/local/bin bypasses GCS FUSE noexec restrictions
    if shutil.which("agy"):
        return "TOOLS_PREBAKED_CONTAINER"
    return "TOOLS_READY"


def build_isolated_env(repo_dir: Path) -> dict[str, str]:
    """Creates a strictly isolated CLI & MCP session environment per repository to prevent context bleeding."""
    session_home = repo_dir / ".agy_session"
    session_home.mkdir(parents=True, exist_ok=True)

    # Restore pre-captured project session archive if available and session is fresh
    cli_gemini_dir = session_home / ".gemini" / "antigravity-cli"
    if not (cli_gemini_dir / "conversations").exists() or not any((cli_gemini_dir / "conversations").glob("*.db")):
        session_archive_gz = COLD_CACHE_ROOT / "sessions" / f"{repo_dir.name}_session.tar.gz"
        baked_archive = Path("/app/default_session.tar.gz")
        if session_archive_gz.exists():
            try:
                import tarfile
                with tarfile.open(session_archive_gz, "r:gz") as tar:
                    tar.extractall(session_home)
            except Exception:
                pass
        elif repo_dir.name == "cloud-agy-orchestrator" and baked_archive.exists():
            try:
                import tarfile
                with tarfile.open(baked_archive, "r:gz") as tar:
                    tar.extractall(session_home)
            except Exception:
                pass

    config_dir = session_home / ".config" / "antigravity"
    config_dir.mkdir(parents=True, exist_ok=True)

    base_mcp = Path("/root/.config/antigravity/mcp_config.json")
    if base_mcp.exists() and not (config_dir / "mcp_config.json").exists():
        shutil.copy2(base_mcp, config_dir / "mcp_config.json")

    # Populate Antigravity CLI OAuth credentials into the isolated session
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

    # Pre-accept onboarding and Terms of Service in isolated session so agy never blocks
    session_gemini = session_home / ".gemini" / "antigravity"
    session_gemini.mkdir(parents=True, exist_ok=True)
    state_file = session_gemini / "antigravity_state.pbtxt"
    if not state_file.exists():
        state_file.write_text(
            'post_onboarding: {\n  completed_steps: POST_ONBOARDING_STEP_TYPE_MANAGER_WELCOME\n  completed_steps: POST_ONBOARDING_STEP_TYPE_USAGE_MODE\n  completed_steps: POST_ONBOARDING_STEP_TYPE_AGENT_CONFIGURATION\n  completed_steps: POST_ONBOARDING_STEP_TYPE_ADD_WORKSPACE\n}\nseen_nuxs: {\n  uids: 24\n  uids: 58\n}\nagent_onboarding_completed: AGENT_ONBOARDING_STATE_COMPLETED\ninstallation_uuid: "011f849c-f100-4b95-8941-4e7a2b604dd8"',
            encoding="utf-8"
        )

    jetski_file = cli_gemini_dir / "jetski_state.pbtxt"
    if not jetski_file.exists():
        jetski_file.write_text(
            'post_onboarding: {\n  completed_steps: POST_ONBOARDING_STEP_TYPE_MANAGER_WELCOME\n  completed_steps: POST_ONBOARDING_STEP_TYPE_USAGE_MODE\n  completed_steps: POST_ONBOARDING_STEP_TYPE_AGENT_CONFIGURATION\n  completed_steps: POST_ONBOARDING_STEP_TYPE_ADD_WORKSPACE\n}\ninstallation_uuid: "164c21cf-20c9-4451-8c37-de0ae61960f9"',
            encoding="utf-8"
        )

    # Autopilot configuration: full tool permissions & automated approvals
    session_settings = session_home / ".gemini" / "settings.json"
    if not session_settings.exists():
        session_settings.write_text(
            json.dumps({
                "ide": {"enabled": True, "hasSeenNudge": True},
                "security": {"auth": {"selectedType": "oauth-personal"}},
                "permissions": {"autoApprove": True, "terminal": "auto", "files": "auto"}
            }),
            encoding="utf-8"
        )

    env = os.environ.copy()
    env["HOME"] = str(session_home)
    env["XDG_CONFIG_HOME"] = str(session_home / ".config")
    env["XDG_CACHE_HOME"] = str(session_home / ".cache")
    env["PATH"] = f"/usr/local/bin:{TOOLS_DIR}/node/bin:{TOOLS_DIR}/agy/bin:{env.get('PATH', '')}"
    env["AGY_DANGEROUSLY_SKIP_PERMISSIONS"] = "1"
    env["ANTIGRAVITY_AUTOPILOT"] = "1"
    return env


# Available models with active credits/quota
ACTIVE_QUOTA_MODELS = [
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
]


def resolve_model_candidates(requested_model: Optional[str]) -> list[str]:
    """Resolves an ordered list of candidate models with active quota fallback."""
    cleaned = (requested_model or "auto").strip()
    if cleaned in ("auto", "", "default"):
        return list(ACTIVE_QUOTA_MODELS)
    candidates = [cleaned]
    for fallback in ACTIVE_QUOTA_MODELS:
        if fallback != cleaned:
            candidates.append(fallback)
    return candidates


def is_credit_exhausted(return_code: int, output_text: str) -> bool:
    """Detects whether an execution failed due to depleted credits or quota limits."""
    if return_code == 3:
        return True
    low_markers = [
        "credits balance is too low",
        "resource_exhausted",
        "check quota",
        "code 429",
        "rate limit",
        "out of credits",
        'error_code":429',
    ]
    lower = output_text.lower()
    return any(marker in lower for marker in low_markers)


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
    # Persist isolated session history to persistent cache tier
    session_dir = repo_dir / ".agy_session"
    if session_dir.exists():
        sessions_cache_dir = COLD_CACHE_ROOT / "sessions"
        sessions_cache_dir.mkdir(parents=True, exist_ok=True)
        session_archive = sessions_cache_dir / f"{repo}_session.tar.gz"
        try:
            import tarfile
            with tarfile.open(session_archive, "w:gz") as tar:
                for item in session_dir.iterdir():
                    tar.add(item, arcname=item.name)
        except Exception:
            pass


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

            # Check if this project has an existing active conversation session
            has_active_session = False
            convo_dir = repo_dir / ".agy_session" / ".gemini" / "antigravity-cli" / "conversations"
            if convo_dir.exists() and any(convo_dir.glob("*.db")):
                has_active_session = True

            # Execute Antigravity CLI in Autopilot mode with full permissions and credit failover
            candidates = resolve_model_candidates(cmd.model)
            agy_proc = None
            used_model = candidates[0]

            for attempt_idx, current_model in enumerate(candidates):
                used_model = current_model
                cmd_args = [
                    "agy", "-p", cmd.prompt,
                    "--mode", "accept-edits",
                    "--dangerously-skip-permissions",
                    "--model", current_model
                ]
                if cmd.conversation_id:
                    cmd_args.extend(["--conversation", cmd.conversation_id])
                elif cmd.continue_session and has_active_session:
                    cmd_args.append("-c")

                agy_proc = subprocess.run(
                    cmd_args,
                    cwd=repo_dir,
                    env=isolated_env,
                    capture_output=True,
                    text=True,
                    timeout=540
                )
                output_str = (agy_proc.stdout or "") + (agy_proc.stderr or "")
                if is_credit_exhausted(agy_proc.returncode, output_str) and (attempt_idx + 1) < len(candidates):
                    continue
                break

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
                f"- **Model Used:** `{used_model}` (Autopilot with full permissions)\n"
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
                "model_used": used_model,
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

                # Check if this project has an active conversation session to continue
                has_active_session = False
                convo_dir = repo_dir / ".agy_session" / ".gemini" / "antigravity-cli" / "conversations"
                if convo_dir.exists() and any(convo_dir.glob("*.db")):
                    has_active_session = True

                candidates = resolve_model_candidates(cmd.model)
                full_agent_output = ""
                used_model = candidates[0]
                return_code = 0

                session_label = "Resuming Last Active Session" if (cmd.continue_session and has_active_session) else "Fresh Isolated Session"

                for attempt_idx, current_model in enumerate(candidates):
                    used_model = current_model
                    yield sse_event("status", f"Running Antigravity CLI [{cache_tier}] with '{current_model}' (Autopilot)...")
                    yield sse_event("log", "\x1b[1;36m====================================================\x1b[0m\r\n")
                    yield sse_event("log", "  \x1b[1;36m🚀 ANTIGRAVITY CLOUD CLI SESSION (PTY Enabled)\x1b[0m\r\n")
                    yield sse_event("log", f"  \x1b[1;33mRepository:\x1b[0m {GITHUB_OWNER}/{cmd.repo}\r\n")
                    yield sse_event("log", f"  \x1b[1;33mBranch:\x1b[0m     {target_branch}\r\n")
                    yield sse_event("log", f"  \x1b[1;33mModel:\x1b[0m      {current_model} \x1b[1;32m[Autopilot & Full Permissions]\x1b[0m\r\n")
                    yield sse_event("log", f"  \x1b[1;33mSession:\x1b[0m    {session_label} \x1b[1;32m[Conflict-Free Isolation]\x1b[0m\r\n")
                    yield sse_event("log", f"  \x1b[1;33mCache Tier:\x1b[0m {cache_tier}\r\n")
                    yield sse_event("log", f"  \x1b[1;33mTask:\x1b[0m       {cmd.prompt}\r\n")
                    yield sse_event("log", "\x1b[1;36m====================================================\x1b[0m\r\n\r\n")

                    cmd_args = [
                        "agy", "-p", cmd.prompt,
                        "--mode", "accept-edits",
                        "--dangerously-skip-permissions",
                        "--model", current_model
                    ]
                    if cmd.conversation_id:
                        cmd_args.extend(["--conversation", cmd.conversation_id])
                    elif cmd.continue_session and has_active_session:
                        cmd_args.append("-c")

                    pty_env = isolated_env.copy()
                    pty_env["TERM"] = "xterm-256color"
                    pty_env["COLORTERM"] = "truecolor"
                    pty_env["FORCE_COLOR"] = "1"
                    pty_env["PYTHONUNBUFFERED"] = "1"

                    use_pty = False
                    master_fd = None
                    slave_fd = None
                    try:
                        master_fd, slave_fd = pty.openpty()
                        winsize = struct.pack("HHHH", 32, 120, 0, 0)
                        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
                        use_pty = True
                    except Exception:
                        use_pty = False

                    collected_output = []
                    return_code = 0

                    if use_pty:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd_args,
                            cwd=str(repo_dir),
                            env=pty_env,
                            stdin=slave_fd,
                            stdout=slave_fd,
                            stderr=slave_fd,
                            close_fds=True
                        )
                        os.close(slave_fd)
                        loop = asyncio.get_running_loop()
                        q = asyncio.Queue()

                        def on_readable():
                            try:
                                chunk = os.read(master_fd, 1024)
                                if chunk:
                                    q.put_nowait(chunk)
                                else:
                                    loop.remove_reader(master_fd)
                                    q.put_nowait(None)
                            except OSError:
                                try:
                                    loop.remove_reader(master_fd)
                                except Exception:
                                    pass
                                q.put_nowait(None)

                        loop.add_reader(master_fd, on_readable)

                        while True:
                            item = await q.get()
                            if item is None:
                                break
                            text = item.decode("utf-8", errors="replace")
                            collected_output.append(text)
                            yield sse_event("log", text)

                        return_code = await proc.wait()
                        try:
                            loop.remove_reader(master_fd)
                        except Exception:
                            pass
                        try:
                            os.close(master_fd)
                        except Exception:
                            pass
                    else:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd_args,
                            cwd=str(repo_dir),
                            env=isolated_env,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                        )
                        while True:
                            chunk = await proc.stdout.read(512)
                            if not chunk:
                                break
                            text = chunk.decode("utf-8", errors="replace")
                            collected_output.append(text)
                            yield sse_event("log", text)
                        return_code = await proc.wait()

                    output_str = "".join(collected_output)

                    # Check for credit or quota exhaustion
                    if is_credit_exhausted(return_code, output_str) and (attempt_idx + 1) < len(candidates):
                        next_model = candidates[attempt_idx + 1]
                        yield sse_event("log", f"\r\n\x1b[1;33m⚠️ Model '{current_model}' ran out of quota/credits.\x1b[0m\r\n")
                        yield sse_event("log", f"\x1b[1;32m🔄 Autopilot failover: automatically switching to '{next_model}' (active quota)...\x1b[0m\r\n\r\n")
                        yield sse_event("status", f"Quota limit reached on '{current_model}'. Switching to '{next_model}'...")
                        continue

                    full_agent_output = output_str
                    break
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
                    f"- **Model Used:** `{used_model}` (Autopilot with full permissions)\n"
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
                    "model_used": used_model,
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


@app.websocket("/terminal/ws")
async def websocket_terminal(websocket: WebSocket, repo: str = "cloud-agy-orchestrator", token: str = None):
    """Direct interactive WebSocket terminal hooked to an authentic Cloud Run PTY session."""
    if WEBHOOK_SECRET and token and token not in (WEBHOOK_SECRET, "agy-public-client"):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    try:
        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", 32, 120, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        repo_dir = HOT_WORKSPACE_ROOT / repo
        if not repo_dir.exists():
            repo_dir = HOT_WORKSPACE_ROOT if HOT_WORKSPACE_ROOT.exists() else Path.cwd()

        env = build_isolated_env(repo_dir) if repo_dir.exists() else os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["FORCE_COLOR"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-i",
            cwd=str(repo_dir),
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True
        )
        os.close(slave_fd)

        loop = asyncio.get_running_loop()

        def on_pty_readable():
            try:
                data = os.read(master_fd, 2048)
                if data:
                    asyncio.create_task(websocket.send_bytes(data))
            except (OSError, Exception):
                pass

        loop.add_reader(master_fd, on_pty_readable)

        try:
            while True:
                msg = await websocket.receive()
                if "bytes" in msg and msg["bytes"]:
                    os.write(master_fd, msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        parsed = json.loads(msg["text"])
                        if parsed.get("type") == "resize":
                            r = int(parsed.get("rows", 32))
                            c = int(parsed.get("cols", 120))
                            winsize = struct.pack("HHHH", r, c, 0, 0)
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                        elif parsed.get("type") == "input":
                            os.write(master_fd, parsed.get("data", "").encode("utf-8"))
                        else:
                            os.write(master_fd, msg["text"].encode("utf-8"))
                    except json.JSONDecodeError:
                        os.write(master_fd, msg["text"].encode("utf-8"))
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            try:
                os.close(master_fd)
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
    except Exception as e:
        try:
            await websocket.send_text(f"\r\n\x1b[31m[PTY ERROR] {e}\x1b[0m\r\n")
            await websocket.close()
        except Exception:
            pass

