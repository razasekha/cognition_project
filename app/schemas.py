"""Pydantic request/response models and the structured_output_schema for Agent 2."""

from typing import Any, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# API request bodies
# ---------------------------------------------------------------------------

class InjectRequest(BaseModel):
    focus_area: str = Field(
        default="security",
        description=(
            "Describe the type of flaw to inject. Examples: "
            "'security - hardcoded credentials', "
            "'dependency - add an outdated pinned version', "
            "'code quality - introduce an N+1 query'"
        ),
    )


class ScanRequest(BaseModel):
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "Optional focus area for a targeted scan. "
            "When omitted a general scan across all categories is performed. "
            "Example: 'authentication and session management code'"
        ),
    )


class FixRequest(BaseModel):
    pass  # trigger only; issue_id comes from path


class FixFreetextRequest(BaseModel):
    description: str = Field(
        description="Describe the issue to fix in plain language. Be specific about what is wrong and where."
    )


class FeatureRequest(BaseModel):
    description: str = Field(
        description=(
            "Describe the feature to implement. Be specific about behaviour, "
            "files to modify, and any acceptance criteria. "
            "Example: 'Add a /healthz endpoint that returns DB connection status'"
        )
    )


# ---------------------------------------------------------------------------
# Structured output schema for Agent 2 (the scanner)
# Passed verbatim to the Devin API as structured_output_schema.
# ---------------------------------------------------------------------------

SCANNER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "description": "List of issues found in the repository",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Issue severity level",
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Issue category, e.g. 'security', 'dependency', "
                            "'code-quality', 'performance'"
                        ),
                    },
                    "file": {
                        "type": "string",
                        "description": "Relative file path where the issue was found",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Approximate line number (0 if unknown)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Clear description of the issue",
                    },
                    "recommendation": {
                        "type": "string",
                        "description": "Concrete recommendation for how to fix the issue",
                    },
                },
                "required": ["severity", "category", "description"],
            },
        },
        "scan_summary": {
            "type": "string",
            "description": "One-paragraph summary of what was scanned and the overall findings",
        },
    },
    "required": ["issues"],
}


# ---------------------------------------------------------------------------
# API response models
# ---------------------------------------------------------------------------

class SessionOut(BaseModel):
    session_id: str
    role: str
    status: str
    devin_url: Optional[str] = None
    pr_url: Optional[str] = None
    acus_consumed: float = 0.0


class IssueOut(BaseModel):
    id: int
    severity: str
    category: str
    file: Optional[str] = None
    line: Optional[int] = None
    description: str
    recommendation: Optional[str] = None
    status: str
    github_issue_url: Optional[str] = None
    fix_pr_url: Optional[str] = None


class MetricsOut(BaseModel):
    issues: dict[str, Any]
    features: dict[str, Any]
    sessions: dict[str, Any]
    mttr_seconds: int
