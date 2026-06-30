"""Single source of truth for campaign-account role capabilities.

The role lives in `campaign_accounts.role` (a `String(16)` column). Historically
the values were `scraping | dm | both`. The **inbox** capability — reading the
DM inbox of a `scrape_mode='dm_threads'` campaign — is orthogonal and
*combinable*: the inbox account may ALSO scrape bios and/or send DMs. Because
exactly one inbox exists per account, only **one** account per campaign may
carry the inbox capability; the other accounts (scraping/dm) are unlimited.

Roles are modelled as composite strings instead of separate boolean columns so
no DB migration is needed (`role` is already a free string). The trade-off is a
combinatorial set of values — which is exactly why every query/filter MUST go
through the constants and helpers below and NEVER inline a tuple like
`("scraping", "both")`: a new combo can then never silently miss a call-site.

Capability matrix:

    role             scrape(bio)   dm   inbox
    ---------------   -----------   --   -----
    scraping              x
    dm                                x
    both                  x         x
    inbox                                  x
    inbox_scraping        x                x
    inbox_dm                          x    x
    inbox_both            x         x      x
"""
from __future__ import annotations

# Can perform bio scraping (user_info_v1 enrichment) for a campaign.
SCRAPE_ROLES: tuple[str, ...] = ("scraping", "both", "inbox_scraping", "inbox_both")

# Can send DMs for a campaign.
DM_ROLES: tuple[str, ...] = ("dm", "both", "inbox_dm", "inbox_both")

# Carries the inbox capability (DM-thread listing). Capped at 1 per campaign.
INBOX_ROLES: tuple[str, ...] = ("inbox", "inbox_scraping", "inbox_dm", "inbox_both")

# Every valid role value (schema validation + UI dropdown).
ALL_ROLES: tuple[str, ...] = (
    "scraping",
    "dm",
    "both",
    "inbox",
    "inbox_scraping",
    "inbox_dm",
    "inbox_both",
)

# Default applied when a row predates the column or omits the role.
DEFAULT_ROLE = "both"


def can_scrape(role: str | None) -> bool:
    """True if an account with this role may run the bio (scraping) phase."""
    return (role or DEFAULT_ROLE) in SCRAPE_ROLES


def can_dm(role: str | None) -> bool:
    """True if an account with this role may send DMs."""
    return (role or DEFAULT_ROLE) in DM_ROLES


def is_inbox(role: str | None) -> bool:
    """True if an account with this role carries the inbox capability."""
    return (role or DEFAULT_ROLE) in INBOX_ROLES
