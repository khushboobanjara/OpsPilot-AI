# Guest access for a public demo

Put one link on LinkedIn and let anyone try the product safely: no sign-up, no shared password, sample data only, nothing they do can change it.

## What a visitor gets

Open the site, click **Try the live demo (read-only)**. They can:

- browse the dashboard, invoices, vendors, model versions and metrics (sample data),
- download the invoice CSV,
- ask the AI assistant questions about the data (`/chat`),
- search and ask questions about the sample documents (`/rag/search`, and the assistant's `search_documents` tool; only documents marked for everyone are visible).

They cannot, by design: create, edit or delete users, change settings, approve or reject invoices, triage emails, upload, delete or re-index documents, read the audit log, read the review queue, or call the MCP server.

## How it is built

| Piece | Where | What it does |
| --- | --- | --- |
| `POST /auth/guest` | `app/auth_routes.py` | Issues a signed, HttpOnly, SameSite=Strict cookie. No user row is created. Rate limited per IP and globally. Writes a `guest_login` audit row. |
| Guest principal | `app/auth.py` | The role is decided on the server and is always `viewer`. The token carries no role. The name must match `guest-` plus 12 hex characters, the epoch must match, and the token expires (default 60 minutes). |
| Read-only gate | `app/auth.py` `require_roles` | Every request that is not GET/HEAD/OPTIONS is refused for a guest, except `POST /chat` and `POST /rag/search`. It runs inside the shared authorization dependency, so a new route added later is protected even if it forgets to ask for a stricter role. |
| Budgets | `app/guest.py` | Per session: 15 assistant questions and 30 searches. All guests together: 500 assistant questions per UTC day (a hard ceiling on Groq cost). |
| MCP | `app/mcp_server/auth.py` | A guest session is never accepted as an MCP credential. |
| Sample data | `app/demo.py` | On start, loads the sample invoices into an empty database and indexes `docs/sample_documents` for everyone. It never wipes anything. |

Existing authentication, roles, the admin area, API-key ingestion and the legacy `OPSPILOT_DEMO_MODE` are unchanged. Guest access is off unless `OPSPILOT_GUEST_ENABLED=true`.

## Environment variables

Required for a public demo (set them in `.env` or your host's secret store):

| Variable | Value | Notes |
| --- | --- | --- |
| `OPSPILOT_ENV` | `production` | Refuses to start without auth, a strong secret and secure cookies. |
| `OPSPILOT_AUTH_ENABLED` | `true` | Guest access needs it. |
| `OPSPILOT_JWT_SECRET` | 32+ random characters | `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Secret. |
| `OPSPILOT_COOKIE_SECURE` | `true` | Serve over HTTPS. |
| `OPSPILOT_GUEST_ENABLED` | `true` | Turns the button and endpoint on. |
| `OPSPILOT_DB_*` | a dedicated database | Sample data only. Never the database that holds real invoices. |
| `OPSPILOT_BOOTSTRAP_ADMIN_USER` / `_PASSWORD` | your own admin | Created once if there are no users. Password of 10+ characters, chosen by you; remove both lines after the first start. Guests can never use it. |

For the assistant and document Q&A (optional, recommended for the demo):

| Variable | Value |
| --- | --- |
| `OPSPILOT_CHAT_ENABLED` | `true` |
| `OPSPILOT_GROQ_API_KEY` | your Groq key (secret, stays on the server). Use a key with a spending limit. |
| `OPSPILOT_RAG_ENABLED` | `true` (first start downloads the embedding model once) |

Guest tuning (all optional; defaults shown in `.env.example`): `OPSPILOT_GUEST_SESSION_MINUTES`, `OPSPILOT_GUEST_EPOCH`, `OPSPILOT_GUEST_LOGINS_PER_MINUTE_PER_IP`, `OPSPILOT_GUEST_LOGINS_PER_MINUTE_GLOBAL`, `OPSPILOT_GUEST_CHAT_PER_SESSION`, `OPSPILOT_GUEST_SEARCH_PER_SESSION`, `OPSPILOT_GUEST_CHAT_PER_DAY_GLOBAL`, `OPSPILOT_GUEST_SEED_ON_START`, `OPSPILOT_GUEST_SAMPLE_DOCS_DIR`.

The app refuses to start guest access if auth is off, if `OPSPILOT_DEMO_MODE` is on (that mode wipes users), if `OPSPILOT_IMAP_*` is set (a real mailbox), or if a limit is out of range.

## Deployment steps

1. **Use a separate deployment and database** for the public demo. Guests read whatever is in the database, so it must hold sample data only. Do not run the mailbox poller or folder watcher against it.
2. Generate the JWT secret and choose an admin password for yourself. Put the settings above in `.env` (Docker Compose passes them to the container) or in your host's environment settings. Do not commit `.env`.
3. Start it:
   - Docker: `docker compose up -d --build`.
   - Without Docker: `pip install -r requirements.txt` then `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers`.
4. Put HTTPS in front (Caddy, nginx, Cloudflare or your platform's TLS). Run uvicorn with `--proxy-headers` so the per-IP limit sees real client IPs, and keep a request-size limit at the proxy.
5. **Run one worker.** The guest budgets are in memory per process. With several workers each has its own counters.
6. Check: `curl https://your-host/health` should show `"guest_access": true`. Open the site in a private window, click the demo button, ask a question, search a document. Try `POST /users` from the browser console: it must answer 403.
7. Sign in as your own admin to review the `guest_login` audit rows and chat usage.

## Operating it

- **End every open guest session now:** set `OPSPILOT_GUEST_EPOCH` to the next number and restart.
- **Turn guest access off:** set `OPSPILOT_GUEST_ENABLED=false` and restart. Open guest sessions stop working immediately.
- **Cost control:** lower `OPSPILOT_GUEST_CHAT_PER_DAY_GLOBAL`, and set a spending limit on the Groq key.
- **Reset the sample data:** point to a fresh database (guest access never writes business data, so there is normally nothing to reset).

## Things to know (stated plainly)

- Assistant questions are sent to Groq. The guest banner tells visitors not to type personal or confidential information.
- Guest sessions are anonymous. The audit log records a random `guest-…` name, not an IP address.
- Budgets are in memory: a restart resets them. The per-IP session limit and the daily ceiling are what bound abuse; add a rate limit at the proxy as well for a link that might go viral.
- A guest can read every sample invoice, including the email text the sample data contains. That is the point of the demo and the reason the database must hold sample data only.
- The older `OPSPILOT_DEMO_MODE` is kept for compatibility. With accounts enabled, its hourly reset drops every table (including users), so prefer guest access for anything public.

## Tests

`tests/test_guest.py` (run with `python -m pytest tests/test_guest.py`, or the whole suite). It covers the switch and startup checks, the session shape, forged/expired/stale tokens, a walk over every route in the app proving each write is refused for a guest, the central gate on a deliberately over-permissive test route, MCP rejection, assistant and document Q&A with budgets, role-restricted documents, that real users and admin keep working, and the sample-data seeding.
