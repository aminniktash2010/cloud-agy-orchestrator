from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shutil
import subprocess
import io
import zipfile
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
import pty
import termios
import struct
import fcntl
from typing import Optional, Any
from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Model Hub: priority-ordered provider failover chain (Epic 1)
from hub import ModelHub, load_config
from hub import observability as hub_observability
from hub.router import HubError
from hub.session_mcp import ensure_hub_mcp_entry
from hub.signals import AGY_QUOTA_DEATH_MARKERS, agy_exhausted, parse_reset_estimate
from hub.terminal_hubmode import (
    HubTermState,
    offer_banner,
    path_b_banner,
    run_hub_repl,
)
from hub.types import HubRequest, HubResult, RepoContext
from hub.mcp_server import handle_mcp, require_mcp_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-service")

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

# Model Hub singleton: one router owns the priority ladder for every entry point (AD-1)
model_hub = ModelHub()


@asynccontextmanager
async def _hub_lifespan(app: FastAPI):
    """Starts the supervised live-quota poller/probe (live-quota Epic C, AD-11).

    The poller runs the AGY CLI probe on a background task only — chat
    requests never wait on it. Graceful shutdown cancels the task.
    """
    from hub.quota_poller import start as start_quota_poller

    stop_poller = start_quota_poller(model_hub)
    try:
        yield
    finally:
        await stop_poller()


# The app object is constructed above (line ~40), before the hub singleton
# exists; attach the lifespan context here where both are in scope.
app.router.lifespan_context = _hub_lifespan


class ChatCommand(BaseModel):
    repo: str
    prompt: str
    branch: str = "main"
    # "auto" = unpinned ladder (the default). Any other value is a strict
    # cross-tier model pin (AD-29). (Was "gemini-3.8-flash-high", which only
    # ever re-ordered tier 0's first attempt — effectively the ladder anyway.)
    model: str = "auto"
    use_feature_branch: bool = False
    conversation_id: Optional[str] = None
    continue_session: bool = True
    allow_model_fallback: bool = True  # loud substitution when no carrier serves a pinned model (default on; explicit False restores strict model_unavailable)


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

    # Self-healing hub MCP registration (AD-61): deploys wipe container-local
    # session dirs, so re-assert the model-hub entry on every session setup.
    # Placed after the archive restore above (which can overwrite the session
    # dir) and next to the legacy MCP copy. Never fails session setup.
    try:
        hub_token = os.environ.get("HUB_MCP_TOKEN", "").strip()
        if hub_token:
            hub_url = (
                os.environ.get("HUB_PUBLIC_URL", "").strip()
                or "https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app"
            ).rstrip("/")
            ensure_hub_mcp_entry(
                session_home / ".gemini" / "config" / "mcp_config.json",
                hub_url,
                hub_token,
            )
        else:
            logger.warning("HUB_MCP_TOKEN unset — skipping model-hub MCP registration")
    except Exception as e:
        logger.warning("model-hub MCP registration failed (continuing): %s", e)

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


# Model selection & credit-failover now live in the Model Hub (hub/):
# hub.config.DEFAULT_LADDER + hub.router.ModelHub replace the code below.
# Exhaustion markers moved verbatim to hub.signals.agy_exhausted().


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
    public_url = f"https://github.com/{GITHUB_OWNER}/{repo}.git"
    auth_url = f"https://x-access-token:{GITHUB_PAT}@github.com/{GITHUB_OWNER}/{repo}.git" if GITHUB_PAT else public_url
    remote_url = auth_url
    cache_status = "COLD_CLONE"

    if (repo_dir / ".git").exists():
        cache_status = "WARM_RAM_HIT"
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir, check=False)
        try:
            subprocess.run(["git", "fetch", "--depth=1", "origin", branch], cwd=repo_dir, check=True)
            subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, check=True)
        except Exception:
            subprocess.run(["git", "remote", "set-url", "origin", public_url], cwd=repo_dir, check=False)
            subprocess.run(["git", "fetch", "--depth=1", "origin", branch], cwd=repo_dir, check=False)
        subprocess.run(["git", "clean", "-fd", "-e", ".agy_session"], cwd=repo_dir, check=False)

    elif archive_path.exists():
        cache_status = "GCS_SNAPSHOT_RESTORE"
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "--use-compress-program=zstd", "-xf", str(archive_path), "-C", str(repo_dir)], check=True)
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir, check=False)
        try:
            subprocess.run(["git", "fetch", "--depth=1", "origin", branch], cwd=repo_dir, check=True)
            subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, check=True)
        except Exception:
            subprocess.run(["git", "remote", "set-url", "origin", public_url], cwd=repo_dir, check=False)

    else:
        # Check if directory exists but lacks .git (e.g. from prior failed clone)
        if repo_dir.exists() and not (repo_dir / ".git").exists():
            temp_clone = Path(f"/tmp/clone_{repo}")
            if temp_clone.exists():
                shutil.rmtree(temp_clone, ignore_errors=True)
            try:
                subprocess.run(["git", "clone", "--depth=1", remote_url, str(temp_clone)], check=True)
            except Exception:
                subprocess.run(["git", "clone", "--depth=1", public_url, str(temp_clone)], check=True)
            for item in temp_clone.iterdir():
                dest = repo_dir / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
            shutil.rmtree(temp_clone, ignore_errors=True)
        else:
            try:
                subprocess.run(["git", "clone", "--depth=1", remote_url, str(repo_dir)], check=True)
            except Exception:
                subprocess.run(["git", "clone", "--depth=1", public_url, str(repo_dir)], check=True)

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


@app.get("/sessions/{repo}")
async def get_project_sessions(repo: str):
    """Returns all relevant Antigravity sessions for a specific project with isolation."""
    repo_clean = repo.strip()
    sessions = []
    seen_ids = set()

    possible_dbs = [
        HOT_WORKSPACE_ROOT / repo_clean / ".agy_session" / ".gemini" / "antigravity-cli" / "conversation_summaries.db",
        HOT_WORKSPACE_ROOT / repo_clean / ".agy_session" / ".gemini" / "antigravity" / "conversation_summaries.db",
        Path(f"/workspace/{repo_clean}/.agy_session/.gemini/antigravity-cli/conversation_summaries.db"),
        Path(f"/home/amin/cloud-agy-orchestrator/project-sessions/{repo_clean}/.agy_session/.gemini/antigravity-cli/conversation_summaries.db"),
        Path("/root/.gemini/antigravity/conversation_summaries.db"),
        Path("/root/.gemini/antigravity-cli/conversation_summaries.db"),
    ]

    for db_path in possible_dbs:
        if db_path.exists():
            try:
                con = sqlite3.connect(db_path)
                cur = con.cursor()
                cur.execute("PRAGMA table_info(conversation_summaries);")
                cols = [r[1] for r in cur.fetchall()]
                if "conversation_id" not in cols:
                    con.close()
                    continue

                select_cols = ["conversation_id", "title", "last_modified_time"]
                if "step_count" in cols:
                    select_cols.append("step_count")
                if "workspace_uris" in cols:
                    select_cols.append("workspace_uris")

                rows = cur.execute(f"SELECT {', '.join(select_cols)} FROM conversation_summaries ORDER BY last_modified_time DESC;").fetchall()
                con.close()

                for r in rows:
                    cid = r[0]
                    if cid in seen_ids:
                        continue
                    title = r[1] or "Active Development Session"
                    last_mod = r[2] or ""
                    step_count = r[3] if len(r) > 3 and isinstance(r[3], int) else 0
                    ws = r[4] if len(r) > 4 and isinstance(r[4], str) else ""

                    # Filter by workspace if reading from a global DB
                    if ws and repo_clean.lower() not in ws.lower() and ('-1' not in ws or repo_clean not in ws):
                        continue

                    seen_ids.add(cid)
                    sessions.append({
                        "conversation_id": cid,
                        "title": title,
                        "last_modified_time": last_mod,
                        "step_count": step_count,
                        "is_last_active": len(sessions) == 0,
                    })
            except Exception:
                pass

        if sessions:
            break

    # If no sessions in SQLite summaries, scan conversation DB files directly
    convo_dirs = [
        HOT_WORKSPACE_ROOT / repo_clean / ".agy_session" / ".gemini" / "antigravity-cli" / "conversations",
        Path(f"/workspace/{repo_clean}/.agy_session/.gemini/antigravity-cli/conversations"),
        Path(f"/home/amin/cloud-agy-orchestrator/project-sessions/{repo_clean}/.agy_session/.gemini/antigravity-cli/conversations"),
    ]
    for cdir in convo_dirs:
        if cdir.exists():
            for db_file in sorted(cdir.glob("*.db"), key=lambda f: f.stat().st_mtime, reverse=True):
                cid = db_file.stem
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    sessions.append({
                        "conversation_id": cid,
                        "title": f"Session {cid[:8]}",
                        "last_modified_time": datetime.datetime.fromtimestamp(db_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "step_count": 0,
                        "is_last_active": len(sessions) == 0,
                    })

    return {"repo": repo_clean, "count": len(sessions), "sessions": sessions}


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


@app.get("/hub/status")
async def hub_status(x_webhook_token: str = Header(None)):
    """Model Hub ladder status: tier order, key/config state, circuit breakers,
    quota usage. Never contains secrets (AC-10)."""
    # Public readability (same convention as /chat): the payload never contains
    # secrets (AC-10), only key-configured booleans and circuit/quota state.
    if x_webhook_token and WEBHOOK_SECRET and x_webhook_token not in (WEBHOOK_SECRET, "agy-public-client"):
        raise HTTPException(status_code=401, detail="Unauthorized webhook token")
    return model_hub.status()


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """Model Hub MCP server (Epic M1): stateless JSON-RPC 2.0 over Streamable HTTP.

    Tools: hub_ask (fallback-ladder answers, tier-0 skipped by default),
    hub_status (secret-free ladder state). Bearer auth is enforced BEFORE any
    MCP processing (AD-21).
    """
    if not os.environ.get("HUB_MCP_TOKEN", ""):
        # Graceful degradation: the secret doesn't exist yet (Amin creates it).
        # Fail closed with a clear operator signal, never crash.
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32603, "message": "MCP server not configured: HUB_MCP_TOKEN is not set"}},
            status_code=503,
        )
    require_mcp_token(request.headers.get("authorization"))  # 401 before anything else
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=200,
        )
    status_code, resp_body = await handle_mcp(body, model_hub)
    if resp_body is None:  # notification → HTTP 202, empty body
        return Response(status_code=202)
    return JSONResponse(resp_body, status_code=status_code)


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

            # Execute via the Model Hub: priority ladder with automatic failover (AD-1).
            # ChatCommand.model: "auto"/""/"default" = unpinned ladder; any other
            # value is a strict cross-tier model pin (AD-29). The legacy
            # requested_model_override is retired (kept in the type only).
            hub_request = HubRequest(
                prompt=cmd.prompt,
                repo=cmd.repo,
                branch=cmd.branch,
                use_feature_branch=cmd.use_feature_branch,
                conversation_id=cmd.conversation_id,
                continue_session=cmd.continue_session,
                repo_ctx=RepoContext(
                    repo_dir=str(repo_dir),
                    env=isolated_env,
                    has_active_session=has_active_session,
                    target_branch=target_branch,
                    cache_tier=cache_tier,
                ),
                model=(
                    None
                    if (cmd.model or "auto").strip().lower() in ("auto", "", "default")
                    else cmd.model.strip()
                ),
                allow_model_fallback=cmd.allow_model_fallback,
            )
            result = await model_hub.execute(hub_request)

            if result.ladder_empty:
                # FR-5.1: structured 429, never a bare 500 or a hallucinated answer
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "ladder_empty",
                        "message": "All model-hub tiers exhausted or unavailable.",
                        "attempts": [a.model_dump() for a in result.failover_attempts],
                    },
                )

            if result.status == "model_unavailable":
                # AD-34: 400 = unknown model name (client error); 422 = well-formed
                # pin no carrier could serve. Never confused with ladder_empty (429).
                code = (result.model_error or {}).get("code", "no_carrier")
                raise HTTPException(
                    status_code=400 if code == "no_match" else 422,
                    detail={
                        "error": "model_unavailable",
                        **(result.model_error or {}),
                        "attempts": [a.model_dump() for a in result.failover_attempts],
                    },
                )

            # Side effects happen once, after the winner, only for agent_loop (AD-6).
            # answer_only (advisory) tiers never mutate the repo.
            pushed = False
            commit_hash = ""
            if result.capability_mode == "agent_loop":
                subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
                diff_check = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)

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
            hub_cfg = load_config()
            report_md = (
                f"# Antigravity Cloud Build Report\n"
                f"- **Timestamp (UTC):** {datetime.datetime.utcnow().isoformat()}\n"
                f"- **Repository:** `{GITHUB_OWNER}/{cmd.repo}`\n"
                f"- **Branch:** `{target_branch}`\n"
                f"- **Commit:** `{commit_hash}`\n"
                f"- **Cache Tier:** `{cache_tier}`\n"
                f"- **Changes Pushed:** `{pushed}`\n\n"
                f"{hub_observability.drive_hub_block(result)}\n\n"
                f"## Prompt\n```\n{cmd.prompt}\n```\n\n"
                f"## Agent Output\n```\n{result.answer[-3000:]}\n```\n"
                f"{hub_observability.drive_footer(hub_cfg)}\n"
            )
            drive_sync_status = sync_status_to_5tb_drive(report_md)

            return {
                "status": "success",
                "repo": f"{GITHUB_OWNER}/{cmd.repo}",
                "branch": target_branch,
                "model_used": result.model,
                "commit": commit_hash,
                "cache_tier_used": cache_tier,
                "changes_pushed": pushed,
                "drive_sync": drive_sync_status,
                "agent_output": result.answer[-2000:],
                "serving_line": result.serving_line,
                "hub": {
                    "tier_used": result.tier_used,
                    "provider": result.provider,
                    "model": result.model,
                    "capability_mode": result.capability_mode,
                    "advisory_banner": result.advisory_banner,
                    "failover_attempts": [a.model_dump() for a in result.failover_attempts],
                },
            }

        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Command failed: {e.stderr or str(e)}")
        except HubError as e:
            raise HTTPException(status_code=500, detail=f"Model hub error: {e}")


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

                yield sse_event("status", "Model hub engaged: walking the priority ladder...")

                hub_request = HubRequest(
                    prompt=cmd.prompt,
                    repo=cmd.repo,
                    branch=cmd.branch,
                    use_feature_branch=cmd.use_feature_branch,
                    conversation_id=cmd.conversation_id,
                    continue_session=cmd.continue_session,
                    repo_ctx=RepoContext(
                        repo_dir=str(repo_dir),
                        env=isolated_env,
                        has_active_session=has_active_session,
                        target_branch=target_branch,
                        cache_tier=cache_tier,
                    ),
                    model=(
                        None
                        if (cmd.model or "auto").strip().lower() in ("auto", "", "default")
                        else cmd.model.strip()
                    ),
                    allow_model_fallback=cmd.allow_model_fallback,
                )

                final_result: HubResult | None = None
                async for hub_event in model_hub.execute_stream(hub_request):
                    if hub_event.kind == "log":
                        yield sse_event("log", hub_event.payload)
                    elif hub_event.kind == "status":
                        payload = hub_event.payload
                        yield sse_event(
                            "status",
                            payload if isinstance(payload, str) else json.dumps(payload),
                        )
                    elif hub_event.kind == "tier_switch":
                        # Live failover visibility (FR-4.2)
                        yield sse_event("tier_switch", hub_event.payload)
                        to = (hub_event.payload or {}).get("to", {})
                        reason = (hub_event.payload or {}).get("reason", "")
                        yield sse_event(
                            "status",
                            f"🔄 Hub failover ({reason}): now trying "
                            f"{to.get('provider', '?')} / {to.get('model', '?')} "
                            f"[{to.get('tier', '?')}]...",
                        )
                    elif hub_event.kind == "error":
                        yield sse_event("error", hub_event.payload)
                        return
                    elif hub_event.kind == "done":
                        final_result = HubResult.model_validate(hub_event.payload)

                if final_result is None:
                    yield sse_event("error", "Model hub produced no result.")
                    return

                if final_result.ladder_empty:
                    # FR-5.1: structured 429-style payload, never a bare 500
                    yield sse_event("done", {
                        "status": "ladder_empty",
                        "error": "ladder_empty",
                        "message": "All model-hub tiers exhausted or unavailable.",
                        "attempts": [a.model_dump() for a in final_result.failover_attempts],
                    })
                    return

                if final_result.status == "model_unavailable":
                    # AD-34: mirrors the collect handler's 400/422 mapping.
                    yield sse_event("done", {
                        "status": "model_unavailable",
                        "error": "model_unavailable",
                        **(final_result.model_error or {}),
                        "attempts": [a.model_dump() for a in final_result.failover_attempts],
                    })
                    return

                yield sse_event(
                    "status",
                    f"Hub finished: served by {final_result.provider} "
                    f"({final_result.model}) [{final_result.tier_used}]. "
                    "Inspecting working tree...",
                )

                # Side effects once, post-winner, only for agent_loop (AD-6)
                pushed = False
                commit_hash = ""
                if final_result.capability_mode == "agent_loop":
                    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
                    diff_check = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)

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
                hub_cfg = load_config()
                report_md = (
                    f"# Antigravity Cloud Build Report\n"
                    f"- **Timestamp (UTC):** {datetime.datetime.utcnow().isoformat()}\n"
                    f"- **Repository:** `{GITHUB_OWNER}/{cmd.repo}`\n"
                    f"- **Branch:** `{target_branch}`\n"
                    f"- **Commit:** `{commit_hash}`\n"
                    f"- **Cache Tier:** `{cache_tier}`\n"
                    f"- **Changes Pushed:** `{pushed}`\n\n"
                    f"{hub_observability.drive_hub_block(final_result)}\n\n"
                    f"## Prompt\n```\n{cmd.prompt}\n```\n\n"
                    f"## Agent Output\n```\n{final_result.answer[-3000:]}\n```\n"
                    f"{hub_observability.drive_footer(hub_cfg)}\n"
                )
                drive_sync_status = sync_status_to_5tb_drive(report_md)

                yield sse_event("done", {
                    "status": "success",
                    "repo": cmd.repo,
                    "github_repo": f"{GITHUB_OWNER}/{cmd.repo}",
                    "branch": target_branch,
                    "model_used": final_result.model,
                    "commit": commit_hash,
                    "changes_pushed": pushed,
                    "cache_tier": cache_tier,
                    "preview_url": preview_url,
                    "drive_sync": drive_sync_status,
                    "serving_line": final_result.serving_line,
                    "hub": {
                        "tier_used": final_result.tier_used,
                        "provider": final_result.provider,
                        "model": final_result.model,
                        "capability_mode": final_result.capability_mode,
                        "advisory_banner": final_result.advisory_banner,
                        "failover_attempts": [a.model_dump() for a in final_result.failover_attempts],
                    },
                })

            except HubError as e:
                yield sse_event("error", f"Model hub error: {e}")
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
async def websocket_terminal(websocket: WebSocket, repo: str = "cloud-agy-orchestrator", token: str = None, conversation_id: str = None):
    """Direct interactive WebSocket terminal hooked to an authentic Cloud Run PTY session."""
    # Hardened: a missing token must NOT short-circuit to accepted (was: anonymous
    # WebSocket shell on the public container). The PWA always sends a token.
    if token not in (WEBHOOK_SECRET, "agy-public-client"):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    try:
        repo_clean = (repo or "cloud-agy-orchestrator").strip()
        await websocket.send_text(f"\x1b[90mPreparing workspace for {repo_clean}...\x1b[0m\r\n")

        # Ensure repository exists and is isolated in /workspace/{repo_clean}
        try:
            repo_dir, cache_stat = prepare_cached_repo(repo_clean, "main")
            await websocket.send_text(f"\x1b[32m✔ Workspace ready [{cache_stat}]\x1b[0m\r\n")
        except Exception as e:
            logger.warning(f"prepare_cached_repo fallback for {repo_clean}: {e}")
            repo_dir = HOT_WORKSPACE_ROOT / repo_clean
            repo_dir.mkdir(parents=True, exist_ok=True)
            await websocket.send_text(f"\x1b[33m! Workspace initialized: {e}\x1b[0m\r\n")

        # --- Terminal hub-mode (Epic T1, AD-49): per-connection state machine ---
        # TUI -> ARMED (quota death seen) -> HUBMODE (user pressed Ctrl+H / /hub)
        state = HubTermState()

        # P4: set in the handler's finally. reap_repl bails when set so a
        # REPL finishing after disconnect can never orphan a TUI subprocess.
        handler_closed = False

        loop = asyncio.get_running_loop()
        pty_tail: list[bytes] = []

        # Mutable TUI incarnation (rebound on /exit respawn)
        tui_proc = None
        tui_master_fd = None
        tui_exit_watcher = None

        def maybe_arm_from_stream() -> None:
            """Live quota-death sniffing on the PTY tail (AD-48 §3.3)."""
            if state.phase != "tui" or state.banner_shown:
                return
            tail_text = b"".join(pty_tail).decode("utf-8", errors="replace")
            if any(m in tail_text.lower() for m in AGY_QUOTA_DEATH_MARKERS):
                state.reset_estimate = parse_reset_estimate(tail_text)
                state.phase = "armed"
                state.banner_shown = True
                # P13: guard the send like the Path B banner — a close-race
                # must not surface as a "never retrieved" warning.
                async def _send_offer() -> None:
                    try:
                        await websocket.send_text(
                            offer_banner(state.reset_estimate))
                    except Exception:
                        pass
                asyncio.create_task(_send_offer())

        async def spawn_tui():
            """(Re)spawn the agy TUI in a fresh PTY; wires reader + exit watcher."""
            nonlocal tui_proc, tui_master_fd, tui_exit_watcher
            master_fd, slave_fd = pty.openpty()
            try:
                winsize = struct.pack("HHHH", 32, 120, 0, 0)
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

                env = build_isolated_env(repo_dir)
                env["AGY_TARGET_REPO"] = repo_clean
                env["PWD"] = str(repo_dir)
                if conversation_id:
                    env["AGY_CONVERSATION_ID"] = conversation_id
                    if conversation_id == "new":
                        env["AGY_FORCE_NEW"] = "1"
                env["TERM"] = "xterm-256color"
                env["COLORTERM"] = "truecolor"
                env["FORCE_COLOR"] = "1"
                env["PYTHONUNBUFFERED"] = "1"

                def set_controlling_terminal():
                    try:
                        os.setsid()
                        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                    except Exception:
                        pass

                shell_bin = "/usr/local/bin/open-agy" if Path("/usr/local/bin/open-agy").exists() else "/bin/bash"
                proc_args = [shell_bin, repo_clean] if shell_bin == "/usr/local/bin/open-agy" else [shell_bin, "-i"]

                proc = await asyncio.create_subprocess_exec(
                    *proc_args,
                    cwd=str(repo_dir),
                    env=env,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=set_controlling_terminal,
                    close_fds=True
                )
            except Exception:
                # P16: never leak the PTY fds if the spawn itself fails.
                os.close(master_fd)
                os.close(slave_fd)
                raise
            os.close(slave_fd)
            pty_tail.clear()  # new incarnation: never sniff the old TUI's tail

            def on_pty_readable():
                try:
                    data = os.read(master_fd, 2048)
                except OSError:
                    return
                if data:
                    pty_tail.append(data)
                    del pty_tail[:-8]  # keep ~16KB of tail for the exhaustion check
                    maybe_arm_from_stream()
                    asyncio.create_task(websocket.send_bytes(data))

            loop.add_reader(master_fd, on_pty_readable)

            async def watch_proc_exit():
                # Path B: TUI exited on exhausted quota -> offer hub mode.
                # (The old "agy-only, use /chat" banner is superseded by hub mode.)
                rc = await proc.wait()
                try:
                    loop.remove_reader(master_fd)  # no EIO spin on the dead PTY
                except Exception:
                    pass
                tail = b"".join(pty_tail).decode("utf-8", errors="replace")
                if agy_exhausted(rc if rc is not None else -1, tail):
                    if state.phase == "tui" and not state.banner_shown:
                        state.reset_estimate = parse_reset_estimate(tail)
                        state.phase = "armed"
                        state.banner_shown = True
                        try:
                            await websocket.send_text(
                                path_b_banner(state.reset_estimate)
                            )
                        except Exception:
                            pass

            tui_proc, tui_master_fd = proc, master_fd
            tui_exit_watcher = asyncio.create_task(watch_proc_exit())

        async def park_tui():
            """Park the TUI: terminate -> 50ms -> kill. Idempotent (AD-51) —
            on an already-dead proc (Path B) this is a no-op chain."""
            nonlocal tui_proc, tui_master_fd, tui_exit_watcher
            if tui_exit_watcher is not None:
                tui_exit_watcher.cancel()
                tui_exit_watcher = None
            if tui_master_fd is not None:
                try:
                    loop.remove_reader(tui_master_fd)
                except Exception:
                    pass
                try:
                    os.close(tui_master_fd)
                except Exception:
                    pass
                tui_master_fd = None
            if tui_proc is not None:
                try:
                    tui_proc.terminate()
                    await asyncio.sleep(0.05)
                    if tui_proc.returncode is None:
                        tui_proc.kill()
                except Exception:
                    pass
                tui_proc = None

        hub_q: asyncio.Queue | None = None
        repl_task: asyncio.Task | None = None

        async def enter_hubmode():
            nonlocal hub_q, repl_task
            state.phase = "hubmode"
            await park_tui()
            hub_q = asyncio.Queue()
            repl_task = asyncio.create_task(
                run_hub_repl(
                    websocket.send_text, websocket.send_bytes,
                    hub_q, model_hub, state,
                )
            )
            # Eager reap: act on "respawn" the moment the REPL returns,
            # without waiting for the user's next keystroke.
            repl_task.add_done_callback(
                lambda t: asyncio.create_task(reap_repl()))

        async def reap_repl():
            """Act on a finished REPL: "respawn" -> TUI back."""
            nonlocal hub_q, repl_task
            if handler_closed:
                # P4: the websocket is gone — a respawn here would orphan a
                # TUI subprocess. Bail; the handler's finally already cleaned up.
                return
            if repl_task is None or not repl_task.done():
                return
            action = None
            crashed = False
            if not repl_task.cancelled():
                try:
                    action = repl_task.result()
                except Exception:
                    action = None
                    crashed = True
            repl_task, hub_q = None, None
            if crashed:
                # P3: the REPL died on a real exception — phase would stay
                # "hubmode" with hub_q=None and the receive loop would drop
                # input forever. Tear the connection down (guarded) so the
                # handler's finally cleans up instead of wedging.
                try:
                    await websocket.close()
                except Exception:
                    pass
                return
            if action == "respawn":
                try:
                    await spawn_tui()
                except Exception as e:
                    # P5: never wedge on respawn failure — stay usable in a
                    # fresh hub-mode REPL.
                    try:
                        await websocket.send_text(
                            f"\r\n⚠️ TUI respawn failed: {e} — staying in hub mode.\r\n"
                        )
                    except Exception:
                        pass
                    await enter_hubmode()
                    return
                state.phase = "tui"
                state.banner_shown = False
                state.armed_line.clear()
                # session_pin is kept (AD-56): harmless if hub mode re-enters.
                await websocket.send_text(
                    "\r\n\x1b[32m✔ agy TUI reconnected.\x1b[0m\r\n"
                )

        await spawn_tui()

        try:
            while True:
                await reap_repl()
                msg = await websocket.receive()
                data: bytes | None = None
                if "bytes" in msg and msg["bytes"]:
                    data = msg["bytes"]
                elif "text" in msg and msg["text"]:
                    try:
                        parsed = json.loads(msg["text"])
                        if parsed.get("type") == "resize":
                            r = int(parsed.get("rows", 32))
                            c = int(parsed.get("cols", 120))
                            winsize = struct.pack("HHHH", r, c, 0, 0)
                            if tui_master_fd is not None:
                                try:
                                    fcntl.ioctl(
                                        tui_master_fd, termios.TIOCSWINSZ, winsize
                                    )
                                except OSError:
                                    pass
                            continue
                        elif parsed.get("type") == "input":
                            data = parsed.get("data", "").encode("utf-8")
                        else:
                            data = msg["text"].encode("utf-8")
                    except json.JSONDecodeError:
                        data = msg["text"].encode("utf-8")
                if data is None:
                    continue
                if state.phase == "hubmode":
                    # The REPL owns input now; the PTY is parked.
                    if hub_q is not None:
                        hub_q.put_nowait(data)
                    await reap_repl()
                    continue
                if state.phase == "armed":
                    if b"\x08" in data:  # AD-50: Ctrl+H intercept (consumed)
                        # P14: don't drop the co-transmitted bytes — strip the
                        # \x08, forward the remainder (harmless: the TUI is
                        # being parked), then enter hub mode.
                        rest = data.replace(b"\x08", b"")
                        if rest and tui_master_fd is not None:
                            try:
                                os.write(tui_master_fd, rest)
                            except OSError:
                                pass
                        await enter_hubmode()
                        continue
                    state.armed_line += data
                    # P15: apply DEL erases so a corrected "/hub" line still
                    # matches (byte-level is enough — this is a command match).
                    cleaned = bytearray()
                    for b in state.armed_line:
                        if b == 0x7F:
                            if cleaned:
                                cleaned.pop()
                        else:
                            cleaned.append(b)
                    state.armed_line = cleaned
                    if b"\r" in state.armed_line or b"\n" in state.armed_line:
                        line = bytes(state.armed_line).decode(
                            "utf-8", errors="replace"
                        )
                        state.armed_line.clear()
                        if line.strip() == "/hub":
                            await enter_hubmode()
                            continue
                # TUI / ARMED (non-switch): forward to the PTY. Guarded so a
                # dead PTY (Path B) never kills the input loop (AD-49).
                if tui_master_fd is not None:
                    try:
                        os.write(tui_master_fd, data)
                    except OSError:
                        pass
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            handler_closed = True  # P4: reap_repl must never spawn after this
            if repl_task is not None:
                repl_task.cancel()
            if tui_exit_watcher is not None:
                tui_exit_watcher.cancel()
            if tui_master_fd is not None:
                try:
                    loop.remove_reader(tui_master_fd)
                except Exception:
                    pass
                try:
                    os.close(tui_master_fd)
                except Exception:
                    pass
            if tui_proc is not None:
                try:
                    tui_proc.terminate()
                    await asyncio.sleep(0.05)
                    if tui_proc.returncode is None:
                        tui_proc.kill()
                except Exception:
                    pass
    except Exception as e:
        try:
            await websocket.send_text(f"\r\n\x1b[31m[PTY ERROR] {e}\x1b[0m\r\n")
            await websocket.close()
        except Exception:
            pass

