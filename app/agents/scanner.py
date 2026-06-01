"""Agent 2 – Scanner.

Runs a Devin session with structured_output_schema so findings come back as
validated JSON. New findings are deduplicated into SQLite and a GitHub issue
is opened for each one.
"""

import logging
import uuid
from typing import Any, Optional

import requests

from app import db, devin_client
from app.config import settings
from app.schemas import SCANNER_OUTPUT_SCHEMA

logger = logging.getLogger(__name__)


SCANNER_PROMPT_TEMPLATE = """You are a senior security and code-quality engineer performing a code review on the repository {repo}.

Your task:
1. Clone / inspect the repository (branch: master).
2. Scan the codebase for real issues. Focus on these categories (but report anything you find):
   - **security**: hardcoded secrets, injection vulnerabilities, insecure hashing, missing auth
   - **dependency**: outdated or vulnerable pinned versions in requirements.txt / package.json
   - **code-quality**: N+1 queries, swallowed exceptions, dead code, obvious logic bugs
   - **performance**: unnecessary full scans, O(n²) in hot paths

3. For each issue found, populate the structured output schema with:
   - severity (low/medium/high/critical)
   - category
   - file (relative path)
   - line (approximate; 0 if unknown)
   - description (clear, specific)
   - recommendation (concrete fix guidance)

4. Limit your findings to the most actionable issues (max 10). Prefer high/critical.

5. After completing the scan, call `provide_structured_output` with `is_final=true` to submit your findings.

Important: populate the structured output EVEN IF you find no issues (return an empty `issues` array and a summary explaining the codebase is clean).
"""

GITHUB_ISSUE_TITLE_TEMPLATE = "[{severity}] {category}: {description_short}"
GITHUB_ISSUE_BODY_TEMPLATE = """## Issue detected by automated scan

**Severity:** {severity}
**Category:** {category}
**File:** `{file}` (line ~{line})

### Description
{description}

### Recommendation
{recommendation}

---
*Detected by the Devin Orchestrator automated scanner. Session: [{session_id}]({devin_url})*
"""


def _open_github_issue(
    title: str,
    body: str,
) -> Optional[str]:
    """Open a GitHub issue and return its URL, or None on failure."""
    if not settings.github_token:
        logger.warning("GITHUB_TOKEN not set — skipping GitHub issue creation.")
        return None

    url = f"https://api.github.com/repos/{settings.github_repo}/issues"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.post(url, headers=headers, json={"title": title, "body": body}, timeout=15)
    if resp.ok:
        issue_url = resp.json().get("html_url")
        logger.info("GitHub issue created: %s", issue_url)
        return issue_url
    else:
        logger.error("GitHub issue creation failed: %s %s", resp.status_code, resp.text[:200])
        return None


def run() -> tuple[list[int], dict[str, Any]]:
    """Run Agent 2. Returns (list of new issue IDs, completed session dict)."""
    run_id = str(uuid.uuid4())
    prompt = SCANNER_PROMPT_TEMPLATE.format(repo=settings.target_repo)
    tags = ["agent:scanner", f"run:{run_id}"]

    logger.info("Scanner starting | run_id=%s", run_id)

    session_data = devin_client.create_session(
        prompt=prompt,
        tags=tags,
        repos=[settings.target_repo],
        structured_output_schema=SCANNER_OUTPUT_SCHEMA,
        structured_output_required=True,
        max_acu_limit=settings.max_acu_scanner,
        bypass_approval=True,
        title=f"[Scanner] run:{run_id[:8]}",
    )

    session_id = session_data["session_id"]
    devin_url = session_data["url"]

    db.upsert_session(
        session_id=session_id,
        role="scanner",
        devin_url=devin_url,
        run_id=run_id,
        status="new",
    )

    final = devin_client.poll_until_done(session_id)
    db.update_session(
        session_id=session_id,
        status=final.get("status", "error"),
        status_detail=final.get("status_detail"),
        acus_consumed=final.get("acus_consumed", 0.0),
    )

    new_issue_ids: list[int] = []

    if not devin_client.is_success(final):
        logger.warning("Scanner session %s did not finish successfully.", session_id)
        return new_issue_ids, final

    structured = final.get("structured_output") or {}
    findings = structured.get("issues", [])
    logger.info("Scanner found %d issue(s) in session %s.", len(findings), session_id)

    for finding in findings:
        severity = finding.get("severity", "low")
        category = finding.get("category", "unknown")
        file_path = finding.get("file") or ""
        line = finding.get("line") or 0
        description = finding.get("description", "")
        recommendation = finding.get("recommendation") or ""

        issue_id = db.insert_issue_if_new(
            severity=severity,
            category=category,
            file=file_path,
            line=line,
            description=description,
            recommendation=recommendation,
            source_session_id=session_id,
        )

        if issue_id is None:
            logger.debug("Skipping duplicate finding: %s | %s | %s", file_path, category, line)
            continue

        # Open a GitHub issue for each new finding
        description_short = description[:80] + ("…" if len(description) > 80 else "")
        gh_title = GITHUB_ISSUE_TITLE_TEMPLATE.format(
            severity=severity.upper(),
            category=category,
            description_short=description_short,
        )
        gh_body = GITHUB_ISSUE_BODY_TEMPLATE.format(
            severity=severity,
            category=category,
            file=file_path or "unknown",
            line=line or "unknown",
            description=description,
            recommendation=recommendation or "See description.",
            session_id=session_id,
            devin_url=devin_url,
        )
        gh_url = _open_github_issue(gh_title, gh_body)
        if gh_url:
            db.set_issue_github_url(issue_id, gh_url)

        new_issue_ids.append(issue_id)
        logger.info("New issue logged: id=%d severity=%s file=%s", issue_id, severity, file_path)

    return new_issue_ids, final
