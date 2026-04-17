"""Keyword sets used to filter job postings at ingest time.

**Source of truth:** `docs/icp.md` §4 (trigger events) + §8 (regulatory anchors)
+ §9 (Arthur-specific phrases). When the ICP doc changes, update these lists.

This is a cheap pre-filter. The LLM validator is the authoritative classifier —
these keywords only decide whether a posting is *worth spending an LLM call on*.

Grouped by signal_type so the matched group informs downstream scoring.
"""
from __future__ import annotations

# (signal_type, [keywords, case-insensitive, matched against title + description])
JOB_KEYWORD_GROUPS: list[tuple[str, list[str]]] = [
    (
        # Tier 1 — explicit governance / risk / MRM roles
        "job_posting.ai_governance",
        [
            "ai governance",
            "agent governance",
            "responsible ai",
            "ai risk",
            "ai risk management",
            "ai compliance",
            "ai assurance",
            "ml governance",
            "model risk management",
            "model risk",
            "mrm",
            "ai policy",
            "ai trust and safety",
            "ai audit",
            "agent audit",
            # Regulatory-frame roles
            "sr 11-7",
            "eu ai act",
            "nist ai rmf",
        ],
    ),
    (
        # Tier 1 — AI leadership hires (CAIO / Head of AI / VP AI etc.)
        "job_posting.ai_leadership",
        [
            "chief ai officer",
            "chief artificial intelligence officer",
            "caio",
            "head of ai",
            "head of agentic ai",
            "head of responsible ai",
            "head of applied ai",
            "vp of ai",
            "vp, ai",
            "vp artificial intelligence",
            "director of ai",
            "head of machine learning",
            "head of ml platform",
            "head of mlops",
            "ai center of excellence",
            "ai coe",
        ],
    ),
    (
        # Tier 2 — platform / scaling / agent-infra roles
        "job_posting.ml_platform",
        [
            "ml platform",
            "mlops",
            "ml infrastructure",
            "llm platform",
            "genai platform",
            "agentic ai",
            "ai agent engineer",
            "agent platform",
            "ai platform engineer",
            "llmops",
            "agent infrastructure",
            "bedrock agents",
            "vertex ai agents",
            "langchain",
            "crewai",
            # Security-adjacent AI roles — CISO world is an Arthur buyer too
            "ai security engineer",
            "prompt injection",
            "shadow ai",
            "shadow agents",
        ],
    ),
]

# Patterns that should suppress a posting even if keywords match — these are baked
# in; operator-added suppressions live in the DB via the `suppressions` table.
HARD_SUPPRESSION_SUBSTRINGS = [
    "recruiting agency",
    "staffing firm",
    "on behalf of our client",
    "contract-to-hire on behalf",
]


def classify_job(title: str, description: str) -> tuple[str, list[str]] | None:
    """Return (signal_type, matched_keywords) or None if no group matches."""
    haystack = f"{title}\n{description}".lower()
    for substr in HARD_SUPPRESSION_SUBSTRINGS:
        if substr in haystack:
            return None
    for signal_type, keywords in JOB_KEYWORD_GROUPS:
        matched = [kw for kw in keywords if kw in haystack]
        if matched:
            return signal_type, matched
    return None
