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

import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.agents import fixer, injector, scanner
from app.config import settings
from app.schemas import InjectRequest, MetricsOut

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Scheduled scan task
# ---------------------------------------------------------------------------

def _scheduled_scan() -> None:
    """Called by APScheduler on the configured cron. Runs scan then auto-fires fixers."""
    logger.info("Scheduled scan triggered.")
    try:
        new_ids, _ = scanner.run()
        if new_ids:
            logger.info("Auto-firing fixer for %d new issue(s): %s", len(new_ids), new_ids)
            for issue_id in new_ids:
                try:
                    fixer.run(issue_id)
                except Exception as exc:
                    logger.exception("Fixer failed for issue %d: %s", issue_id, exc)
    except Exception as exc:
        logger.exception("Scheduled scan failed: %s", exc)


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
    scheduler.start()
    logger.info("APScheduler started. Scan cron: %s", settings.scan_cron)
    yield
    scheduler.shutdown(wait=False)
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


def _bg_scan() -> None:
    try:
        new_ids, _ = scanner.run()
        if new_ids:
            logger.info("Scan found %d new issue(s); firing fixers.", len(new_ids))
            for issue_id in new_ids:
                try:
                    fixer.run(issue_id)
                except Exception as exc:
                    logger.exception("Fixer failed for issue %d: %s", issue_id, exc)
    except Exception as exc:
        logger.exception("Manual scan failed: %s", exc)


def _bg_fix(issue_id: int) -> None:
    try:
        fixer.run(issue_id)
    except Exception as exc:
        logger.exception("Fixer background task failed for issue %d: %s", issue_id, exc)


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
def scan(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger Agent 2 to scan the repository immediately."""
    logger.info("POST /scan triggered manually.")
    background_tasks.add_task(_bg_scan)
    return {
        "message": "Scanner session started in background. Agent 3 will auto-fire for new findings.",
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
