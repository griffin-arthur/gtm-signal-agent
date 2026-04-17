"""Create the four custom company properties the signal agent writes to.

Idempotent — safe to re-run. Skips properties that already exist.

Usage:
    .venv/bin/python -m scripts.setup_hubspot

Requires HUBSPOT_ACCESS_TOKEN with scope: crm.schemas.companies.write

The property group "arthur_signals" is created on demand so the four properties
live together in the UI instead of cluttering the "Information" group.
"""
from __future__ import annotations

import sys

from hubspot import HubSpot
from hubspot.crm.properties import (
    PropertyCreate,
    PropertyGroupCreate,
)
from hubspot.crm.properties.exceptions import ApiException

from signal_agent.config import settings

GROUP_NAME = "arthur_signals"
GROUP_LABEL = "Arthur Signals"
OBJECT_TYPE = "companies"

PROPERTIES = [
    dict(
        name="arthur_signal_score",
        label="Arthur Signal Score",
        type="number",
        field_type="number",
        description="Cumulative signal score over the last 60 days (0-30+).",
    ),
    dict(
        name="arthur_signal_tier",
        label="Arthur Signal Tier",
        type="enumeration",
        field_type="select",
        description="Tier of the top contributing signal (tier_1 = strongest).",
        options=[
            dict(label="Tier 1", value="tier_1", displayOrder=0),
            dict(label="Tier 2", value="tier_2", displayOrder=1),
            dict(label="Tier 3", value="tier_3", displayOrder=2),
        ],
    ),
    dict(
        name="arthur_signal_summary",
        label="Arthur Signal Summary",
        type="string",
        field_type="textarea",
        description="LLM-generated one-sentence summary of why this account matters.",
    ),
    dict(
        name="arthur_last_signal_date",
        label="Arthur Last Signal Date",
        type="date",
        field_type="date",
        description="When the most recent contributing signal was detected.",
    ),
]


def main() -> int:
    if not settings.hubspot_access_token:
        print("HUBSPOT_ACCESS_TOKEN not set in .env", file=sys.stderr)
        return 1

    client = HubSpot(access_token=settings.hubspot_access_token)

    # 1. Ensure group exists.
    try:
        groups = client.crm.properties.groups_api.get_all(object_type=OBJECT_TYPE).results
        group_names = {g.name for g in groups}
    except ApiException as e:
        print(f"Failed to list property groups: {e}", file=sys.stderr)
        return 1

    if GROUP_NAME not in group_names:
        try:
            client.crm.properties.groups_api.create(
                object_type=OBJECT_TYPE,
                property_group_create=PropertyGroupCreate(
                    name=GROUP_NAME,
                    label=GROUP_LABEL,
                    display_order=-1,
                ),
            )
            print(f"[created] property group: {GROUP_LABEL}")
        except ApiException as e:
            print(f"Failed to create group: {e}", file=sys.stderr)
            return 1
    else:
        print(f"[exists]  property group: {GROUP_LABEL}")

    # 2. List existing properties once, then create any missing.
    existing = {p.name for p in client.crm.properties.core_api.get_all(object_type=OBJECT_TYPE).results}

    for spec in PROPERTIES:
        if spec["name"] in existing:
            print(f"[exists]  property: {spec['name']}")
            continue
        payload = PropertyCreate(
            name=spec["name"],
            label=spec["label"],
            type=spec["type"],
            field_type=spec["field_type"],
            group_name=GROUP_NAME,
            description=spec.get("description", ""),
            options=spec.get("options"),
        )
        try:
            client.crm.properties.core_api.create(
                object_type=OBJECT_TYPE, property_create=payload
            )
            print(f"[created] property: {spec['name']}")
        except ApiException as e:
            print(f"Failed to create {spec['name']}: {e}", file=sys.stderr)
            return 1

    print()
    print("HubSpot setup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
