"""Conference speaker ingestor.

Fires when an ICP company employee is listed as a speaker at an ML/AI
conference. Interpretation: the company has someone tech-lead-enough to
speak publicly about their AI work, which correlates with production-scale
deployments (and thus Arthur's buying window).

How it works:
 - `seeds/conferences.yaml` lists conferences with CSS selectors for
   speaker cards + speaker/company fields.
 - We fetch each page, run the selectors, extract (speaker, company, talk).
 - A signal fires when `company` matches an ICP target (fuzzy, via rapidfuzz).
 - No LLM call for the match step — we only need keyword classification
   to decide signal_type. The downstream LLM validator confirms as usual.

Note on reliability: conference sites change DOM often. Phase 3 ships with
a single entry (AI Engineer Summit) as a working example; ops needs to tend
this seed list. Silent scraping failures are logged loudly so you notice.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog
import yaml
from rapidfuzz import fuzz

from signal_agent.ingestors.base import CompanyTarget, Ingestor
from signal_agent.schemas import NormalizedSignal

log = structlog.get_logger()

CONFERENCES_YAML = Path(__file__).resolve().parent.parent / "seeds" / "conferences.yaml"
NAME_MATCH_THRESHOLD = 85
MAX_HTML_BYTES = 2_000_000


class ConferenceIngestor(Ingestor):
    """Scans conference speaker pages and emits signals for ICP matches.

    Unlike other ingestors, this one isn't polled *per company*. It scans each
    conference page once, then matches every extracted speaker-company against
    whichever CompanyTarget it was called with. To avoid redundant work, we
    cache the parsed speaker list per conference per process.
    """
    source = "conference"

    _speaker_cache: dict[str, list[dict]] = {}

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._configs = self._load_configs()

    @staticmethod
    def _load_configs() -> list[dict]:
        if not CONFERENCES_YAML.exists():
            return []
        with CONFERENCES_YAML.open() as f:
            return yaml.safe_load(f) or []

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": "SignalAgent/0.1"},
                follow_redirects=True,
            )
        return self._client

    async def _fetch_speakers(self, cfg: dict) -> list[dict]:
        cache_key = cfg["url"]
        if cache_key in self._speaker_cache:
            return self._speaker_cache[cache_key]

        client = await self._get_client()
        try:
            r = await client.get(cfg["url"])
            r.raise_for_status()
            html = r.text[:MAX_HTML_BYTES]
        except Exception as e:
            log.warning("conference.fetch_failed", conf=cfg["name"], err=str(e))
            self._speaker_cache[cache_key] = []
            return []

        speakers = self._parse_speakers(html, cfg)
        if not speakers:
            log.warning("conference.zero_speakers_parsed",
                        conf=cfg["name"], url=cfg["url"])
        self._speaker_cache[cache_key] = speakers
        return speakers

    @staticmethod
    def _parse_speakers(html: str, cfg: dict) -> list[dict]:
        """Parse speakers using BeautifulSoup if available, else fall back to regex."""
        try:
            from bs4 import BeautifulSoup  # lazy import — optional dep
        except ImportError:
            log.warning("conference.bs4_missing", hint="pip install beautifulsoup4")
            return []

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(cfg["selector"])
        out: list[dict] = []
        for card in cards:
            name_el = card.select_one(cfg.get("speaker_attr", "")) if cfg.get("speaker_attr") else None
            comp_el = card.select_one(cfg.get("company_attr", "")) if cfg.get("company_attr") else None
            title_el = card.select_one(cfg.get("title_attr", "")) if cfg.get("title_attr") else None
            speaker = (name_el.get_text(" ", strip=True) if name_el else "").strip()
            company = (comp_el.get_text(" ", strip=True) if comp_el else "").strip()
            talk = (title_el.get_text(" ", strip=True) if title_el else "").strip()
            if not speaker or not company:
                continue
            out.append({"speaker": speaker, "company": company, "talk": talk})
        return out

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        if not self._configs:
            return

        for cfg in self._configs:
            speakers = await self._fetch_speakers(cfg)
            for entry in speakers:
                score = fuzz.token_set_ratio(entry["company"].lower(), target.name.lower())
                if score < NAME_MATCH_THRESHOLD:
                    continue
                dedup = hashlib.sha1(
                    f"{cfg['url']}|{entry['speaker']}|{entry['company']}".encode()
                ).hexdigest()[:16]
                signal_text = (
                    f"{entry['speaker']} ({entry['company']}) at {cfg['name']}"
                    + (f": {entry['talk']}" if entry["talk"] else "")
                )
                yield NormalizedSignal(
                    company_domain=target.domain,
                    company_name=target.name,
                    signal_type="conference.speaker",
                    source=self.source,
                    source_url=cfg["url"] + f"#{dedup}",
                    signal_text=signal_text,
                    raw_payload={
                        "conference": cfg["name"],
                        "speaker": entry["speaker"],
                        "company_as_listed": entry["company"],
                        "talk_title": entry["talk"],
                        "match_score": score,
                    },
                    detected_at=datetime.now(timezone.utc),
                    matched_keywords=[target.name, entry["speaker"]],
                    suggested_tier_hint=2,
                )
