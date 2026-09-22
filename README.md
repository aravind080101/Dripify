# Dripify MCP for Render

A remote Streamable HTTP MCP server for the **documented** Dripify Open API. Includes 10 tools: campaign listing, campaign lead lists, campaign statistics, lead listing/detail/activity/search/upload, team listing, team members; plus one raw webhook archive read tool. Cursors are passed through as `nextCursor`. Team statistics are inlined in the team/member responses.

## Deploy

1. Ensure your Dripify subscription has Open API access (Advanced or Pro). Generate an API key under Dripify Settings → Integrations.
2. Push this folder as the root of a **private** GitHub repository. In Render, create a Blueprint from the repository (`render.yaml`). Review PostgreSQL and web-service plan pricing before confirming.
3. Set `DRIPIFY_API_KEY`, `MCP_BEARER_TOKEN`, and `WEBHOOK_TOKEN` in Render. Generate independent secrets using `python -c 'import secrets; print(secrets.token_urlsafe(48))'`. Render links `DATABASE_URL` to its managed PostgreSQL database. Never commit `.env`.
4. Check `https://YOUR-SERVICE.onrender.com/health`. Configure the MCP client for Streamable HTTP at `https://YOUR-SERVICE.onrender.com/mcp/` with header `Authorization: Bearer YOUR_MCP_BEARER_TOKEN`.
5. Optional: In each Dripify campaign's webhook settings, configure the **After LinkedIn reply is received** trigger to POST to `https://YOUR-SERVICE.onrender.com/webhooks/dripify/YOUR_WEBHOOK_TOKEN`. Treat this URL as a secret. Test with a real Dripify webhook and inspect the saved raw snapshot using `dripify_get_stored_webhook_events`.

## Local run

```bash
cp .env.example .env
# Fill in .env, then:
docker build -t dripify-mcp .
docker run --env-file .env -p 10000:10000 dripify-mcp
```

## API boundaries

`dripify_upload_leads` creates a **new lead list** per call (1–1000 identifiers), and does not append to an existing list. Collection starts when the campaign is active in Dripify. The public API does not provide campaign activation, campaign detail by ID, individual team member lookup, direct LinkedIn message sending, or full inbox history. The activity endpoint returns timeline events without message text. Reply webhooks may include inbox conversations, but only snapshots from configured triggers onward; the archive stores the original JSON without assuming a fixed schema. The archive lookup uses text search and can produce false positives for numeric IDs; inspect lead identity before acting on the returned payload. The webhook URL uses a secret path token because Dripify's help page does not document a signed delivery scheme; keep it confidential and rotate it if exposed. Postgres webhook data includes personal information; restrict access and retention as appropriate.

Dripify documents **60 requests/minute and 5,000/day** per API key. This server returns 429 errors and `Retry-After` to clients; clients should throttle pagination and retries.

Sources: [Open API reference](https://api.dripify.com/), [Open API access](https://help.dripify.com/en/articles/16664719-open-api), [Webhook overview](https://help.dripify.com/en/articles/16772911-dripify-integrations).
