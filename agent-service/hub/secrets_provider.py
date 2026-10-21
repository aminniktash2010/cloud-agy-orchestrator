"""Secret Manager API fetch with TTL (for MODEL_HUB_CONFIG hot-reload).

Uses the Secret Manager **API at runtime** via ADC — deliberately NOT
env-mounted, so config edits take effect without a redeploy (AD-4).
Implemented on top of ``google-api-python-client`` (already a dependency)
to avoid adding ``google-cloud-secret-manager`` to the slim container.

All google imports are lazy so unit tests never need google-auth installed.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("model-hub.secrets")

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def fetch_secret(secret_id: str, project_id: str, ttl_s: int = 60) -> str | None:
    """Returns the latest version payload of ``secret_id``, cached for ``ttl_s``.

    Returns None when ADC/secret is unavailable — callers fail closed to the
    baked default ladder (never crash the request path on a fetch failure).
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(secret_id)
        if hit and (now - hit[0]) < ttl_s:
            return hit[1]

    try:
        import google.auth
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        creds, default_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        project = project_id or default_project
        if not project:
            logger.warning("secret-manager fetch skipped: no project id")
            return None

        service = build("secretmanager", "v1", credentials=creds, cache_discovery=False)
        name = f"projects/{project}/secrets/{secret_id}/versions/latest"
        resp = service.projects().secrets().versions().access(name=name).execute()
        import base64

        payload = base64.b64decode(resp["payload"]["data"]).decode("utf-8")
    except ImportError as e:
        logger.warning("secret-manager fetch skipped (missing google libs): %s", e)
        return None
    except Exception as e:  # HttpError (404/403), ADC errors, network
        logger.warning("secret-manager fetch failed for %s: %s", secret_id, e)
        return None

    with _cache_lock:
        _cache[secret_id] = (now, payload)
    return payload


def clear_cache() -> None:
    """Test hook."""
    with _cache_lock:
        _cache.clear()
