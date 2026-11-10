# Deployment contract

Run `server.py` as an unprivileged read-only identity with only the aggregate SQLite
snapshot available. Run `sync.py` under a separate identity with private credentials
and state, using `sync.env.example` as a placeholder template. Keep env files mode
0600 in mode-0700 directories. Never give the server the sync credentials or raw
statement inbox. Concrete host units, ingress rules and account-specific settings
belong in a separate private deployment repository.

The server binds **127.0.0.1:8767** and serves Streamable HTTP at `/mcp`.
Set `PLAIDMCP_PUBLIC_HOST=plaidmcp.example.com` to allow the exact public Host and
Origin used by a trusted proxy. Configure both ACCESS_TEAM_DOMAIN and ACCESS_AUD
for origin JWT validation; production exposure requires Access assertions. Set
`PLAIDMCP_REQUIRE_ACCESS=true` to fail startup when validation is unconfigured.
Cloudflare Access may provide Managed OAuth, with a restrictive owner-only policy.
The public service should expose only `/mcp`, never the owner linking helper.

The Link helper is a temporary process bound only to **127.0.0.1:8768**; reach it
through an SSH tunnel to your own host. It must not be exposed by a public tunnel.
Use Sandbox initially. A deployment can run without Plaid credentials: sync then
publishes an empty snapshot and imports any explicitly supplied CSV statements,
without contacting Plaid. `PLAIDMCP_SANDBOX_ONLY=true` hard-fails non-Sandbox sync
or Link configuration before any upstream request. Do not enable production sync
without a separate deliberate configuration review.

Private runtime paths may use `/var/lib/plaidmcp-private` for sync state and
`/var/lib/plaidmcp/aggregate.sqlite` for the readable snapshot; choose and protect
paths appropriate to your host. Do not install this source checkout over an
existing running application. Pin the source revision and hash-locked wheels,
validate locally, then switch the new service on explicitly. Keep rollback copies
of changed deployment files, preserve existing services/routes, and fail closed
until origin authentication is ready.
