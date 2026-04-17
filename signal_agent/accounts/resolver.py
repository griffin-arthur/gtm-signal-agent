"""Account resolution — match signal's company to a HubSpot record, or create one.

Resolution order:
 1. Local `companies` row already has `hubspot_id` → use it.
 2. HubSpot search by exact domain → adopt id, persist locally.
 3. HubSpot search by fuzzy name (rapidfuzz) → if top match score ≥ 90, adopt.
    Otherwise hold for human review (not yet: for Phase 1 we just create).
 4. Company is ICP-qualified and not present → create in HubSpot.

Fuzzy name matching sits behind a conservative threshold because mismatches here
create account conflicts that are very expensive for RevOps to untangle.
"""
from __future__ import annotations

import structlog
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_agent.integrations.hubspot import HubSpotClient
from signal_agent.models import Company

log = structlog.get_logger()

FUZZY_NAME_ACCEPT_THRESHOLD = 90


class AccountResolver:
    def __init__(self, hubspot: HubSpotClient | None = None) -> None:
        self._hubspot = hubspot or HubSpotClient()

    def resolve(self, session: Session, company: Company) -> str | None:
        """Return HubSpot company id, creating local/remote records as needed."""
        if company.hubspot_id:
            return company.hubspot_id

        # 1. Search by domain
        hs = self._hubspot.find_company_by_domain(company.domain)
        if hs:
            company.hubspot_id = hs.id
            session.flush()
            log.info("account.resolved_by_domain", domain=company.domain, hubspot_id=hs.id)
            return hs.id

        # 2. Fuzzy name match against existing local companies with hubspot_ids.
        #    This catches the common case where HubSpot has the company under a
        #    different domain (e.g. acme.com vs acmecorp.com).
        candidates = session.execute(
            select(Company).where(Company.hubspot_id.is_not(None))
        ).scalars().all()
        best = None
        best_score = 0
        for cand in candidates:
            score = fuzz.token_set_ratio(company.name.lower(), cand.name.lower())
            if score > best_score:
                best, best_score = cand, score
        if best and best_score >= FUZZY_NAME_ACCEPT_THRESHOLD:
            company.hubspot_id = best.hubspot_id
            session.flush()
            log.info(
                "account.resolved_by_fuzzy_name",
                name=company.name, matched_to=best.name, score=best_score,
            )
            return best.hubspot_id

        # 3. ICP-qualified → create.
        if company.is_icp:
            created = self._hubspot.create_company(domain=company.domain, name=company.name)
            company.hubspot_id = created.id
            session.flush()
            log.info("account.created", domain=company.domain, hubspot_id=created.id)
            return created.id

        log.info("account.unresolved_not_icp", domain=company.domain)
        return None
