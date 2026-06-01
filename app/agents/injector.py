"""Agent 1 – Injector (bad engineer simulation).

Creates a Devin session that introduces one realistic flaw into the target
repository and merges it directly to master. This populates the repo with
something for Agent 2 to find.
"""

import logging
import uuid

from app import db, devin_client
from app.config import settings

logger = logging.getLogger(__name__)


INJECTOR_PROMPT_TEMPLATE = """You are simulating a careless software engineer working on the repository {repo}.

Your task is to introduce exactly ONE realistic code flaw into the codebase, then commit and push it directly to the `master` branch.

Focus area: {focus_area}

Guidelines:
- The flaw must be realistic — something a real developer might accidentally introduce.
- Choose a flaw type appropriate to the focus area. Examples by category:
  - security: hardcoded API key or password in source code, SQL injection via f-string, missing authentication check on an endpoint, use of MD5/SHA1 for password hashing
  - dependency: pin a package to an old, vulnerable version in requirements.txt or package.json
  - code-quality: introduce an N+1 database query, remove error handling so exceptions are silently swallowed, add dead code with an obvious logic bug
  - performance: add an unnecessary full table scan or O(n²) loop in a hot path
- Keep the change small (1–20 lines) so it is easy to spot and fix.
- Write a realistic-looking but vague commit message that does NOT reveal the flaw (e.g. "Update auth helper", "Bump dependency", "Refactor query builder").
- Push directly to `master` — do NOT open a pull request.
- After pushing, output a one-sentence summary of what you changed and where.

Do not explain or reveal the flaw in your response beyond the final summary.
"""


def run(focus_area: str) -> dict:
    """Trigger Agent 1. Returns the completed session dict from the DB."""
    run_id = str(uuid.uuid4())
    prompt = INJECTOR_PROMPT_TEMPLATE.format(
        repo=settings.target_repo,
        focus_area=focus_area,
    )
    tags = ["agent:injector", f"run:{run_id}"]

    logger.info("Injector starting | run_id=%s | focus=%s", run_id, focus_area)

    session_data = devin_client.create_session(
        prompt=prompt,
        tags=tags,
        repos=[settings.target_repo],
        max_acu_limit=settings.max_acu_injector,
        bypass_approval=True,
        title=f"[Injector] {focus_area[:60]}",
    )

    session_id = session_data["session_id"]
    devin_url = session_data["url"]

    db.upsert_session(
        session_id=session_id,
        role="injector",
        devin_url=devin_url,
        run_id=run_id,
        status="new",
    )

    # Poll to completion in a background thread (caller is already in a background task)
    final = devin_client.poll_until_done(session_id)

    pr_urls = devin_client.get_pr_urls(final)
    db.update_session(
        session_id=session_id,
        status=final.get("status", "error"),
        status_detail=final.get("status_detail"),
        pr_url=pr_urls[0] if pr_urls else None,
        acus_consumed=final.get("acus_consumed", 0.0),
    )

    success = devin_client.is_success(final)
    logger.info(
        "Injector session %s done | success=%s | acus=%.2f",
        session_id,
        success,
        final.get("acus_consumed", 0.0),
    )
    return final
