"""Agent 1 – Injector (bad engineer simulation).

Creates a Devin session that introduces one realistic flaw, opens a PR,
and merges it into master. This populates the repo with something for
Agent 2 to find while leaving an observable PR trail.
"""

import logging
import uuid

from app import db, devin_client
from app.config import settings

logger = logging.getLogger(__name__)


INJECTOR_PROMPT_TEMPLATE = """You are simulating a careless software engineer working on the repository {repo}.

Your task is to introduce exactly ONE realistic code flaw into the codebase, open a pull request, and merge it into `master`.

Focus area: {focus_area}

Step-by-step instructions:
1. Create a new branch from `master` named `inject/<short-slug>` (e.g. `inject/lodash-pin`).
2. Make the code change — introduce exactly ONE realistic flaw matching the focus area. Examples:
   - security: hardcoded API key or password in source code, SQL injection via f-string, missing auth check on an endpoint, MD5/SHA1 for password hashing
   - dependency: pin a package to an old, vulnerable version in requirements.txt or package.json
   - code-quality: N+1 database query, swallowed exception, dead code with a logic bug
   - performance: unnecessary full table scan or O(n²) loop in a hot path
3. Keep the change small (1–20 lines) so it is easy to spot and fix later.
4. Commit with a realistic-looking but vague message that does NOT reveal the flaw (e.g. "Update auth helper", "Bump dependency", "Refactor query builder").
5. Push the branch and open a pull request against `master` with a similarly vague title and description.
6. Immediately merge the pull request into `master` using a merge commit (do not squash or rebase).
7. Output one sentence summarising what file you changed and what the flaw is.

Important:
- The pull request MUST be merged — do not leave it open.
- Do NOT wait for a reply after your summary. Your task is fully complete once the PR is merged. Stop immediately.

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

    def _on_poll(s: dict) -> None:
        db.update_session(
            session_id=session_id,
            status=s.get("status", "new"),
            status_detail=s.get("status_detail"),
            acus_consumed=s.get("acus_consumed", 0.0),
        )

    final = devin_client.poll_until_done(session_id, on_poll=_on_poll)

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
