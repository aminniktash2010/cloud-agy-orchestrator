#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Google Play Console CLI Integration (tools/play_console_cli.py)
Supports:
  1. keygen     : Generate 4096-bit RSA PKCS12 upload keystores via keytool
  2. sign       : Sign .aab / .apk files (via jarsigner OR built-in pure-Python PKCS12/PKCS7 signer)
  3. verify-key : Validate Google Cloud Service Account JSON & test RS256 OAuth2 JWT signing
  4. status     : Query Google Play Android Publisher v3 API for active tracks & versionCodes
  5. deploy     : Upload signed .aab / .apk, attach release notes, and publish to Play Console tracks
"""

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
try:
    from cryptography.hazmat.primitives.serialization import pkcs12, pkcs7
except ImportError:
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
        pkcs7 = None
    except ImportError:
        pkcs12 = None
        pkcs7 = None

ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD_BASE = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def resolve_auth_token(key_path_or_env: Optional[str] = None) -> str:
    token = os.environ.get("PLAY_ACCESS_TOKEN")
    if token and token.strip():
        return token.strip()
    if key_path_or_env or os.environ.get("PLAY_SERVICE_ACCOUNT_JSON"):
        sa = load_service_account(key_path_or_env)
        return get_access_token(sa)
    # Automatic fallback: use gcloud IAM Credentials API impersonation for play-upload SA
    try:
        gcloud_bin = "/home/amin/google-cloud-sdk/bin/gcloud"
        if not os.path.exists(gcloud_bin):
            gcloud_bin = shutil.which("gcloud") or "gcloud"
        gcloud_token = subprocess.check_output(
            [gcloud_bin, "auth", "print-access-token"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode().strip()
        sa_email = "play-upload@project-51459e90-fbb3-471f-9f9.iam.gserviceaccount.com"
        iam_url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa_email}:generateAccessToken"
        resp = requests.post(
            iam_url,
            headers={"Authorization": f"Bearer {gcloud_token}", "Content-Type": "application/json"},
            json={"scope": ["https://www.googleapis.com/auth/androidpublisher"]},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()["accessToken"]
    except Exception:
        pass
    sa = load_service_account(key_path_or_env)
    return get_access_token(sa)


def load_service_account(key_path_or_env: Optional[str]) -> dict:
    raw = None
    if key_path_or_env and os.path.isfile(key_path_or_env):
        raw = pathlib.Path(key_path_or_env).read_text(encoding="utf-8")
    elif key_path_or_env and key_path_or_env.strip().startswith("{"):
        raw = key_path_or_env
    elif os.environ.get("PLAY_SERVICE_ACCOUNT_JSON"):
        env_val = os.environ["PLAY_SERVICE_ACCOUNT_JSON"].strip()
        if os.path.isfile(env_val):
            raw = pathlib.Path(env_val).read_text(encoding="utf-8")
        else:
            raw = env_val
    if not raw:
        raise RuntimeError(
            "No Service Account key provided. Pass --key /path/to/service-account.json "
            "or set PLAY_SERVICE_ACCOUNT_JSON or PLAY_ACCESS_TOKEN environment variable."
        )
    info = json.loads(raw)
    for field in ("client_email", "private_key", "token_uri"):
        if field not in info:
            raise ValueError(f"Service account JSON is missing required field: {field}")
    return info


def create_signed_jwt(sa_info: dict, scope: str = ANDROID_PUBLISHER_SCOPE) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if "private_key_id" in sa_info:
        header["kid"] = sa_info["private_key_id"]
    payload = {
        "iss": sa_info["client_email"],
        "scope": scope,
        "aud": sa_info.get("token_uri", "https://oauth2.googleapis.com/token"),
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
    private_key = serialization.load_pem_private_key(
        sa_info["private_key"].encode("utf-8"), password=None
    )
    signature = private_key.sign(
        signing_input.encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{signing_input}.{b64url(signature)}"


def get_access_token(sa_info: dict) -> str:
    assertion = create_signed_jwt(sa_info)
    token_uri = sa_info.get("token_uri", "https://oauth2.googleapis.com/token")
    resp = requests.post(
        token_uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def cmd_verify_key(args: argparse.Namespace) -> None:
    sa = load_service_account(args.key)
    jwt_token = create_signed_jwt(sa)
    print("=== Service Account Key Verified ===")
    print(f"Project ID   : {sa.get('project_id', 'N/A')}")
    print(f"Client Email : {sa['client_email']}")
    print(f"Key ID       : {sa.get('private_key_id', 'N/A')}")
    print(f"JWT Signed   : OK ({len(jwt_token)} bytes, RS256)")
    if args.online:
        token = get_access_token(sa)
        print(f"OAuth2 Token : Acquired successfully ({token[:12]}...)")


def cmd_keygen(args: argparse.Namespace) -> None:
    ks_path = pathlib.Path(args.keystore)
    if ks_path.exists() and not args.force:
        print(f"Error: {ks_path} already exists. Pass --force to overwrite.", file=sys.stderr)
        sys.exit(1)
    if ks_path.exists():
        ks_path.unlink()

    password = args.password or os.environ.get("STORE_PASSWORD")
    if not password:
        import secrets as _sec
        password = "Play_" + _sec.token_urlsafe(14)

    subprocess.run([
        "keytool", "-genkeypair", "-v",
        "-keystore", str(ks_path),
        "-storetype", "PKCS12",
        "-alias", args.alias,
        "-keyalg", "RSA",
        "-keysize", "4096",
        "-validity", str(args.validity),
        "-storepass", password,
        "-keypass", password,
        "-dname", args.dname,
    ], check=True)

    out = subprocess.check_output([
        "keytool", "-list", "-v",
        "-keystore", str(ks_path),
        "-storepass", password,
    ], text=True)

    sha1 = re.search(r"SHA1:\s*([0-9A-F:]+)", out)
    sha256 = re.search(r"SHA256:\s*([0-9A-F:]+)", out)
    print("\n=== Upload Keystore Generated ===")
    print(f"Keystore File : {ks_path}")
    print(f"Key Alias     : {args.alias}")
    print(f"Password      : {password}")
    print(f"SHA-1         : {sha1.group(1) if sha1 else 'N/A'}")
    print(f"SHA-256       : {sha256.group(1) if sha256 else 'N/A'}")


def sign_jar_pure_python(artifact: pathlib.Path, ks_path: pathlib.Path, password: str, alias: str) -> None:
    """Signs an AAB or APK (JAR/ZIP v1 signature) using Python cryptography PKCS12 + PKCS7."""
    p12_bytes = ks_path.read_bytes()
    priv_key, cert, add_certs = pkcs12.load_key_and_certificates(p12_bytes, password.encode("utf-8"))
    if priv_key is None or cert is None:
        raise RuntimeError(f"Could not load private key / certificate from {ks_path}")

    manifest_lines = [
        "Manifest-Version: 1.0",
        "Created-By: PlayConsoleCLI 1.0 (Atari-TV)",
        "",
    ]
    entry_sections = []

    with tempfile.NamedTemporaryFile(suffix=artifact.suffix, delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)

    try:
        with zipfile.ZipFile(artifact, "r") as zin, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename.upper().startswith("META-INF/"):
                    continue
                data = zin.read(item.filename)
                zout.writestr(item, data)
                digest_b64 = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
                section = f"Name: {item.filename}\r\nSHA-256-Digest: {digest_b64}\r\n\r\n"
                entry_sections.append(section)

            manifest_bytes = ("\r\n".join(manifest_lines) + "".join(entry_sections)).encode("utf-8")
            manifest_sha256 = base64.b64encode(hashlib.sha256(manifest_bytes).digest()).decode("ascii")

            sf_header = (
                "Signature-Version: 1.0\r\n"
                "Created-By: PlayConsoleCLI 1.0 (Atari-TV)\r\n"
                f"SHA-256-Digest-Manifest: {manifest_sha256}\r\n\r\n"
            )
            sf_sections = []
            for sec in entry_sections:
                sec_digest = base64.b64encode(hashlib.sha256(sec.encode("utf-8")).digest()).decode("ascii")
                name_line = sec.split("\r\n", 1)[0]
                sf_sections.append(f"{name_line}\r\nSHA-256-Digest: {sec_digest}\r\n\r\n")

            sf_bytes = (sf_header + "".join(sf_sections)).encode("utf-8")

            if pkcs7 is None:
                raise RuntimeError("Pure-python jar signing requires cryptography >= 3.1 or a system jarsigner binary.")
            builder = pkcs7.PKCS7SignatureBuilder().set_data(sf_bytes).add_signer(
                cert, priv_key, hashes.SHA256()
            )
            for extra in (add_certs or []):
                builder = builder.add_certificate(extra)

            rsa_der = builder.sign(
                serialization.Encoding.DER,
                [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary],
            )

            sf_name = re.sub(r"[^A-Z0-9_-]", "_", alias.upper())[:8] or "CERT"
            zout.writestr("META-INF/MANIFEST.MF", manifest_bytes)
            zout.writestr(f"META-INF/{sf_name}.SF", sf_bytes)
            zout.writestr(f"META-INF/{sf_name}.RSA", rsa_der)

        shutil.move(str(tmp_path), str(artifact))
        cert_fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
        cert_fp_colon = ":".join(cert_fp[i:i+2] for i in range(0, len(cert_fp), 2))
        print(f"Signed {artifact.name} (META-INF/{sf_name}.RSA, SHA256withRSA)")
        print(f"Signer Certificate SHA-256: {cert_fp_colon}")
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def cmd_sign(args: argparse.Namespace) -> None:
    artifact = pathlib.Path(args.artifact)
    if not artifact.exists():
        raise FileNotFoundError(f"Artifact not found: {artifact}")
    ks = pathlib.Path(args.keystore)
    if not ks.exists():
        raise FileNotFoundError(f"Keystore not found: {ks}")
    password = args.password or os.environ.get("STORE_PASSWORD")
    key_password = args.key_password or os.environ.get("KEY_PASSWORD") or password
    if not password:
        raise RuntimeError("Provide --password or set STORE_PASSWORD environment variable.")

    jarsigner_bin = shutil.which("jarsigner")
    if jarsigner_bin:
        subprocess.run([
            jarsigner_bin,
            "-verbose",
            "-sigalg", "SHA256withRSA",
            "-digestalg", "SHA-256",
            "-keystore", str(ks),
            "-storepass", password,
            "-keypass", key_password,
            str(artifact),
            args.alias,
        ], check=True)
        subprocess.run([jarsigner_bin, "-verify", "-verbose", "-certs", str(artifact)], check=True)
        print(f"Successfully signed and verified via jarsigner: {artifact}")
    else:
        sign_jar_pure_python(artifact, ks, password, args.alias)


def cmd_status(args: argparse.Namespace) -> None:
    token = resolve_auth_token(args.key)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    pkg = args.package

    try:
        edit_resp = requests.post(f"{API_BASE}/{pkg}/edits", headers=headers, json={}, timeout=30)
        edit_resp.raise_for_status()
        edit_id = edit_resp.json()["id"]

        tracks_resp = requests.get(f"{API_BASE}/{pkg}/edits/{edit_id}/tracks", headers=headers, timeout=30)
        print(f"=== Play Console Status for {pkg} ===")
        if tracks_resp.status_code == 200:
            print("\n[Tracks & Releases]")
            for tr in tracks_resp.json().get("tracks", []):
                track_name = tr.get("track")
                releases = tr.get("releases", [])
                if not releases:
                    print(f"- {track_name}: No active releases")
                for rel in releases:
                    print(f"- {track_name}: {rel.get('name', 'N/A')} (status: {rel.get('status')}, versionCodes: {rel.get('versionCodes')})")
        else:
            print(f"\n[Tracks & Releases] Warning: Play API returned HTTP {tracks_resp.status_code}")

        bundles_resp = requests.get(f"{API_BASE}/{pkg}/edits/{edit_id}/bundles", headers=headers, timeout=30)
        if bundles_resp.status_code == 200:
            print("\n[Uploaded App Bundles (AAB)]")
            bundles = bundles_resp.json().get("bundles", [])
            for b in bundles:
                print(f"- versionCode: {b.get('versionCode')} (sha256: {b.get('sha256')[:16]}...)")

        listings_resp = requests.get(f"{API_BASE}/{pkg}/edits/{edit_id}/listings", headers=headers, timeout=30)
        if listings_resp.status_code == 200:
            print("\n[Store Listings]")
            for item in listings_resp.json().get("listings", []):
                print(f"- [{item.get('language')}]: {item.get('title')}")

        requests.delete(f"{API_BASE}/{pkg}/edits/{edit_id}", headers=headers, timeout=30)
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 0
        err_msg = e.response.text if e.response is not None else str(e)
        if status_code == 404:
            print(f"Play Console status for '{pkg}': NOT FOUND (HTTP 404).")
            print(f"The app '{pkg}' has not been registered in Google Play Console yet.")
        else:
            print(f"Play Console API error (HTTP {status_code}): {err_msg}")
            raise


def cmd_deploy(args: argparse.Namespace) -> None:
    artifact = pathlib.Path(args.artifact)
    if not artifact.exists():
        raise FileNotFoundError(f"Artifact not found: {artifact}")

    token = resolve_auth_token(args.key)
    headers = {"Authorization": f"Bearer {token}"}
    pkg = args.package

    print(f"[1/4] Creating Play Console edit session for {pkg}...")
    edit_resp = requests.post(f"{API_BASE}/{pkg}/edits", headers=headers, json={}, timeout=30)
    edit_resp.raise_for_status()
    edit_id = edit_resp.json()["id"]

    is_aab = artifact.suffix.lower() == ".aab"
    endpoint = "bundles" if is_aab else "apks"
    mime = "application/octet-stream"
    print(f"[2/4] Uploading {artifact.name} ({artifact.stat().st_size} bytes) to /{endpoint}...")
    with artifact.open("rb") as fh:
        up_resp = requests.post(
            f"{UPLOAD_BASE}/{pkg}/edits/{edit_id}/{endpoint}?uploadType=media",
            headers={**headers, "Content-Type": mime},
            data=fh,
            timeout=300,
        )
    if not up_resp.ok:
        err_text = up_resp.text
        requests.delete(f"{API_BASE}/{pkg}/edits/{edit_id}", headers=headers, timeout=30)
        if up_resp.status_code == 403 and "signed with the wrong key" in err_text:
            try:
                err_msg = up_resp.json().get("error", {}).get("message", err_text)
            except Exception:
                err_msg = err_text
            print(f"\n[!] Google Play Console Upload Key Mismatch (HTTP 403):\n    {err_msg}")
            print("    -> Action Required: Submit 'ataritv_upload_certificate.pem' in Play Console:")
            print("       Release > Setup > App signing > Request upload key reset")
            if getattr(args, "allow_key_reset_pending", False):
                print("::warning::Play Console upload key reset is pending for com.mamali.ataritv. Skipping track commit until key reset completes.")
                return
        up_resp.raise_for_status()
    version_code = up_resp.json()["versionCode"]
    print(f"      Uploaded successfully -> versionCode: {version_code}")

    print(f"[3/4] Assigning versionCode {version_code} to track '{args.track}' (status: {args.status})...")
    release_body = {
        "versionCodes": [str(version_code)],
        "status": args.status,
    }
    if args.release_name:
        release_body["name"] = args.release_name
    if args.notes:
        release_body["releaseNotes"] = [{"language": args.lang, "text": args.notes}]

    track_resp = requests.put(
        f"{API_BASE}/{pkg}/edits/{edit_id}/tracks/{args.track}",
        headers={**headers, "Content-Type": "application/json"},
        json={"track": args.track, "releases": [release_body]},
        timeout=30,
    )
    track_resp.raise_for_status()

    print(f"[4/4] Committing edit {edit_id} on Google Play Console...")
    commit_resp = requests.post(
        f"{API_BASE}/{pkg}/edits/{edit_id}:commit",
        headers=headers,
        timeout=60,
    )
    commit_resp.raise_for_status()
    print(f"SUCCESS: {pkg} (versionCode {version_code}) deployed to '{args.track}' track!")


# ==============================================================================
# Google Drive <-> GitHub Actions CI/CD Bridge (drive-pull & drive-upload)
# ==============================================================================
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def get_drive_access_token(sa: dict) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claim = {
        "iss": sa["client_email"],
        "scope": DRIVE_SCOPE,
        "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"),
        "iat": now,
        "exp": now + 3600,
    }
    b64 = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    signing_input = f"{b64(header)}.{b64(claim)}".encode()
    priv_key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    signature = priv_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt_assertion = f"{signing_input.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    resp = requests.post(
        sa.get("token_uri", "https://oauth2.googleapis.com/token"),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt_assertion,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def cmd_drive_pull(args: argparse.Namespace) -> None:
    """Pulls the latest staged release bundle from Google Drive into the repo workspace."""
    sa = load_service_account(args.key)
    token = get_drive_access_token(sa)
    headers = {"Authorization": f"Bearer {token}"}
    q = f"name = '{args.bundle_name}' and trashed = false"
    resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={"q": q, "orderBy": "modifiedTime desc", "pageSize": 1, "fields": "files(id,name,modifiedTime)"},
        timeout=30,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    if not files:
        print(f"No staged bundle named '{args.bundle_name}' found in Google Drive.")
        return
    file_id = files[0]["id"]
    print(f"Downloading staged bundle {files[0]['name']} ({file_id}, modified {files[0]['modifiedTime']})...")
    dl_resp = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        headers=headers,
        params={"alt": "media"},
        timeout=120,
    )
    dl_resp.raise_for_status()
    out_zip = pathlib.Path(args.output)
    out_zip.write_bytes(dl_resp.content)
    if args.extract_to:
        out_dir = pathlib.Path(args.extract_to)
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_zip, "r") as zf:
            zf.extractall(out_dir)
        print(f"Extracted bundle into {out_dir}")


def cmd_drive_upload(args: argparse.Namespace) -> None:
    """Uploads compiled APK/AAB directly to Google Drive TV Hub (creates or updates by filename)."""
    sa = load_service_account(args.key)
    token = get_drive_access_token(sa)
    headers = {"Authorization": f"Bearer {token}"}
    artifact = pathlib.Path(args.file).expanduser().resolve()
    if not artifact.exists():
        raise FileNotFoundError(f"Artifact not found: {artifact}")
    target_name = args.name or artifact.name
    q_parts = [f"name = '{target_name}'", "trashed = false"]
    if args.folder_id:
        q_parts.append(f"'{args.folder_id}' in parents")
    q = " and ".join(q_parts)
    search_resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={"q": q, "pageSize": 1, "fields": "files(id,name,webViewLink)"},
        timeout=30,
    )
    search_resp.raise_for_status()
    existing = search_resp.json().get("files", [])
    data = artifact.read_bytes()

    if existing:
        file_id = existing[0]["id"]
        up_resp = requests.patch(
            f"https://www.googleapis.com/upload/drive/v3/files/{file_id}",
            headers={**headers, "Content-Type": "application/octet-stream"},
            params={"uploadType": "media", "fields": "id,name,webViewLink"},
            data=data,
            timeout=300,
        )
    else:
        boundary = f"===AMPtechDriveUpload_{int(time.time())}==="
        meta = {"name": target_name}
        if args.folder_id:
            meta["parents"] = [args.folder_id]
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        up_resp = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"},
            params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
            data=body,
            timeout=300,
        )
    up_resp.raise_for_status()
    print(json.dumps({"status": "UPLOADED_TO_DRIVE_HUB", "file": up_resp.json()}, indent=2))


def cmd_export_cert(args: argparse.Namespace) -> None:
    """Exports the X.509 upload certificate PEM from a PKCS12/JKS keystore for Play Console Upload Key Reset."""
    ks = pathlib.Path(args.keystore)
    if not ks.exists():
        raise FileNotFoundError(f"Keystore not found: {ks}")
    password = args.password or os.environ.get("STORE_PASSWORD")
    if not password:
        raise RuntimeError("Provide --password or set STORE_PASSWORD environment variable.")
    out_pem = pathlib.Path(args.output)
    keytool_bin = shutil.which("keytool")
    if keytool_bin:
        subprocess.run([
            keytool_bin, "-export", "-rfc",
            "-keystore", str(ks),
            "-alias", args.alias,
            "-storepass", password,
            "-file", str(out_pem),
        ], check=True)
    else:
        p12_bytes = ks.read_bytes()
        _, cert, _ = pkcs12.load_key_and_certificates(p12_bytes, password.encode("utf-8"), None)
        if cert is None:
            raise RuntimeError(f"No certificate found in {ks}")
        out_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Exported PEM certificate to: {out_pem}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Google Play Console & Google Drive TV Hub CI/CD CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    vk = sub.add_parser("verify-key", help="Validate Service Account JSON and test JWT signing")
    vk.add_argument("--key", help="Path to Service Account JSON (or uses PLAY_SERVICE_ACCOUNT_JSON)")
    vk.add_argument("--online", action="store_true", help="Also exchange JWT for live OAuth2 access token")
    vk.set_defaults(func=cmd_verify_key)

    kg = sub.add_parser("keygen", help="Generate a 4096-bit RSA upload keystore via keytool")
    kg.add_argument("--keystore", default="upload-keystore.jks")
    kg.add_argument("--alias", default="ataritv-upload")
    kg.add_argument("--password", help="Keystore password (auto-generated if omitted)")
    kg.add_argument("--validity", type=int, default=9125)
    kg.add_argument("--dname", default="CN=Atari TV, OU=AMPtech, O=AMPtech, L=Toronto, ST=ON, C=CA")
    kg.add_argument("--force", action="store_true")
    kg.set_defaults(func=cmd_keygen)

    sg = sub.add_parser("sign", help="Sign and verify an AAB or APK")
    sg.add_argument("--artifact", required=True, help="Path to .aab or .apk")
    sg.add_argument("--keystore", required=True, help="Path to .jks keystore")
    sg.add_argument("--alias", default="ataritv-upload")
    sg.add_argument("--password", help="Keystore password (or STORE_PASSWORD env)")
    sg.add_argument("--key-password", help="Key password (or KEY_PASSWORD env)")
    sg.set_defaults(func=cmd_sign)

    st = sub.add_parser("status", help="List active tracks and versionCodes on Play Console")
    st.add_argument("--package", default="com.mamali.ataritv")
    st.add_argument("--key", help="Path to Service Account JSON")
    st.set_defaults(func=cmd_status)

    dp = sub.add_parser("deploy", help="Upload and publish an AAB or APK to a Play Console track")
    dp.add_argument("--package", default="com.mamali.ataritv")
    dp.add_argument("--artifact", required=True, help="Path to signed .aab or .apk")
    dp.add_argument("--track", default="internal", choices=["internal", "alpha", "beta", "production"])
    dp.add_argument("--status", default="completed", choices=["completed", "draft", "halted", "inProgress"])
    dp.add_argument("--release-name", help="Optional release label (e.g. '1.1 (2)')")
    dp.add_argument("--notes", help="What's new release notes text")
    dp.add_argument("--lang", default="en-US", help="Release notes BCP-47 language tag")
    dp.add_argument("--key", help="Path to Service Account JSON")
    dp.add_argument("--allow-key-reset-pending", action="store_true", help="Exit 0 with warning if upload key reset is pending on Play Console")
    dp.set_defaults(func=cmd_deploy)

    ec = sub.add_parser("export-cert", help="Export PEM upload certificate from keystore for Play Console key reset")
    ec.add_argument("--keystore", required=True, help="Path to .jks keystore")
    ec.add_argument("--alias", default="ataritv-upload")
    ec.add_argument("--password", help="Keystore password (or STORE_PASSWORD env)")
    ec.add_argument("--output", default="ataritv_upload_certificate.pem", help="Output PEM file path")
    ec.set_defaults(func=cmd_export_cert)

    dpull = sub.add_parser("drive-pull", help="Pull staged release bundle from Google Drive")
    dpull.add_argument("--bundle-name", default="Atari-TV-v1.1-Release-Bundle.zip")
    dpull.add_argument("--output", default="/tmp/drive-bundle.zip")
    dpull.add_argument("--extract-to", default="/tmp/gemini-staged")
    dpull.add_argument("--key", help="Path to Service Account JSON")
    dpull.set_defaults(func=cmd_drive_pull)

    dupload = sub.add_parser("drive-upload", help="Upload built APK/AAB directly to Google Drive TV Hub")
    dupload.add_argument("--file", required=True, help="Path to built APK or AAB")
    dupload.add_argument("--name", default=None, help="Target filename in Google Drive (e.g. Atari-TV.apk)")
    dupload.add_argument("--folder-id", default=None, help="Optional Google Drive folder ID")
    dupload.add_argument("--key", help="Path to Service Account JSON")
    dupload.set_defaults(func=cmd_drive_upload)

    return p


if __name__ == "__main__":
    parser = build_parser()
    ns = parser.parse_args()
    ns.func(ns)
