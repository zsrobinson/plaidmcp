# Plaid MCP

A small, read-only aggregate view of personal money for Claude/Duet. Institutions
and upstream connections are implementation details, not the tool interface.
Start with `overview`; discover accounts/categories/sources before refining a
question. [Exact tools and sign conventions](docs/tools.md) ·
[Surface before this design pass](docs/tools-before.md) ·
[Identity and source rules](docs/design.md) · [Future Pi runbook](deploy/README.md).

## Layout

- `server.py`, `queries.py`, `access.py`: eight generic FastMCP tools, concurrent
  read-only SQLite queries, loopback Streamable HTTP and Access JWT verification.
- `sources.py`, `taxonomy.py`: common Source interface, two adapters, public schema
  and owned financial category taxonomy. Only normalized SQL aggregates cross this interface.
- `store.py`, `sync.py`, `plaid_client.py`: private ingestion, identities, sync,
  endpoint allowlist and atomic publication. The server imports none of them.
- `venmo_import.py`: offline monthly statement fallback, with parse-time stripping.
- `merchants.py`, `production_audit.py`: conservative business eligibility, folded merchant windows, subscription candidates and gated privacy checks.
- `link.py`: owner-only loopback Link helper; never expose through a tunnel.
- `tests/`, `smoke.py`, `sandbox_check.py`: offline and explicit Sandbox validation.
- `deploy/`: generic deployment contract and temporary owner-session template.

Matches a lightweight Python MCP pattern's Python 3.12, official FastMCP/Uvicorn, PyJWT, `uv.lock` and
hash-pinned wheels. No Docker, browser runtime, pandas or persistent Plaid SDK.
SQLite connections are per call, read-only and closed; blocking work runs off the
ASGI event loop. Snapshot replacement is atomic, and each call pins one read
transaction, including multi-section overview and comparison results. No payload,
credential, assertion or raw exception logging. HTTP access logs are disabled.
Initial combined HTTP smoke peak RSS was ~74 MiB on Ubuntu; ARM memory usage and
live institution linking remain unverified. Staged units retain 192 MiB caps.

## Local verification

```sh
uv sync --locked
.venv/bin/pytest -q
.venv/bin/python smoke.py
# Review deploy/README.md before any deployment.
# Explicit live Sandbox only, after private credentials exist:
.venv/bin/python sandbox_check.py --resume
```

Offline fixtures are invented. Tests cover privacy, discovery, re-link identity,
ambiguity, historical activity, another source without tool/schema changes,
comparisons/averages, overview median, freshness, small-group folding, concurrent
MCP calls, private CSV behavior and Access validation. The integration checker
reuses the most recent isolated run under `~/.local/state/plaidmcp-sandbox`, never
production. Omitting `--resume` creates a new throwaway run and fake Sandbox Item.
Credentials remain only in `~/.config/plaidmcp/sandbox.env`, mode 0600 in 0700;
the checker parses it as data and requires exact Sandbox configuration. Never
paste values into commands, chat, Git or logs. Secret-delivery files were shredded.
The separate temporary MCP process inherits no `PLAID_*` variables and is always
stopped. Public SQLite bytes, decoded fields and responses are scanned against
real source strings retained only in checker memory. Short source strings use
whole-token checks to avoid accidental substrings in security names/types.

Live verification: seven tools, nine returned PFC categories requested as v2,
13 holdings and nine transfers; standard data has no peer rows, so a real-response
shape probe checks peer rules without claiming real Venmo support. Plaid does not
echo a PFC version flag in these responses. Sandbox totals are fake finances.
[Plaid Sandbox endpoint reference](https://plaid.com/docs/api/sandbox/).

## Private ingestion

Plaid requests only transactions and investments in Link; no auth, identity,
transfer, payments or other products. Nine normal read-only endpoints are allowed:
`/link/token/create`, `/item/public_token/exchange`, `/accounts/get`,
`/accounts/balance/get` (disabled to avoid paid refresh), `/transactions/sync`,
`/transactions/recurring/get` (optional, with cadence fallback),
`/investments/holdings/get`, `/item/get`, `/institutions/get_by_id`.
`/sandbox/public_token/create` is an explicit exception only for a Sandbox-initialized
client, exact Sandbox host and explicit `PLAID_ENV=sandbox`. Production, development,
missing env and non-approved products fail closed before HTTP. Cached balances
come from `/accounts/get`; sync requests PFCv2 and commits pages/cursor together,
restarting pagination from its original cursor on mutation. Transactions and
holdings may share one Item. Pending transactions are excluded from publication.

Private credentials/state are outside Git. Two future system users separate sync
secrets/raw stripped records from the MCP's aggregate-only snapshot. Provider
account names are fingerprinted for matching and replaced with generated labels;
descriptions, person counterparties, routing/owner/contact fields are not persisted.
Only verified normalized business names or conservative public-brand fallback labels are retained privately for merchant aggregates.
No account numbers except optional last four; no financial writes exist. Link's
CSRF/same-host helper binds 127.0.0.1:8768, warns on duplicate institutions and
supports `--update` without creating a new Item. Production Trial's ten lifetime
Items are not recovered by deletion. Never delete private state to bypass guards.

## Venmo CSV fallback

Personal Venmo via Plaid is unconfirmed. Use only a verified institution id and
`--product transactions` if it becomes available; no Venmo scraper/API/sign-in.
Venmo documents downloadable personal statements in its
[official statement guide](https://help.venmo.com/cs/articles/transaction-history-vhel281).
No actual export has been seen. Transfer a complete month into the private inbox
as `.part`, then atomically rename to `.csv` after upload/permissions are complete.
Future inbox: `/var/lib/plaidmcp-private/import/venmo`, 0700, sync user only.
The existing sync timer imports it; standalone `venmo_import.py` needs no Plaid keys.

BOM/preamble/index tolerant, with US/ISO dates, header aliases and optional JSON
column mapping. Only date, integer cents, in/out direction and fixed peer/purchase/
bank-transfer/fee categories enter the private database. Free text, names, usernames,
raw IDs and filenames are discarded before writes. Each complete month replaces
its prior sanitized rows transactionally; identical genuine rows keep multiplicity.
Publication/cleanup failures are idempotently retryable; affected raw files remain.
Unknown types/statuses, ambiguous/conflicting directions, multi-month files and
oversized input fail with fixed messages. Empty months need YYYY-MM in the filename.

`sync.env.example` documents settings: `PLAIDMCP_VENMO_IMPORT_DIR`, `PLAIDMCP_VENMO_COLUMN_MAP`,
`PLAIDMCP_VENMO_PROCESSED=delete` (default, after successful publication), or opt-in `move`
with a private same-filesystem `PLAIDMCP_VENMO_ARCHIVE`. Unlink is not secure erasure.
`PLAIDMCP_VENMO_FEE_MODE=reject` refuses nonzero fee columns until explicitly set to
`separate` or `included` after checking the real export's arithmetic. Standalone
fee rows work in every mode. Never expose or commit raw CSV/archive contents.

Choose one Venmo feed: source-overlap guards prevent CSV plus a known Venmo Plaid
connection from counting the same history twice. PFC structured peer codes and
Venmo's other-transfer codes are normalized without reading any notes or names;
ambiguous bank classifications stay excluded. This remains a heuristic, not a
claim that all personal-payment data is classified correctly.

### Choosing an institution in the local Link helper

`link.py --product transactions --institution-name "Example Bank"` allows selection
in Plaid's native search without requiring a developer institution ID. The optional
name hint refuses wrong-bank selection before authentication; `--institution ID`
still supports verified IDs and is required for update mode. Already-linked IDs
are blocked in the browser and before exchange; named duplicate feeds are rejected
before token creation. The helper never asks for extra products. Close it after
linking; do not expose it through a public tunnel. Avoid creating duplicate Trial
Items: removing an Item never restores its lifetime slot.

## Merchant totals and subscription candidates

Use `merchant_totals(window="last_6_months", recurring_only=true)` for subscription
candidates. Fixed UTC calendar windows are published at sync; there is no arbitrary
day, account or merchant selector that could isolate a purchase. See
[merchant privacy and cadence rules](docs/tools.md#merchant_totals).

Provider business names use structured identity evidence and conservative normalization.
Missing names may use a curated business-only descriptor fallback during private sync.
All Venmo rows are excluded from merchant naming; people and transfer rails never
become merchants. Uncertain non-Venmo spending joins Other without any text. Merchant rows are folded in SQL **before publication**.
Every named merchant/window needs at least max(2, min_group_count) positive charges.
Other with fewer charges has null amounts; no merchant dates or individual charges
are stored in the aggregate snapshot. The median charge is a multi-charge statistic.

`PLAIDMCP_BACKFILL_MERCHANTS=true` on a one-shot sync rebuilds transaction rows from
an empty cursor, transactionally per existing Item. Failure retains the old rows
and cursor. It never creates an Item or invokes Link, and never changes a private
credential. It only re-reads history Plaid already holds. `days_requested` affects
initialization only; existing initialized Items cannot expand their initial history
by changing it. [Plaid's transaction history documentation](https://plaid.com/docs/transactions/).

`production_audit.py` runs a Production-only sync to a candidate snapshot, audits
its schema/fields/SQLite bytes and every tool against source strings retained only
in memory, then atomically publishes. Failures report only phase/type/field context.
`deploy/production_session.py` is a temporary sudo-owner session template; concrete
operator restrictions, source pins, maintenance/install scripts and host settings
belong in the private deployment repository. It adds no standing privilege.

## Conservative descriptor fallback and internal transfers

`descriptors.py` classifies raw bank text only inside the sync/audit process. It
strips numbers, dates, phones, references, URLs and known/structured location
suffixes; it collapses repeated/truncated fragments. Fallback promotes only a
small generic public-brand/product vocabulary, never arbitrary cleaned text.
Descriptors containing personal-payment rails, honorific/name patterns, email
addresses or unrecognized extra words are rejected. Missing business evidence
means Other. Provider location-only labels are rejected when they match structured
location fields. Conservative rules may omit genuine businesses and cannot prove
that every upstream classification is correct. No raw descriptor is persisted,
even in private state; only the normalized label/provenance is retained privately.

`merchant_source` distinguishes provider_name, descriptor_fallback, mixed and
unknown. Unknown posted non-Venmo spending is now included in Other, rather than
omitted. Its amount is still null below the two-purchase threshold. This expands
coverage, so Other totals from older releases are not directly comparable.

`transfers.py` matches only mutually unique, opposite exact-cent amounts in the
same currency within three calendar days between distinct known linked account
identities. Both legs must have structured/explicit bank-transfer evidence;
payroll and peers cannot match just because an amount happens to be equal.
Ambiguous transfers remain unmatched. Matches are recomputed on every publish,
respect disabled sources, and never expose transaction or pair identifiers.
Cash flow splits payroll, interest, other income, peer receipts, refunds and
internal-transfer amounts. Internal moves are always excluded from income,
spending and net flow; refunds remain offsets to spending, not duplicated income.

## Telemetry

Enabled by default; set `MCP_TELEMETRY=0` (or `false`/`off`) to disable.
Stderr receives one `mcp_call ` prefix followed by a JSON object per dispatched
call, and one `mcp_start ` record when the MCP lifespan is ready:

```json
{"event":"mcp_call","v":1,"ts":"<UTC ISO>","service":"plaidmcp","tool":"<registered name>","outcome":"ok|tool_error|error|timeout|refused","error_class":null,"total_ms":0.0,"phase_ms":{"serialize":0.0},"req_bytes":0,"resp_bytes":0}
{"event":"mcp_start","v":1,"service":"plaidmcp","version":"<package version>","startup_ms":0.0}
```

Durations use a monotonic clock. Startup includes process imports on Linux
(import-to-ready fallback elsewhere). Byte counts measure UTF-8 JSON MCP params
and normalized results, excluding the JSON-RPC envelope. Total time includes
result normalization and a serialization pass; transport delivery is excluded.
Arguments, results and exception messages are never emitted. Unknown tool names
become `unknown`; unknown exception types become `exception`. Returned MCP error
results are `tool_error`; raised exceptions are `error`, with timeouts and
permission failures classified separately. Error classes are fixed:
`ValueError`, `TypeError`, `OSError`, `TimeoutError`, `PermissionError`, `Busy`,
`NotFound`, `ToolError`, `CancelledError`, or `exception` (null on success).
Only measured phases appear; nested occurrences of the same phase count once.

Call phases: `db_open`, `query`, `serialize`.

Sync runs also emit `mcp_sync {"event":"mcp_sync","v":1,"ts":"<UTC ISO>","outcome":"ok|error|timeout|refused","error_class":null,"total_ms":0.0,"phase_ms":{},"pages":0,"retries":0,"items_count":0}`.
Phases are `fetch`, `store`, `publish`. Counts are attempted credential items,
received transaction pages (including retried pages), and pagination retries.
Handled per-item failures mark the run as failed without changing sync behavior.

New transaction Items request 730 days of history by default. Use Link's
`--days-requested N` to choose 30–730 days. Institutions may return less history.
Plaid ignores this initialization setting for already-initialized Items; update
mode does not send it and cannot extend existing history.
