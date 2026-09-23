"""Dripify Open API MCP - multi-account support with webhook archive."""

import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
import psycopg
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


# ============================================================
# CONFIGURATION
# ============================================================

BASE = "https://api.dripify.com/v1/open-api"

DRIPIFY_ACCOUNTS = {
    "account_1": os.environ.get("DRIPIFY_API_KEY_ACCOUNT_1", ""),
    "account_2": os.environ.get("DRIPIFY_API_KEY_ACCOUNT_2", ""),
}

WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ============================================================
# MCP SERVER
# ============================================================

mcp = FastMCP("Dripify Open API")


# ============================================================
# HELPERS
# ============================================================

def page(limit: int, cursor: str | None) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    return {
        "limit": limit,
        **({"cursor": cursor} if cursor else {})
    }


def validate_account(account: str) -> str:
    """
    Validate the requested Dripify account and return its API key.
    """

    if account not in DRIPIFY_ACCOUNTS:
        raise ValueError(
            f"Unknown account '{account}'. "
            "Use 'account_1' or 'account_2'."
        )

    api_key = DRIPIFY_ACCOUNTS[account]

    if not api_key:
        raise RuntimeError(
            f"API key for '{account}' is not configured."
        )

    return api_key


async def call(
    method: str,
    path: str,
    *,
    account: str,
    params: dict | None = None,
    body: dict | None = None
) -> Any:
    """
    Call Dripify Open API using the selected account.
    """

    api_key = validate_account(account)

    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.request(
            method,
            BASE + path,
            headers={
                "X-Api-Key": api_key
            },
            params=params,
            json=body
        )

    if response.is_error:
        raise RuntimeError(
            f"Dripify returned HTTP {response.status_code}; "
            f"retry-after="
            f"{response.headers.get('Retry-After', 'n/a')}"
        )

    return response.json()


# ============================================================
# ACCOUNT TOOLS
# ============================================================

@mcp.tool()
async def dripify_list_accounts() -> dict:
    """
    List configured Dripify account aliases.

    API keys are never returned.
    """

    return {
        "accounts": [
            {
                "account": account,
                "configured": bool(api_key)
            }
            for account, api_key in DRIPIFY_ACCOUNTS.items()
        ]
    }


# ============================================================
# CAMPAIGN TOOLS
# ============================================================

@mcp.tool()
async def dripify_list_campaigns(
    account: str,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List campaigns for a Dripify account.
    """

    return await call(
        "GET",
        "/campaigns",
        account=account,
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_list_campaign_lead_lists(
    account: str,
    campaign_id: int,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List lead lists inside a campaign.
    """

    return await call(
        "GET",
        f"/campaigns/{campaign_id}/lead-lists",
        account=account,
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_get_campaign_statistics(
    account: str,
    campaign_id: int
) -> dict:
    """
    Get campaign statistics including acceptance
    and reply information.
    """

    return await call(
        "GET",
        f"/campaigns/{campaign_id}/statistics",
        account=account
    )


# ============================================================
# LEAD TOOLS
# ============================================================

@mcp.tool()
async def dripify_list_leads(
    account: str,
    limit: int = 25,
    cursor: str | None = None,
    campaign_id: int | None = None,
    lead_list_id: int | None = None,
    status: str | None = None
) -> dict:
    """
    List leads for a Dripify account.

    Optional filters:
    - campaign_id
    - lead_list_id
    - status
    """

    allowed = {
        "READY_FOR_PROCESSING",
        "PROCESSING_ENDED",
        "PROCESS_CORRUPTED",
        "PROCESSING_PAUSED"
    }

    if status is not None and status not in allowed:
        raise ValueError(
            f"status must be one of {sorted(allowed)}"
        )

    params = page(limit, cursor)

    if campaign_id is not None:
        params["campaignId"] = campaign_id

    if lead_list_id is not None:
        params["leadListId"] = lead_list_id

    if status is not None:
        params["status"] = status

    return await call(
        "GET",
        "/leads",
        account=account,
        params=params
    )


@mcp.tool()
async def dripify_get_lead(
    account: str,
    lead_id: int
) -> dict:
    """
    Get information about a specific lead.
    """

    return await call(
        "GET",
        f"/leads/{lead_id}",
        account=account
    )


@mcp.tool()
async def dripify_get_lead_activity(
    account: str,
    lead_id: int,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    Retrieve activity/timeline events for a lead.

    This does not necessarily contain complete
    conversation message bodies.
    """

    return await call(
        "GET",
        f"/leads/{lead_id}/activity",
        account=account,
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_search_leads(
    account: str,
    email: str | None = None,
    linkedin_url: str | None = None
) -> list:
    """
    Search leads by email or LinkedIn URL.
    """

    if not email and not linkedin_url:
        raise ValueError(
            "Provide email or linkedin_url"
        )

    body = {
        **({"email": email} if email else {}),
        **(
            {"linkedinUrl": linkedin_url}
            if linkedin_url
            else {}
        )
    }

    return await call(
        "POST",
        "/leads/search",
        account=account,
        body=body
    )


@mcp.tool()
async def dripify_upload_leads(
    account: str,
    campaign_id: int,
    leads: list[dict[str, str]],
    name: str | None = None
) -> dict:
    """
    Add leads to an EXISTING Dripify campaign.

    This creates a new lead list inside the campaign.

    Each lead must contain exactly one:

    {"linkedinUrl": "https://linkedin.com/in/..."}

    OR

    {"publicId": "linkedin-public-id"}

    Maximum 1000 leads.

    This operation changes Dripify data.
    """

    if not 1 <= len(leads) <= 1000:
        raise ValueError(
            "leads must contain 1 to 1000 entries"
        )

    if name is not None and len(name) > 120:
        raise ValueError(
            "name must be at most 120 characters"
        )

    for lead in leads:

        if not isinstance(lead, dict):
            raise ValueError(
                "Each lead must be an object"
            )

        if set(lead) not in (
            {"linkedinUrl"},
            {"publicId"}
        ):
            raise ValueError(
                "Each lead must contain exactly one "
                "linkedinUrl or publicId"
            )

        value = next(iter(lead.values()))

        if not value:
            raise ValueError(
                "Lead identifier cannot be empty"
            )

    body = {
        "leads": leads,
        **({"name": name} if name else {})
    }

    return await call(
        "POST",
        f"/campaigns/{campaign_id}/leads",
        account=account,
        body=body
    )


# ============================================================
# TEAM TOOLS
# ============================================================

@mcp.tool()
async def dripify_list_teams(
    account: str,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List teams associated with the selected Dripify account.
    """

    return await call(
        "GET",
        "/teams",
        account=account,
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_list_team_members(
    account: str,
    team_id: int,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List members of a Dripify team.
    """

    return await call(
        "GET",
        f"/teams/{team_id}/members",
        account=account,
        params=page(limit, cursor)
    )


# ============================================================
# DATABASE
# ============================================================

def connection():
    """
    Connect to PostgreSQL.
    """

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required for webhook storage"
        )

    return psycopg.connect(DATABASE_URL)


def init_db():
    """
    Create webhook storage table if PostgreSQL is configured.
    """

    if not DATABASE_URL:
        return

    with connection() as db:

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS
            dripify_webhook_events (
                id BIGSERIAL PRIMARY KEY,
                received_at TIMESTAMPTZ
                    NOT NULL DEFAULT now(),
                account TEXT,
                payload JSONB NOT NULL
            )
            """
        )

        # Allows an existing table from the previous
        # single-account version to be upgraded safely.
        db.execute(
            """
            ALTER TABLE dripify_webhook_events
            ADD COLUMN IF NOT EXISTS account TEXT
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            dripify_events_payload_gin
            ON dripify_webhook_events
            USING GIN(payload)
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            dripify_events_account_idx
            ON dripify_webhook_events(account)
            """
        )


# ============================================================
# STORED CONVERSATION TOOLS
# ============================================================

@mcp.tool()
async def dripify_get_conversation(
    account: str,
    lead_id: int | None = None,
    linkedin_url: str | None = None,
    limit: int = 10
) -> dict:
    """
    Get stored webhook conversation data for a lead.

    Provide either lead_id or linkedin_url.

    Data is available only when corresponding webhook
    events have previously been received and stored.
    """

    validate_account(account)

    if lead_id is None and not linkedin_url:
        raise ValueError(
            "Provide lead_id or linkedin_url"
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required for conversation storage"
        )

    if not 1 <= limit <= 50:
        raise ValueError(
            "limit must be between 1 and 50"
        )

    needle = (
        str(lead_id)
        if lead_id is not None
        else linkedin_url
    )

    escaped = (
        needle
        .replace("%", "\\%")
        .replace("_", "\\_")
    )

    with connection() as db:

        rows = db.execute(
            """
            SELECT
                id,
                received_at,
                payload
            FROM dripify_webhook_events
            WHERE account = %s
              AND payload::text ILIKE %s
            ORDER BY received_at DESC
            LIMIT %s
            """,
            (
                account,
                "%" + escaped + "%",
                limit
            )
        ).fetchall()

    if not rows:
        return {
            "found": False,
            "account": account,
            "lead_id": lead_id,
            "linkedin_url": linkedin_url,
            "message": (
                "No stored conversation found "
                "for this lead."
            )
        }

    events = []

    for row in rows:
        events.append({
            "event_id": row[0],
            "received_at": row[1].isoformat(),
            "payload": row[2]
        })

    return {
        "found": True,
        "account": account,
        "lead_id": lead_id,
        "linkedin_url": linkedin_url,
        "events": events
    }


@mcp.tool()
async def dripify_get_stored_webhook_events(
    account: str,
    lead_id: int | None = None,
    linkedin_url: str | None = None,
    limit: int = "account1"
) -> list[dict]:
    """
    Retrieve stored webhook events for a lead
    from a particular Dripify account.
    """

    validate_account(account)

    if lead_id is None and not linkedin_url:
        raise ValueError(
            "Provide lead_id or linkedin_url"
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required for webhook storage"
        )

    if not 1 <= limit <= 100:
        raise ValueError(
            "limit must be between 1 and 100"
        )

    needle = (
        str(lead_id)
        if lead_id is not None
        else linkedin_url
    )

    escaped = (
        needle
        .replace("%", "\\%")
        .replace("_", "\\_")
    )

    with connection() as db:

        rows = db.execute(
            """
            SELECT
                id,
                received_at,
                payload
            FROM dripify_webhook_events
            WHERE account = %s
              AND payload::text ILIKE %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                account,
                "%" + escaped + "%",
                limit
            )
        ).fetchall()

    return [
        {
            "id": row[0],
            "received_at": row[1].isoformat(),
            "payload": row[2]
        }
        for row in rows
    ]


# ============================================================
# HEALTH ENDPOINT
# ============================================================

async def health(request: Request):
    """
    Render health check.
    """

    configured_accounts = [
        account
        for account, api_key
        in DRIPIFY_ACCOUNTS.items()
        if api_key
    ]

    return JSONResponse({
        "ok": True,
        "service": "dripify-mcp",
        "configured_accounts": configured_accounts,
        "database_configured": bool(DATABASE_URL)
    })


# ============================================================
# DRIPIFY WEBHOOK
# ============================================================

async def webhook(request: Request):
    """
    Receive Dripify webhook events.

    URL format:

    /webhooks/dripify/{account}/{token}

    Example:

    /webhooks/dripify/account_1/SECRET
    """

    account = request.path_params["account"]
    token = request.path_params["token"]

    if account not in DRIPIFY_ACCOUNTS:
        return JSONResponse(
            {"error": "unknown account"},
            status_code=404
        )

    if (
        not WEBHOOK_TOKEN
        or not secrets.compare_digest(
            token,
            WEBHOOK_TOKEN
        )
    ):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401
        )

    if not DATABASE_URL:
        return JSONResponse(
            {"error": "database unavailable"},
            status_code=503
        )

    raw = await request.body()

    if len(raw) > 1_000_000:
        return JSONResponse(
            {"error": "payload too large"},
            status_code=413
        )

    try:

        payload = json.loads(raw)

        with connection() as db:

            db.execute(
                """
                INSERT INTO
                dripify_webhook_events(
                    account,
                    payload
                )
                VALUES (
                    %s,
                    %s::jsonb
                )
                """,
                (
                    account,
                    json.dumps(payload)
                )
            )

    except (
        ValueError,
        psycopg.Error
    ):

        return JSONResponse(
            {
                "error":
                "invalid payload or database error"
            },
            status_code=400
        )

    return JSONResponse({
        "stored": True,
        "account": account
    })


# ============================================================
# MCP HTTP APP
# ============================================================

mcp_app = mcp.http_app(
    path="/",
    stateless_http=True,
    json_response=True
)


# ============================================================
# APPLICATION STARTUP
# ============================================================

@asynccontextmanager
async def lifespan(app):

    if not any(DRIPIFY_ACCOUNTS.values()):
        raise RuntimeError(
            "Set DRIPIFY_API_KEY_ACCOUNT_1 "
            "and/or DRIPIFY_API_KEY_ACCOUNT_2"
        )

    init_db()

    async with mcp_app.lifespan(app):
        yield


# ============================================================
# STARLETTE APP
# ============================================================

app = Starlette(
    routes=[
        Route(
            "/health",
            health,
            methods=["GET"]
        ),

        Route(
            "/webhooks/dripify/{account}/{token}",
            webhook,
            methods=["POST"]
        ),

        Mount(
            "/",
            app=mcp_app
        )
    ],
    lifespan=lifespan
)
