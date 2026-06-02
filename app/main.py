"""FastAPI application and APScheduler wiring.

Routes
------
POST /inject           – Trigger Agent 1 (bad engineer)
POST /scan             – Trigger Agent 2 (scanner) immediately
POST /fix/{issue_id}   – Trigger Agent 3 (fixer) for one issue
GET  /dashboard        – HTML observability dashboard
GET  /api/issues       – JSON list of all issues
GET  /api/sessions     – JSON list of all sessions
GET  /api/metrics      – JSON summary metrics
GET  /healthz          – Health check
"""

import datetime
import logging
import re
import requests
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.agents import fixer, injector, scanner
from app.config import settings
from app.schemas import InjectRequest, MetricsOut, ScanRequest, FeatureRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")


def _datetimeformat(epoch: int) -> str:
    """Format a Unix epoch as '02 Jun 2026, 00:10' in local server time."""
    try:
        dt = datetime.datetime.fromtimestamp(int(epoch))
        return dt.strftime("%d %b %Y, %H:%M")
    except Exception:
        return "—"


templates.env.filters["datetimeformat"] = _datetimeformat

# Shared executor for parallel fixer sessions
_fixer_pool = ThreadPoolExecutor(max_workers=settings.max_parallel_fixers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pr_number_from_url(pr_url: str) -> str | None:
    """Extract PR number from a GitHub PR URL."""
    try:
        return pr_url.rstrip("/").split("/")[-1]
    except Exception:
        return None


def _is_pr_merged_github(pr_url: str) -> bool:
    """Check GitHub REST API to see if a PR has been merged. Returns True if merged."""
    if not settings.github_token:
        return False
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not m:
        return False
    repo, pr_num = m.group(1), m.group(2)
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_num}"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"token {settings.github_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return bool(data.get("merged_at"))
    except Exception as exc:
        logger.warning("GitHub PR check failed for %s: %s", pr_url, exc)
    return False


def _is_pr_merged_devin(issue: dict) -> bool:
    """Fallback: check Devin session pull_requests[].pr_state == 'merged'."""
    fix_session_id = issue.get("fix_session_id")
    if not fix_session_id:
        return False
    from app import devin_client
    try:
        session = devin_client.get_session(fix_session_id)
        prs = session.get("pull_requests") or []
        for pr in prs:
            if pr.get("pr_state") == "merged":
                return True
    except Exception as exc:
        logger.warning("Devin PR state check failed for session %s: %s", fix_session_id, exc)
    return False


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

def _scheduled_scan() -> None:
    """Called by APScheduler on the configured cron. Runs scan then auto-fires fixers in parallel."""
    logger.info("Scheduled scan triggered.")
    try:
        new_ids, _ = scanner.run()
        if new_ids:
            logger.info(
                "Auto-firing %d fixer(s) in parallel: %s", len(new_ids), new_ids
            )
            for issue_id in new_ids:
                _fixer_pool.submit(_safe_fixer_run, issue_id)
    except Exception as exc:
        logger.exception("Scheduled scan failed: %s", exc)


def _poll_pr_merges() -> None:
    """Poll GitHub (or Devin fallback) for merged PRs and flip status to 'fixed'."""
    issues = db.get_issues_by_status("pr_created")
    if not issues:
        return
    logger.info("PR merge poll: checking %d open PR(s).", len(issues))
    for issue in issues:
        pr_url = issue.get("fix_pr_url")
        if not pr_url:
            continue
        merged = _is_pr_merged_github(pr_url) or _is_pr_merged_devin(issue)
        if merged:
            logger.info("Issue %d PR merged — marking fixed. PR: %s", issue["id"], pr_url)
            db.set_issue_fixed(issue["id"])


def _safe_fixer_run(issue_id: int) -> None:
    try:
        fixer.run(issue_id)
    except Exception as exc:
        logger.exception("Fixer failed for issue %d: %s", issue_id, exc)


def _recover_sessions() -> None:
    """On startup, re-poll any sessions still marked as running/new in the DB.

    This handles the case where a server restart killed background threads
    mid-flight. We fetch the real current status from Devin, update the DB,
    and for completed scanners we re-process the structured output so issues
    are not lost.
    """
    orphans = db.get_incomplete_sessions()
    if not orphans:
        logger.info("Session recovery: no incomplete sessions found.")
        return

    logger.info("Session recovery: found %d incomplete session(s) — re-polling Devin.", len(orphans))

    for sess in orphans:
        session_id = sess["session_id"]
        role = sess["role"]
        issue_id = sess.get("issue_id")
        try:
            from app import devin_client as dc
            current = dc.get_session(session_id)
            status = current.get("status", "")
            detail = current.get("status_detail", "")
            pr_urls = dc.get_pr_urls(current)
            pr_url = pr_urls[0] if pr_urls else None

            db.update_session(
                session_id=session_id,
                status=status,
                status_detail=detail,
                pr_url=pr_url,
                acus_consumed=current.get("acus_consumed", 0.0),
            )

            is_done = dc.is_done_ok(current)

            if role == "scanner" and is_done:
                # Re-process structured output — emit any issues we missed
                structured = current.get("structured_output") or {}
                findings = structured.get("issues", [])
                devin_url = sess.get("devin_url", "")
                new_ids: list[int] = []
                for finding in findings:
                    issue_id_new = db.insert_issue_if_new(
                        severity=finding.get("severity", "low"),
                        category=finding.get("category", "unknown"),
                        file=finding.get("file") or "",
                        line=finding.get("line") or 0,
                        description=finding.get("description", ""),
                        recommendation=finding.get("recommendation") or "",
                        source_session_id=session_id,
                    )
                    if issue_id_new:
                        new_ids.append(issue_id_new)
                        logger.info(
                            "Recovery: logged missed issue id=%d from scanner %s",
                            issue_id_new, session_id,
                        )
                if new_ids:
                    logger.info(
                        "Recovery: scanner %s had %d unlogged issue(s) — firing fixers.",
                        session_id, len(new_ids),
                    )
                    for iid in new_ids:
                        _fixer_pool.submit(_safe_fixer_run, iid)

            elif role == "fixer" and issue_id and is_done:
                if pr_url:
                    db.set_issue_pr_created(issue_id, pr_url)
                    logger.info(
                        "Recovery: fixer %s succeeded for issue %d, pr=%s",
                        session_id, issue_id, pr_url,
                    )
                else:
                    db.set_issue_failed(issue_id)
                    logger.warning(
                        "Recovery: fixer %s for issue %d completed without PR.",
                        session_id, issue_id,
                    )

            elif status in ("error", "suspended"):
                if role == "fixer" and issue_id:
                    db.set_issue_failed(issue_id)
                logger.warning(
                    "Recovery: session %s (%s) is in terminal error state %s/%s.",
                    session_id, role, status, detail,
                )
            else:
                # Still genuinely running — re-submit to poll in background
                logger.info(
                    "Recovery: session %s (%s) still active (%s/%s) — re-attaching poller.",
                    session_id, role, status, detail,
                )
                if role == "scanner":
                    _fixer_pool.submit(_reattach_scanner, session_id)
                elif role == "fixer" and issue_id:
                    _fixer_pool.submit(_reattach_fixer, session_id, issue_id)

        except Exception as exc:
            logger.exception("Recovery failed for session %s: %s", session_id, exc)


def _reattach_scanner(session_id: str) -> None:
    """Re-attach a poll loop to a scanner session that was still running at startup."""
    from app import devin_client as dc
    try:
        def _on_poll(s: dict) -> None:
            db.update_session(
                session_id=session_id,
                status=s.get("status", "new"),
                status_detail=s.get("status_detail"),
                acus_consumed=s.get("acus_consumed", 0.0),
            )

        final = dc.poll_until_done(session_id, on_poll=_on_poll)
        db.update_session(
            session_id=session_id,
            status=final.get("status", "error"),
            status_detail=final.get("status_detail"),
            acus_consumed=final.get("acus_consumed", 0.0),
        )

        if not dc.is_done_ok(final):
            return

        structured = final.get("structured_output") or {}
        findings = structured.get("issues", [])
        devin_url = ""
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT devin_url FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row:
                devin_url = row["devin_url"] or ""

        new_ids: list[int] = []
        for finding in findings:
            iid = db.insert_issue_if_new(
                severity=finding.get("severity", "low"),
                category=finding.get("category", "unknown"),
                file=finding.get("file") or "",
                line=finding.get("line") or 0,
                description=finding.get("description", ""),
                recommendation=finding.get("recommendation") or "",
                source_session_id=session_id,
            )
            if iid:
                new_ids.append(iid)
        if new_ids:
            for iid in new_ids:
                _fixer_pool.submit(_safe_fixer_run, iid)
        logger.info("Reattached scanner %s completed — %d new issue(s).", session_id, len(new_ids))
    except Exception as exc:
        logger.exception("Reattached scanner %s failed: %s", session_id, exc)


def _reattach_fixer(session_id: str, issue_id: int) -> None:
    """Re-attach a poll loop to a fixer session that was still running at startup."""
    from app import devin_client as dc
    try:
        def _on_poll(s: dict) -> None:
            db.update_session(
                session_id=session_id,
                status=s.get("status", "new"),
                status_detail=s.get("status_detail"),
                pr_url=(dc.get_pr_urls(s) or [None])[0],
                acus_consumed=s.get("acus_consumed", 0.0),
            )

        final = dc.poll_until_done(session_id, on_poll=_on_poll)
        pr_urls = dc.get_pr_urls(final)
        pr_url = pr_urls[0] if pr_urls else None
        db.update_session(
            session_id=session_id,
            status=final.get("status", "error"),
            status_detail=final.get("status_detail"),
            pr_url=pr_url,
            acus_consumed=final.get("acus_consumed", 0.0),
        )
        if dc.is_done_ok(final) and pr_url:
            db.set_issue_pr_created(issue_id, pr_url)
        else:
            db.set_issue_failed(issue_id)
        logger.info("Reattached fixer %s for issue %d completed.", session_id, issue_id)
    except Exception as exc:
        logger.exception("Reattached fixer %s failed: %s", session_id, exc)


def _parse_cron(cron_expr: str) -> CronTrigger:
    """Parse a standard 5-field cron expression into an APScheduler CronTrigger."""
    parts = re.split(r"\s+", cron_expr.strip())
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression (need 5 fields): {cron_expr!r}")
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
    )


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    trigger = _parse_cron(settings.scan_cron)
    scheduler.add_job(_scheduled_scan, trigger, id="scanner", replace_existing=True)
    scheduler.add_job(
        _poll_pr_merges,
        IntervalTrigger(minutes=settings.pr_merge_poll_minutes),
        id="pr_merge_poller",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "APScheduler started. Scan cron: %s | PR merge poll: every %d min",
        settings.scan_cron,
        settings.pr_merge_poll_minutes,
    )
    # Re-attach pollers for any sessions that were mid-flight when the server last stopped
    _fixer_pool.submit(_recover_sessions)
    yield
    scheduler.shutdown(wait=False)
    _fixer_pool.shutdown(wait=False)
    logger.info("APScheduler stopped.")


app = FastAPI(
    title="Devin Orchestrator",
    description="Three-agent code quality automation using the Devin API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Background task helpers
# ---------------------------------------------------------------------------

def _bg_inject(focus_area: str) -> None:
    try:
        injector.run(focus_area)
    except Exception as exc:
        logger.exception("Injector background task failed: %s", exc)


def _bg_scan(focus_area: str | None = None) -> None:
    try:
        new_ids, _ = scanner.run(focus_area=focus_area)
        if new_ids:
            logger.info("Scan found %d new issue(s); firing fixers in parallel.", len(new_ids))
            for issue_id in new_ids:
                _fixer_pool.submit(_safe_fixer_run, issue_id)
    except Exception as exc:
        logger.exception("Manual scan failed: %s", exc)


def _bg_fix(issue_id: int) -> None:
    _fixer_pool.submit(_safe_fixer_run, issue_id)


def _bg_feature(description: str) -> None:
    try:
        fixer.run_feature(description)
    except Exception as exc:
        logger.exception("Feature background task failed: %s", exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/inject")
def inject(body: InjectRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger Agent 1 to introduce a flaw into the target repository."""
    logger.info("POST /inject | focus_area=%s", body.focus_area)
    background_tasks.add_task(_bg_inject, body.focus_area)
    return {
        "message": "Injector session started in background.",
        "focus_area": body.focus_area,
        "target_repo": settings.target_repo,
    }


@app.post("/scan")
def scan(background_tasks: BackgroundTasks, body: ScanRequest = ScanRequest()) -> dict[str, Any]:
    """Trigger Agent 2 to scan the repository immediately. Optionally accepts a focus prompt."""
    focus = body.prompt or None
    logger.info("POST /scan triggered manually. targeted=%s", bool(focus))
    background_tasks.add_task(_bg_scan, focus)
    mode = "targeted" if focus else "general"
    return {
        "message": f"Scanner ({mode}) started in background. Agent 3 will auto-fire for new findings.",
        "target_repo": settings.target_repo,
        "focus": focus,
    }


@app.post("/feature")
def feature(body: FeatureRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger Agent 3 to implement a new feature and open a pull request."""
    logger.info("POST /feature | description=%s…", body.description[:60])
    background_tasks.add_task(_bg_feature, body.description)
    return {
        "message": "Feature implementation session started in background.",
        "description": body.description[:120],
        "target_repo": settings.target_repo,
    }


@app.post("/fix/{issue_id}")
def fix(issue_id: int, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger Agent 3 to fix a specific issue by ID."""
    issue = db.get_issue(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail=f"Issue {issue_id} not found.")
    if issue["status"] not in ("open", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Issue {issue_id} has status '{issue['status']}' — only 'open' or 'failed' can be fixed.",
        )
    logger.info("POST /fix/%d triggered manually.", issue_id)
    background_tasks.add_task(_bg_fix, issue_id)
    return {
        "message": f"Fixer session started for issue {issue_id}.",
        "issue": {
            "id": issue["id"],
            "severity": issue["severity"],
            "category": issue["category"],
            "description": issue["description"][:120],
        },
    }


@app.get("/api/issues")
def api_issues() -> list[dict[str, Any]]:
    return db.get_all_issues()


@app.get("/api/sessions")
def api_sessions() -> list[dict[str, Any]]:
    return db.get_all_sessions()


@app.get("/api/metrics", response_model=MetricsOut)
def api_metrics() -> dict[str, Any]:
    return db.get_metrics()


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    """Combined snapshot for the Control Center — sessions, issues, and metrics in one call."""
    return {
        "sessions": db.get_all_sessions(),
        "issues": db.get_all_issues(),
        "metrics": db.get_metrics(),
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    issues = db.get_all_issues()
    sessions = db.get_all_sessions()
    metrics = db.get_metrics()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "issues": issues,
            "sessions": sessions,
            "metrics": metrics,
            "target_repo": settings.target_repo,
            "scan_cron": settings.scan_cron,
        },
    )
