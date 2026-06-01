"""Thin wrapper around the Devin v3 API.

All three agents call these helpers — create_session, get_session, poll_until_done.
"""

import logging
import time
from typing import Any, Optional

import requests

from app.config import settings

logger = logging.getLogger(__name__)

BASE = settings.devin_base_url
ORG = settings.devin_org_id

TERMINAL_STATUSES = {"exit", "error", "suspended"}
SUCCESS_STATUS = "exit"
SUCCESS_DETAIL = "finished"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.devin_api_key}",
        "Content-Type": "application/json",
    }


def create_session(
    prompt: str,
    tags: Optional[list[str]] = None,
    repos: Optional[list[str]] = None,
    structured_output_schema: Optional[dict[str, Any]] = None,
    structured_output_required: bool = False,
    max_acu_limit: Optional[int] = None,
    bypass_approval: bool = True,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Create a new Devin session and return the full response dict."""
    payload: dict[str, Any] = {
        "prompt": prompt,
        "bypass_approval": bypass_approval,
    }
    if tags:
        payload["tags"] = tags
    if repos:
        payload["repos"] = repos
    if structured_output_schema:
        payload["structured_output_schema"] = structured_output_schema
        payload["structured_output_required"] = structured_output_required
    if max_acu_limit:
        payload["max_acu_limit"] = max_acu_limit
    if title:
        payload["title"] = title

    url = f"{BASE}/organizations/{ORG}/sessions"
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    logger.info("Created session %s | url: %s", data["session_id"], data["url"])
    return data


def get_session(session_id: str) -> dict[str, Any]:
    """Fetch current session details."""
    url = f"{BASE}/organizations/{ORG}/sessions/{session_id}"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_sessions(tags: Optional[list[str]] = None, limit: int = 50) -> list[dict[str, Any]]:
    """List org sessions, optionally filtered by tags."""
    url = f"{BASE}/organizations/{ORG}/sessions"
    params: dict[str, Any] = {"limit": limit}
    resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or data.get("sessions") or []
    if tags:
        tag_set = set(tags)
        items = [s for s in items if tag_set.issubset(set(s.get("tags", [])))]
    return items


def poll_until_done(
    session_id: str,
    interval: Optional[int] = None,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    """Block until the session reaches a terminal state, then return full session data.

    Returns the session dict. Callers check session["status"] and session["status_detail"]
    to determine success vs failure.
    """
    poll_every = interval or settings.poll_interval_seconds
    max_wait = timeout or settings.poll_timeout_seconds
    deadline = time.time() + max_wait
    backoff = poll_every

    while True:
        session = get_session(session_id)
        status = session.get("status", "")
        detail = session.get("status_detail", "")
        logger.debug("Session %s | status=%s detail=%s", session_id, status, detail)

        if status in TERMINAL_STATUSES:
            if status == SUCCESS_STATUS and detail == SUCCESS_DETAIL:
                logger.info("Session %s finished successfully.", session_id)
            else:
                logger.warning(
                    "Session %s ended with status=%s detail=%s", session_id, status, detail
                )
            return session

        if time.time() > deadline:
            logger.error("Session %s timed out after %ds.", session_id, max_wait)
            return session

        time.sleep(backoff)
        # Mild backoff: cap at 60s
        backoff = min(backoff * 1.2, 60)


def is_success(session: dict[str, Any]) -> bool:
    """Return True if the session completed successfully."""
    return (
        session.get("status") == SUCCESS_STATUS
        and session.get("status_detail") == SUCCESS_DETAIL
    )


def get_pr_urls(session: dict[str, Any]) -> list[str]:
    """Extract all PR URLs from a completed session."""
    return [pr["pr_url"] for pr in session.get("pull_requests", []) if pr.get("pr_url")]
