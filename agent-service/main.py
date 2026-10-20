import asyncio
import datetime
import os
import shutil
import subprocess
import io
import zipfile
from collections import defaultdict
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
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
    """Bootstraps Node.js 20, Antigravity CLI, and Stitch-Loop onto the 5GB GCS bucket once."""
    async with TOOLS_LOCK:
        node_bin = TOOLS_DIR / "node" / "bin" / "node"
        if node_bin.exists():
            return "TOOLS_WARM_HIT"

        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        bootstrap_script = f"""
        set -e
        mkdir -p {TOOLS_DIR}/node
        curl -fsSL https://nodejs.org/dist/v20.18.0/node-v20.18.0-linux-x64.tar.xz | tar -xJ --strip-components=1 -C {TOOLS_DIR}/node
        export PATH="{TOOLS_DIR}/node/bin:$PATH"

        mkdir -p {TOOLS_DIR}/agy
        export HOME="{TOOLS_DIR}/agy"
        curl -fsSL https://antigravity.google/cli/install.sh | bash || true

        mkdir -p {TOOLS_DIR}/skills && cd {TOOLS_DIR}/skills
        npx --yes skills add https://github.com/google-labs-code/stitch-skills --skill stitch-loop || true
        """
        subprocess.run(["bash", "-c", bootstrap_script], check=True)
        return "TOOLS_BOOTSTRAPPED_TO_GCS"


def build_isolated_env(repo_dir: Path) -> dict[str, str]:
    """Creates a strictly isolated CLI & MCP session environment per repository to prevent context bleeding."""
    session_home = repo_dir / ".agy_session"
    config_dir = session_home / ".config" / "antigravity"
    config_dir.mkdir(parents=True, exist_ok=True)

    base_mcp = Path("/root/.config/antigravity/mcp_config.json")
    if base_mcp.exists() and not (config_dir / "mcp_config.json").exists():
        shutil.copy2(base_mcp, config_dir / "mcp_config.json")

    env = os.environ.copy()
    env["HOME"] = str(session_home)
    env["XDG_CONFIG_HOME"] = str(session_home / ".config")
    env["XDG_CACHE_HOME"] = str(session_home / ".cache")
    env["PATH"] = f"{TOOLS_DIR}/node/bin:{TOOLS_DIR}/agy/.antigravity/bin:{TOOLS_DIR}/agy/bin:{env.get('PATH', '')}"
    return env


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


def sync_status_to_5tb_drive(markdown_report: str):
    """Updates pre-created latest_build_status.md in your 5TB Google Drive (_AGY_Cloud_Vault)."""
    if not DRIVE_STATUS_FILE_ID:
        return "SKIPPED_NO_FILE_ID"
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
        drive_service = build("drive", "v3", credentials=creds)
        temp_report = Path("/tmp/latest_build_status.md")
        temp_report.write_text(markdown_report, encoding="utf-8")
        media = MediaFileUpload(str(temp_report), mimetype="text/markdown", resumable=True)
        drive_service.files().update(fileId=DRIVE_STATUS_FILE_ID, media_body=media).execute()
        return "SYNCED_TO_5TB_DRIVE"
    except Exception as e:
        return f"DRIVE_SYNC_WARNING: {e}"


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agy-cloud-orchestrator"}


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
    return {"status": "success", "repo": repo, "preview_url": preview_url}


@app.post("/chat")
async def execute_agent_task(cmd: ChatCommand, x_webhook_token: str = Header(None)):
    if WEBHOOK_SECRET and x_webhook_token != WEBHOOK_SECRET:
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

            # Execute Antigravity CLI inside isolated per-repo session
            agy_proc = subprocess.run(
                ["agy", "-p", cmd.prompt],
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
            if diff_check.stdout.strip():
                subprocess.run(
                    ["git", "commit", "-m", f"feat(agy): {cmd.prompt[:65]}"],
                    cwd=repo_dir,
                    check=True
                )
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
                "cache_tier_used": cache_tier,
                "changes_pushed": pushed,
                "drive_sync": drive_sync_status,
                "agent_output": agy_proc.stdout[-2000:],
            }

        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Command failed: {e.stderr or str(e)}")
