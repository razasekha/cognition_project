"""Agent 3 – Fixer.

Creates a Devin session per issue that opens a pull request with the fix.
The session is given the full issue context including the linked GitHub issue.
"""

import logging
import uuid
from typing import Any, Optional

from app import db, devin_client
from app.config import settings

logger = logging.getLogger(__name__)


FIXER_PROMPT_TEMPLATE = """You are a senior software engineer working on the repository {repo}.

A code quality issue has been automatically detected and needs to be fixed. Your job is to fix it properly and open a pull request.

## Issue Details

**ID:** #{issue_id}
**Severity:** {severity}
**Category:** {category}
**File:** `{file}` (line ~{line})
**Description:** {description}
**Recommended Fix:** {recommendation}
{github_issue_section}

## Your Task

1. Clone the repository and check out `master`.
2. Understand the full context around the issue (read the file, related files, tests).
3. Implement the fix correctly:
   - Fix the specific problem described above.
   - Do not introduce new issues.
   - Follow the existing code style.
   - Add or update tests if there are existing tests for the affected code.
4. Commit your changes with a clear, descriptive commit message referencing the issue.
5. Open a pull request against `master` with:
   - A clear title: `Fix: {description_short}`
   - A description explaining what was wrong and how you fixed it
   - Reference to the GitHub issue (if available): `Closes #{github_issue_number}`

Do not merge the pull request — leave it open for human review.

When the pull request is open, your task is fully complete. Do NOT wait for a reply, ask follow-up questions, or request any confirmation. Stop immediately after confirming the PR URL.
"""

GITHUB_ISSUE_SECTION_TEMPLATE = """
**GitHub Issue:** {github_issue_url}
"""


def _extract_issue_number(github_issue_url: Optional[str]) -> Optional[str]:
    """Extract the issue number from a GitHub issue URL."""
    if not github_issue_url:
        return None
    try:
        return github_issue_url.rstrip("/").split("/")[-1]
    except Exception:
        return None


def run(issue_id: int) -> Optional[dict[str, Any]]:
    """Trigger Agent 3 for the given issue_id. Returns the completed session dict or None."""
    issue = db.get_issue(issue_id)
    if not issue:
        logger.error("Fixer: issue %d not found in database.", issue_id)
        return None

    if issue["status"] not in ("open", "failed"):
        logger.info(
            "Fixer: skipping issue %d with status=%s", issue_id, issue["status"]
        )
        return None

    run_id = str(uuid.uuid4())
    description_short = (issue["description"] or "")[:60]
    github_issue_url = issue.get("github_issue_url")
    github_issue_number = _extract_issue_number(github_issue_url)

    github_issue_section = ""
    if github_issue_url:
        github_issue_section = GITHUB_ISSUE_SECTION_TEMPLATE.format(
            github_issue_url=github_issue_url
        )

    prompt = FIXER_PROMPT_TEMPLATE.format(
        repo=settings.target_repo,
        issue_id=issue_id,
        severity=issue.get("severity", "unknown"),
        category=issue.get("category", "unknown"),
        file=issue.get("file") or "unknown",
        line=issue.get("line") or "unknown",
        description=issue.get("description", ""),
        recommendation=issue.get("recommendation") or "See description.",
        description_short=description_short,
        github_issue_section=github_issue_section,
        github_issue_number=github_issue_number or "",
    )

    tags = [
        "agent:fixer",
        f"run:{run_id}",
        f"issue:{issue_id}",
    ]

    logger.info("Fixer starting | issue_id=%d | run_id=%s", issue_id, run_id)

    session_data = devin_client.create_session(
        prompt=prompt,
        tags=tags,
        repos=[settings.target_repo],
        max_acu_limit=settings.max_acu_fixer,
        bypass_approval=True,
        title=f"[Fixer] issue#{issue_id}: {description_short[:40]}",
    )

    session_id = session_data["session_id"]
    devin_url = session_data["url"]

    db.upsert_session(
        session_id=session_id,
        role="fixer",
        devin_url=devin_url,
        run_id=run_id,
        issue_id=issue_id,
        status="new",
    )
    db.set_issue_fixing(issue_id, session_id)

    def _on_poll(s: dict) -> None:
        db.update_session(
            session_id=session_id,
            status=s.get("status", "new"),
            status_detail=s.get("status_detail"),
            pr_url=(devin_client.get_pr_urls(s) or [None])[0],
            acus_consumed=s.get("acus_consumed", 0.0),
        )

    final = devin_client.poll_until_done(session_id, on_poll=_on_poll)

    pr_urls = devin_client.get_pr_urls(final)
    pr_url = pr_urls[0] if pr_urls else None

    db.update_session(
        session_id=session_id,
        status=final.get("status", "error"),
        status_detail=final.get("status_detail"),
        pr_url=pr_url,
        acus_consumed=final.get("acus_consumed", 0.0),
    )

    if devin_client.is_done_ok(final) and pr_url:
        db.set_issue_fixed(issue_id, pr_url)
        logger.info(
            "Fixer session %s succeeded | issue=%d | pr=%s | acus=%.2f",
            session_id, issue_id, pr_url, final.get("acus_consumed", 0.0),
        )
    else:
        db.set_issue_failed(issue_id)
        logger.warning(
            "Fixer session %s failed | issue=%d | status=%s detail=%s",
            session_id, issue_id, final.get("status"), final.get("status_detail"),
        )

    return final
