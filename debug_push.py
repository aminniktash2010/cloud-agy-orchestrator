#!/usr/bin/env python3
"""
debug_push.py - Diagnostic tool to inspect Git push capabilities and GitHub PAT permissions
across Cloud Antigravity Orchestrator repositories.
"""

import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ANSI colors
GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
CYAN = "\033[1;36m"
BOLD = "\033[1m"
RESET = "\033[0m"

GITHUB_OWNER = "aminniktash2010"

# Target projects managed by orchestrator
PROJECTS = [
    {"repo": "cloud-agy-orchestrator", "type": "Orchestrator Core"},
    {"repo": "media-tracker", "type": "Web App / PWA"},
    {"repo": "AMPtech-Hub", "type": "Android App"},
    {"repo": "Atari-TV", "type": "Android TV App"},
    {"repo": "River-Ride", "type": "Game / Web"},
    {"repo": "price-tracker", "type": "Android / Web"},
    {"repo": "Mamali-IPTV", "type": "Android TV"},
    {"repo": "SpeakConfidentTV", "type": "Android TV"},
    {"repo": "Home-Painting-Marketplace-", "type": "Mobile App"},
    {"repo": "gcp-wif-foundation", "type": "Terraform / Infra"},
]

def extract_pat_from_git() -> str:
    """Extract PAT from git remote URL or environment."""
    pat = os.environ.get("GITHUB_PAT") or os.environ.get("GITHUB_TOKEN")
    if pat:
        return pat

    for repo_path in ["/workspace/cloud-agy-orchestrator", "/workspace/media-tracker"]:
        if os.path.exists(repo_path):
            try:
                res = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    check=True
                )
                url = res.stdout.strip()
                if "x-access-token:" in url:
                    token_part = url.split("x-access-token:")[1].split("@")[0]
                    return token_part
                elif "@github.com" in url and "https://" in url:
                    token_part = url.split("https://")[1].split("@github.com")[0]
                    if ":" in token_part:
                        token_part = token_part.split(":")[1]
                    return token_part
            except Exception:
                pass
    return ""

def api_get(endpoint: str, token: str):
    url = f"https://api.github.com{endpoint}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "cloud-agy-push-debugger"
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as e:
        try:
            err_data = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_data = {"message": str(e)}
        return e.code, err_data
    except Exception as e:
        return 0, {"message": str(e)}

def test_git_push_capability(repo_name: str, token: str) -> tuple[bool, str]:
    """Tests push capability by running git push --dry-run or testing git refs write API."""
    local_path = Path(f"/workspace/{repo_name}")
    if local_path.exists() and (local_path / ".git").exists():
        cmd = ["git", "push", "--dry-run", "origin", "HEAD"]
        proc = subprocess.run(cmd, cwd=local_path, capture_output=True, text=True)
        if proc.returncode == 0:
            return True, "Dry-run push succeeded"
        else:
            err_lines = proc.stderr.strip().split("\n")
            short_err = err_lines[-1] if err_lines else "Git error"
            return False, short_err

    # Test via API ref creation / deletion probe
    test_branch = "refs/heads/_debug_probe_temp"
    # First check if repo is accessible
    status, repo_info = api_get(f"/repos/{GITHUB_OWNER}/{repo_name}", token)
    if status == 404:
        return False, "404 Not Found (Token lacks repo visibility or repo is private)"
    if status != 200:
        return False, f"HTTP {status}: {repo_info.get('message', 'Error')}"

    # Try probe ref
    default_branch = repo_info.get("default_branch", "main")
    status, branch_info = api_get(f"/repos/{GITHUB_OWNER}/{repo_name}/branches/{default_branch}", token)
    if status != 200:
        return False, f"Could not read default branch: {branch_info.get('message')}"

    sha = branch_info["commit"]["sha"]
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{repo_name}/git/refs"
    req = urllib.request.Request(
        url,
        data=json.dumps({"ref": test_branch, "sha": sha}).encode("utf-8"),
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "cloud-agy-push-debugger"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            # Clean up probe ref immediately
            del_req = urllib.request.Request(
                f"{url}/heads/_debug_probe_temp",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "cloud-agy-push-debugger"
                },
                method="DELETE"
            )
            urllib.request.urlopen(del_req)
            return True, "Write & Push verified (probe ref created & deleted)"
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("message", str(e))
        except Exception:
            err = str(e)
        return False, f"HTTP {e.code}: {err}"

def main():
    print(f"\n{CYAN}{BOLD}=================================================================={RESET}")
    print(f"{CYAN}{BOLD}   🚀 CLOUD AGY ORCHESTRATOR - GIT PUSH & PAT PERMISSION DEBUGGER  {RESET}")
    print(f"{CYAN}{BOLD}=================================================================={RESET}\n")

    token = extract_pat_from_git()
    if not token:
        print(f"{RED}✕ No GitHub Personal Access Token found in environment or git remotes.{RESET}")
        sys.exit(1)

    masked_token = token[:12] + "..." + token[-6:] if len(token) > 20 else "***"
    token_type = "Fine-Grained PAT" if token.startswith("github_pat_") else "Classic PAT / OAuth"
    print(f"• {BOLD}Detected Token:{RESET} {masked_token} ({CYAN}{token_type}{RESET})")

    # Verify user
    status, user_data = api_get("/user", token)
    if status == 200:
        print(f"• {BOLD}Authenticated As:{RESET} {GREEN}{user_data.get('login')}{RESET} (ID: {user_data.get('id')})")
    else:
        print(f"{RED}✕ Token authentication failed (HTTP {status}): {user_data.get('message')}{RESET}")
        sys.exit(1)

    print(f"\n{BOLD}Testing Push & Repository Permissions across all 10 projects:{RESET}\n")
    print(f"{'Repository':<28} {'Type':<18} {'Status':<12} {'Details'}")
    print(f"{'-'*28} {'-'*18} {'-'*12} {'-'*30}")

    has_failures = False
    for proj in PROJECTS:
        name = proj["repo"]
        ptype = proj["type"]
        can_push, detail = test_git_push_capability(name, token)

        if can_push:
            status_str = f"{GREEN}✓ CAN PUSH{RESET}"
        else:
            status_str = f"{RED}✕ FAILED{RESET}"
            has_failures = True

        print(f"{name:<28} {ptype:<18} {status_str:<21} {detail}")

    print(f"\n{CYAN}{BOLD}=================================================================={RESET}")
    if has_failures:
        print(f"{YELLOW}{BOLD}⚠️ DIAGNOSIS & REQUIRED FIX:{RESET}")
        print(f"Your fine-grained token ({CYAN}github_pat_...{RESET}) does not have push permissions")
        print("for all repositories. To fix this:\n")
        print(f"1. Go to: {BOLD}https://github.com/settings/tokens{RESET}")
        print("2. Under 'Fine-grained tokens', click on this token.")
        print("3. Under 'Repository access':")
        print(f"   • Select {BOLD}'All repositories'{RESET} (recommended for orchestrator), OR")
        print(f"   • Under 'Only select repositories', add {BOLD}media-tracker{RESET} and all other target repos.")
        print("4. Under 'Repository permissions':")
        print(f"   • Set {BOLD}'Contents'{RESET} to {GREEN}'Read and write'{RESET}.")
        print(f"5. Click {BOLD}'Save changes'{RESET} at the bottom.")
    else:
        print(f"{GREEN}{BOLD}✓ ALL REPOSITORIES HAVE VALID PUSH PERMISSIONS!{RESET}")
    print(f"{CYAN}{BOLD}=================================================================={RESET}\n")

if __name__ == "__main__":
    main()
