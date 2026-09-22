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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

BASE = "https://api.dripify.com/v1/open-api"
API_KEY = os.environ.get("DRIPIFY_API_KEY", "")
MCP_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
mcp = FastMCP("Dripify Open API")


def page(limit: int, cursor: str | None) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return {"limit": limit, **({"cursor": cursor} if cursor else {})}


async def call(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
    if not API_KEY:
        raise RuntimeError("DRIPIFY_API_KEY is missing")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.request(method, BASE + path, headers={"X-Api-Key": API_KEY}, params=params, json=body)
    if response.is_error:
        # Do not echo upstream body, which may contain personal data.
        raise RuntimeError(f"Dripify returned HTTP {response.status_code}; retry-after={response.headers.get('Retry-After', 'n/a')}")
    return response.json()


@mcp.tool()
async def dripify_list_campaigns(limit: int = 25, cursor: str | None = None) -> dict:
    """List campaigns; pass nextCursor to fetch another page."""
    return await call("GET", "/campaigns", params=page(limit, cursor))


@mcp.tool()
async def dripify_list_campaign_lead_lists(campaign_id: int, limit: int = 25, cursor: str | None = None) -> dict:
    """List lead lists within a campaign."""
    return await call("GET", f"/campaigns/{campaign_id}/lead-lists", params=page(limit, cursor))


@mcp.tool()
async def dripify_get_campaign_statistics(campaign_id: int) -> dict:
    """Get campaign counts, status breakdown, acceptance and reply rates."""
    return await call("GET", f"/campaigns/{campaign_id}/statistics")


@mcp.tool()
async def dripify_list_leads(limit: int = 25, cursor: str | None = None, campaign_id: int | None = None, lead_list_id: int | None = None, status: str | None = None) -> dict:
    """List leads, optionally scoped by campaign, list, or processing status. lead_list_id takes precedence."""
    allowed = {"READY_FOR_PROCESSING", "PROCESSING_ENDED", "PROCESS_CORRUPTED", "PROCESSING_PAUSED"}
    if status is not None and status not in allowed:
        raise ValueError(f"status must be one of {sorted(allowed)}")
    params = page(limit, cursor)
    if campaign_id is not None: params["campaignId"] = campaign_id
    if lead_list_id is not None: params["leadListId"] = lead_list_id
    if status is not None: params["status"] = status
    return await call("GET", "/leads", params=params)


@mcp.tool()
async def dripify_get_lead(lead_id: int) -> dict:
    """Get details and campaign memberships for a lead's numeric Dripify ID."""
    return await call("GET", f"/leads/{lead_id}")


@mcp.tool()
async def dripify_get_lead_activity(lead_id: int, limit: int = 25, cursor: str | None = None) -> dict:
    """Get paginated lead timeline events (not message bodies)."""
    return await call("GET", f"/leads/{lead_id}/activity", params=page(limit, cursor))


@mcp.tool()
async def dripify_search_leads(email: str | None = None, linkedin_url: str | None = None) -> list:
    """Look up leads by email or complete LinkedIn URL. At least one value required."""
    if not email and not linkedin_url:
        raise ValueError("Provide email or linkedin_url")
    body = {**({"email": email} if email else {}), **({"linkedinUrl": linkedin_url} if linkedin_url else {})}
    return await call("POST", "/leads/search", body=body)


@mcp.tool()
async def dripify_upload_leads(campaign_id: int, leads: list[dict[str, str]], name: str | None = None) -> dict:
    """Create a NEW lead list in a campaign. Each lead has exactly one linkedinUrl or publicId. This changes Dripify data."""
    if not 1 <= len(leads) <= 1000:
        raise ValueError("leads must contain 1 to 1000 entries")
    if name is not None and len(name) > 120:
        raise ValueError("name must be at most 120 characters")
    for lead in leads:
        if not isinstance(lead, dict) or set(lead) not in ({"linkedinUrl"}, {"publicId"}) or not next(iter(lead.values())):
            raise ValueError("Each lead must contain exactly one nonempty linkedinUrl or publicId")
    body = {"leads": leads, **({"name": name} if name else {})}
    return await call("POST", f"/campaigns/{campaign_id}/leads", body=body)


@mcp.tool()
async def dripify_list_teams(limit: int = 25, cursor: str | None = None) -> dict:
    """List teams the API key owner belongs to, including inlined statistics."""
    return await call("GET", "/teams", params=page(limit, cursor))


@mcp.tool()
async def dripify_list_team_members(team_id: int, limit: int = 25, cursor: str | None = None) -> dict:
    """List team members and inlined statistics; requires owner or manager access."""
    return await call("GET", f"/teams/{team_id}/members", params=page(limit, cursor))


def connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for webhook storage")
    return psycopg.connect(DATABASE_URL)


def init_db():
    if DATABASE_URL:
        with connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dripify_webhook_events (
                id BIGSERIAL PRIMARY KEY, received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                payload JSONB NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS dripify_events_payload_gin ON dripify_webhook_events USING GIN(payload)")


@mcp.tool()
async def dripify_get_stored_webhook_events(lead_id: int | None = None, linkedin_url: str | None = None, limit: int = 25) -> list[dict]:
    """Retrieve raw Dripify webhook snapshots. Requires lead_id or LinkedIn URL; exact JSON field mapping depends on webhook configuration."""
    if not (lead_id is not None or linkedin_url):
        raise ValueError("Provide lead_id or linkedin_url")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    # Webhook schemas vary. Search exact string values in JSON recursively, without guessing conversation structure.
    needle = str(lead_id) if lead_id is not None else linkedin_url
    with connection() as db:
        rows = db.execute("""SELECT id, received_at, payload FROM dripify_webhook_events
            WHERE payload::text ILIKE %s ORDER BY id DESC LIMIT %s""", ("%" + needle.replace("%", "\\%").replace("_", "\\_") + "%", limit)).fetchall()
    return [{"id": row[0], "received_at": row[1].isoformat(), "payload": row[2]} for row in rows]


async def health(request: Request):
    return JSONResponse({"ok": True})


async def webhook(request: Request):
    if not WEBHOOK_TOKEN or not secrets.compare_digest(request.path_params["token"], WEBHOOK_TOKEN):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not DATABASE_URL:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    raw = await request.body()
    if len(raw) > 1_000_000:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(raw)
        with connection() as db:
            db.execute("INSERT INTO dripify_webhook_events(payload) VALUES (%s::jsonb)", (json.dumps(payload),))
    except (ValueError, psycopg.Error):
        return JSONResponse({"error": "invalid payload or database error"}, status_code=400)
    return JSONResponse({"stored": True})


class BearerGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            expected = "Bearer " + MCP_TOKEN
            if not MCP_TOKEN or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)


@asynccontextmanager
async def lifespan(app):
    if not MCP_TOKEN or not API_KEY:
        raise RuntimeError("Set DRIPIFY_API_KEY and MCP_BEARER_TOKEN")
    init_db()
    async with mcp_app.lifespan(app):
        yield


app = Starlette(routes=[Route("/health", health), Route("/webhooks/dripify/{token}", webhook, methods=["POST"]), Mount("/mcp", app=mcp_app)], lifespan=lifespan)
app.add_middleware(BearerGuard)
