#!/usr/bin/env python3
"""
Captures Antigravity/Gemini conversation sessions from local system
and splits them into isolated, conflict-free per-project sessions.
"""
from __future__ import annotations

import sqlite3
import json
import shutil
import os
import tarfile
from pathlib import Path

# Paths on host
GEMINI_DIR = Path(os.path.expanduser("~/.gemini"))
AGY_IDE_DIR = GEMINI_DIR / "antigravity"
AGY_CLI_DIR = GEMINI_DIR / "antigravity-cli"

PROJECTS_ROOT = Path("/home/amin")
OUTPUT_SESSIONS_DIR = Path("/home/amin/cloud-agy-orchestrator/project-sessions")
OUTPUT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Map known repository path names
REPO_NORMALIZATION = {
    "Mamali-IPTV-1": "Mamali-IPTV",
    "media-tracker-github-pages": "media-tracker",
    "AMPtech-Hub": "tv-hub",
    "cloud-agy-orchestrator": "cloud-agy-orchestrator",
    "Atari-TV": "Atari-TV",
    "SpeakConfidentTV": "SpeakConfidentTV",
    "Home-Painting-Marketplace-": "Home-Painting-Marketplace-",
    "gcp-wif-foundation": "gcp-wif-foundation",
    "price-tracker": "price-tracker",
}


def capture_all_sessions():
    source_db = AGY_IDE_DIR / "conversation_summaries.db"
    if not source_db.exists():
        source_db = AGY_CLI_DIR / "conversation_summaries.db"

    if not source_db.exists():
        print(f"[ERROR] No conversation_summaries.db found in {AGY_IDE_DIR} or {AGY_CLI_DIR}")
        return

    con = sqlite3.connect(source_db)
    cur = con.cursor()

    # Get column names
    cur.execute("PRAGMA table_info(conversation_summaries);")
    cols = [r[1] for r in cur.fetchall()]
    cols_str = ", ".join(cols)
    placeholders = ", ".join(["?" for _ in cols])

    rows = cur.execute(f"SELECT {cols_str} FROM conversation_summaries ORDER BY last_modified_time DESC;").fetchall()
    print(f"Loaded {len(rows)} conversation sessions from {source_db}")

    # Group rows by repo
    repo_rows: dict[str, list[tuple]] = {}
    for row in rows:
        row_dict = dict(zip(cols, row))
        ws = row_dict.get("workspace_uris", "")
        uris = []
        try:
            uris = json.loads(ws) if ws else []
        except Exception:
            pass

        target_repo = "cloud-agy-orchestrator"
        for u in uris:
            raw_name = u.replace("file://", "").strip("/").split("/")[-1]
            target_repo = REPO_NORMALIZATION.get(raw_name, raw_name)
            break

        if target_repo not in repo_rows:
            repo_rows[target_repo] = []
        repo_rows[target_repo].append(row)

    oauth_token_file = AGY_CLI_DIR / "antigravity-oauth-token"
    token_bytes = oauth_token_file.read_bytes() if oauth_token_file.exists() else b""

    for repo_name, r_list in repo_rows.items():
        print(f"\nProcessing project: {repo_name} ({len(r_list)} sessions)...")
        session_root = OUTPUT_SESSIONS_DIR / repo_name / ".agy_session"
        cli_gemini = session_root / ".gemini" / "antigravity-cli"
        ide_gemini = session_root / ".gemini" / "antigravity"
        convos_cli = cli_gemini / "conversations"
        convos_ide = ide_gemini / "conversations"

        convos_cli.mkdir(parents=True, exist_ok=True)
        convos_ide.mkdir(parents=True, exist_ok=True)

        # Write project-isolated conversation_summaries.db
        for target_db in [cli_gemini / "conversation_summaries.db", ide_gemini / "conversation_summaries.db"]:
            if target_db.exists():
                target_db.unlink()
            dest_con = sqlite3.connect(target_db)
            dest_cur = dest_con.cursor()
            dest_cur.execute(f"CREATE TABLE conversation_summaries ({', '.join([f'{c} TEXT' if c not in ('step_count', 'nesting_depth', 'not_fully_idle', 'killed', 'last_user_input_step_index') else f'{c} INTEGER' for c in cols])});")
            dest_cur.executemany(f"INSERT INTO conversation_summaries ({cols_str}) VALUES ({placeholders});", r_list)
            dest_con.commit()
            dest_con.close()

        # Copy SQLite conversation DBs and brain logs
        copied_convos = 0
        last_active_id = None
        for row in r_list:
            cid = row[0]
            if not last_active_id:
                last_active_id = cid

            src_db = AGY_IDE_DIR / "conversations" / f"{cid}.db"
            if not src_db.exists():
                src_db = AGY_CLI_DIR / "conversations" / f"{cid}.db"

            if src_db.exists():
                shutil.copy2(src_db, convos_cli / f"{cid}.db")
                shutil.copy2(src_db, convos_ide / f"{cid}.db")
                copied_convos += 1

            # Copy brain artifacts if present
            src_brain = AGY_IDE_DIR / "brain" / cid
            if src_brain.exists():
                dst_brain = ide_gemini / "brain" / cid
                dst_brain.parent.mkdir(parents=True, exist_ok=True)
                if not dst_brain.exists():
                    shutil.copytree(src_brain, dst_brain, dirs_exist_ok=True)

        # Pre-accept onboarding and store OAuth token
        if token_bytes:
            (cli_gemini / "antigravity-oauth-token").write_bytes(token_bytes)

        state_content = (
            'post_onboarding: {\n  completed_steps: POST_ONBOARDING_STEP_TYPE_MANAGER_WELCOME\n'
            '  completed_steps: POST_ONBOARDING_STEP_TYPE_USAGE_MODE\n'
            '  completed_steps: POST_ONBOARDING_STEP_TYPE_AGENT_CONFIGURATION\n'
            '  completed_steps: POST_ONBOARDING_STEP_TYPE_ADD_WORKSPACE\n}\n'
            'agent_onboarding_completed: AGENT_ONBOARDING_STATE_COMPLETED\n'
            'installation_uuid: "011f849c-f100-4b95-8941-4e7a2b604dd8"\n'
        )
        (ide_gemini / "antigravity_state.pbtxt").write_text(state_content, encoding="utf-8")
        (cli_gemini / "jetski_state.pbtxt").write_text(state_content, encoding="utf-8")

        settings_json = json.dumps({
            "ide": {"enabled": True, "hasSeenNudge": True},
            "security": {"auth": {"selectedType": "oauth-personal"}},
            "permissions": {"autoApprove": True, "terminal": "auto", "files": "auto"}
        }, indent=2)
        (session_root / ".gemini" / "settings.json").write_text(settings_json, encoding="utf-8")
        (cli_gemini / "settings.json").write_text(settings_json, encoding="utf-8")

        if last_active_id:
            (session_root / "last_active_session_id.txt").write_text(last_active_id, encoding="utf-8")

        print(f"  -> Successfully created isolated session for {repo_name} with {copied_convos} active DBs. Last active: {last_active_id}")

        # Also package into compressed archive for persistent GCS cache tier
        archive_path = OUTPUT_SESSIONS_DIR / f"{repo_name}_session.tar.gz"
        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                for item in session_root.iterdir():
                    tar.add(item, arcname=item.name)
            print(f"  -> Packaged snapshot: {archive_path.name} ({archive_path.stat().st_size} bytes)")
        except Exception as e:
            print(f"  -> Note on archive packaging: {e}")

    # Also copy cloud-agy-orchestrator session directly into this workspace
    cloud_orch_session = OUTPUT_SESSIONS_DIR / "cloud-agy-orchestrator" / ".agy_session"
    if cloud_orch_session.exists():
        local_dest = Path("/home/amin/cloud-agy-orchestrator/.agy_session")
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cloud_orch_session, local_dest, dirs_exist_ok=True)
        print("\nCopied cloud-agy-orchestrator session into local .agy_session!")


if __name__ == "__main__":
    capture_all_sessions()
