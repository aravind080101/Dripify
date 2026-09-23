"""Dripify Open API MCP with optional raw reply-webhook archive."""

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
# Configuration
# ============================================================

BASE = "https://api.dripify.com/v1/open-api"

API_KEY = os.environ.get("DRIPIFY_API_KEY", "")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ============================================================
# MCP Server
# ============================================================

mcp = FastMCP("Dripify Open API")


# ============================================================
# Helpers
# ============================================================

def page(limit: int, cursor: str | None) -> dict:
    """Build pagination parameters."""

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    return {
        "limit": limit,
        **({"cursor": cursor} if cursor else {})
    }


async def call(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None
) -> Any:
    """
    Call the Dripify Open API.
    """

    if not API_KEY:
        raise RuntimeError("DRIPIFY_API_KEY is missing")

    async with httpx.AsyncClient(timeout=25) as client:

        response = await client.request(
            method,
            BASE + path,
            headers={
                "X-Api-Key": API_KEY
            },
            params=params,
            json=body
        )

    if response.is_error:
        # Avoid returning upstream response bodies because
        # they could contain personal information.

        raise RuntimeError(
            f"Dripify returned HTTP {response.status_code}; "
            f"retry-after={response.headers.get('Retry-After', 'n/a')}"
        )

    return response.json()


# ============================================================
# CAMPAIGN TOOLS
# ============================================================

@mcp.tool()
async def dripify_list_campaigns(
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List Dripify campaigns.

    Pass nextCursor from the response to fetch another page.
    """

    return await call(
        "GET",
        "/campaigns",
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_list_campaign_lead_lists(
    campaign_id: int,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List lead lists belonging to a Dripify campaign.
    """

    return await call(
        "GET",
        f"/campaigns/{campaign_id}/lead-lists",
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_get_campaign_statistics(
    campaign_id: int
) -> dict:
    """
    Get campaign statistics including counts,
    acceptance rates and reply rates.
    """

    return await call(
        "GET",
        f"/campaigns/{campaign_id}/statistics"
    )


# ============================================================
# LEAD TOOLS
# ============================================================

@mcp.tool()
async def dripify_list_leads(
    limit: int = 25,
    cursor: str | None = None,
    campaign_id: int | None = None,
    lead_list_id: int | None = None,
    status: str | None = None
) -> dict:
    """
    List Dripify leads.

    Leads can optionally be filtered by:
    - campaign
    - lead list
    - processing status

    lead_list_id takes precedence when supplied.
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
        params=params
    )


@mcp.tool()
async def dripify_get_lead(
    lead_id: int
) -> dict:
    """
    Get information about a specific Dripify lead.
    """

    return await call(
        "GET",
        f"/leads/{lead_id}"
    )


@mcp.tool()
async def dripify_get_lead_activity(
    lead_id: int,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    Retrieve timeline/activity events for a lead.

    This endpoint does not necessarily contain
    full message bodies.
    """

    return await call(
        "GET",
        f"/leads/{lead_id}/activity",
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_search_leads(
    email: str | None = None,
    linkedin_url: str | None = None
) -> list:
    """
    Search for leads using email or LinkedIn URL.

    At least one search value must be provided.
    """

    if not email and not linkedin_url:
        raise ValueError(
            "Provide email or linkedin_url"
        )

    body = {
        **({"email": email} if email else {}),
        **({"linkedinUrl": linkedin_url} if linkedin_url else {})
    }

    return await call(
        "POST",
        "/leads/search",
        body=body
    )


@mcp.tool()
async def dripify_upload_leads(
    campaign_id: int,
    leads: list[dict[str, str]],
    name: str | None = None
) -> dict:
    """
    Add leads to an EXISTING Dripify campaign.

    Creates a new lead list inside the campaign.

    Each lead must contain exactly ONE of:

    {"linkedinUrl": "https://linkedin.com/in/..."}
    
    OR

    {"publicId": "linkedin-public-id"}

    Maximum: 1000 leads per request.

    WARNING:
    This operation modifies Dripify data.
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
        body=body
    )


# ============================================================
# TEAM TOOLS
# ============================================================

@mcp.tool()
async def dripify_list_teams(
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List teams that the API-key owner belongs to.
    """

    return await call(
        "GET",
        "/teams",
        params=page(limit, cursor)
    )


@mcp.tool()
async def dripify_list_team_members(
    team_id: int,
    limit: int = 25,
    cursor: str | None = None
) -> dict:
    """
    List members of a Dripify team.

    Requires the appropriate owner/manager permissions.
    """

    return await call(
        "GET",
        f"/teams/{team_id}/members",
        params=page(limit, cursor)
    )

@mcp.tool()
async def dripify_get_conversation(
    lead_id: int | None = None,
    linkedin_url: str | None = None,
    limit: int = 10
) -> dict:
    """
    Get the latest stored Dripify conversation for a lead.

    Conversation data comes from Dripify's
    'After LinkedIn reply is received' webhook.

    Provide either:
    - lead_id
    - linkedin_url
    """

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
            WHERE payload::text ILIKE %s
            ORDER BY received_at DESC
            LIMIT %s
            """,
            (
                "%" + escaped + "%",
                limit
            )
        ).fetchall()

    if not rows:
        return {
            "found": False,
            "lead_id": lead_id,
            "linkedin_url": linkedin_url,
            "message":
                "No stored conversation found for this lead."
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
        "lead_id": lead_id,
        "linkedin_url": linkedin_url,
        "events": events
    }
# ============================================================
# DATABASE
# ============================================================

def connection():
    """
    Connect to PostgreSQL used for webhook storage.
    """

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required for webhook storage"
        )

    return psycopg.connect(DATABASE_URL)


def init_db():
    """
    Create webhook event storage when DATABASE_URL exists.
    """

    if DATABASE_URL:

        with connection() as db:

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS dripify_webhook_events (
                    id BIGSERIAL PRIMARY KEY,
                    received_at TIMESTAMPTZ
                        NOT NULL DEFAULT now(),
                    payload JSONB NOT NULL
                )
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


# ============================================================
# WEBHOOK MCP TOOL
# ============================================================

@mcp.tool()
async def dripify_get_stored_webhook_events(
    lead_id: int | None = None,
    linkedin_url: str | None = None,
    limit: int = 25
) -> list[dict]:
    """
    Retrieve stored Dripify webhook snapshots.

    Requires either:
    - lead_id
    - linkedin_url
    """

    if not (
        lead_id is not None
        or linkedin_url
    ):
        raise ValueError(
            "Provide lead_id or linkedin_url"
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

    with connection() as db:

        rows = db.execute(
            """
            SELECT
                id,
                received_at,
                payload
            FROM dripify_webhook_events
            WHERE payload::text ILIKE %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                "%"
                + needle
                .replace("%", "\\%")
                .replace("_", "\\_")
                + "%",
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
# HTTP HEALTH CHECK
# ============================================================

async def health(
    request: Request
):
    """
    Render health-check endpoint.
    """

    return JSONResponse(
        {
            "ok": True,
            "service": "dripify-mcp"
        }
    )


# ============================================================
# DRIPIFY WEBHOOK RECEIVER
# ============================================================

async def webhook(
    request: Request
):
    """
    Receive Dripify webhook events.

    This endpoint still uses WEBHOOK_TOKEN because webhook
    requests are separate from MCP authentication.
    """

    if (
        not WEBHOOK_TOKEN
        or not secrets.compare_digest(
            request.path_params["token"],
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
                dripify_webhook_events(payload)
                VALUES (%s::jsonb)
                """,
                (
                    json.dumps(payload),
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

    return JSONResponse(
        {"stored": True}
    )


# ============================================================
# MCP HTTP APPLICATION
# ============================================================

mcp_app = mcp.http_app(
    path="/",
    stateless_http=True,
    json_response=True
)


# ============================================================
# APPLICATION LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app):

    if not API_KEY:
        raise RuntimeError(
            "Set DRIPIFY_API_KEY"
        )

    init_db()

    async with mcp_app.lifespan(app):
        yield


# ============================================================
# STARLETTE APPLICATION
# ============================================================

app = Starlette(
    routes=[
        Route(
            "/health",
            health
        ),

        Route(
            "/webhooks/dripify/{token}",
            webhook,
            methods=["POST"]
        ),

        Mount(
            "/mcp",
            app=mcp_app
        )
    ],
    lifespan=lifespan
)
