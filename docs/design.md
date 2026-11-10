# Sources, identity and privacy

The external interface answers questions about personal finances. It deliberately
breaks the initial nine-tool interface once, before deployment. No compatibility
aliases preserve provider-shaped error/category discovery. The new surface is in
[tools.md](tools.md); the frozen original is in [tools-before.md](tools-before.md).

## The source seam

`Source.refresh(db, public)` ingests a feed under the sync user's exclusive lock.
`Source.read_snapshot(db)` returns only normalized, SQL-aggregated rows for four
fixed public tables: sources, accounts, daily activity, holdings. `PlaidSource`
and `VenmoSource` implement both methods. Provider endpoints, products, cursors,
credentials, response schemas and errors are entirely inside the adapters/private
store. Adding an institution instantiates the existing connection adapter; adding
a different feed implements this interface. The new-source test injects another
adapter, discovers its wallet/activity and proves MCP names and input/output
schemas do not change. Public kinds are connection/file_import and data kinds
are balances/spending/holdings. Provider error strings map to owned health states.

The snapshot has no transaction table, transaction/provider IDs, raw descriptions,
notes, routing numbers or owner/contact fields. SQL groups by day, opaque account,
owned category and currency before any activity leaves the database layer. The
read-only module re-aggregates by requested periods/dimensions. It never imports a
source adapter or calls an upstream. User-visible taxonomy is owned here and has
coarse fallback codes, rather than exposing PFC identifiers or accepting free-text
upstream labels as categories. Positions are aggregates, not trade history.

## Stable account identity

The persistent private registry generates random `acct_...` identifiers; public
source identifiers are independent random `src_...` values. Neither encodes any
upstream id, number, name or credential. Keep this registry in private backups.
Deleting state creates new identities; they cannot be reconstructed from providers.

An unchanged private provider-account alias retains its public id. When aliases
change on re-link, match normalized institution name + type + subtype + SHA-256
fingerprint of normalized provider display name. Normalization case-folds and
collapses whitespace. Reuse only when there is exactly one historical candidate
and one incoming account with that key, and no other enabled active connection still
claims it. Otherwise allocate a fresh id; never merge by last four, numbers,
owner identity, approximate names or guessed balance. Changed names/institution
branding plus changed aliases cannot be matched automatically. Ambiguity needs
local owner review; no tool exposes raw matching text or edits identities.

Public labels use type/subtype plus a generated id suffix, never provider owner
text; the suffix is not an account-number mask. Provider names are fingerprinted
at ingestion and are not stored as plaintext. Closed/unavailable accounts retain
historical aliases and activity, have null balances, availability=historical and
no balance data kind. A re-link that resolves to the same identity does not expose
duplicate historical/current accounts. Old records without attribution become a
discoverable unassigned activity account; migration does not guess an account.
A replacement connection can reuse an unambiguous inactive account identity.
Re-enabling a superseded source that still claims the same active account makes
publication fail safely, preserving the previous snapshot until the owner chooses
one feed. Simultaneously enabled active independent connections are conservatively distinct; avoid
overlapping feeds. No automated cross-feed transaction deduplication is claimed.

`PLAIDMCP_DISABLED_SOURCES` is an optional comma-separated list of public source ids
in the sync user's private env: skip refresh and publication without deleting
state/identities or changing any MCP schema. Removing it restores the feed. Full
source deletion should be deliberate private maintenance; never remove a Trial
Item or private database to solve an identity ambiguity. Source ids follow local
feed records, not an institution brand; a replacement feed can have a new source
id while account ids remain stable.

## Publication and migrations

Private schema upgrades add account attribution, active state and identity maps;
existing cursors/tokens remain host-local. A migration fingerprints old account
labels before discarding them. Alias history remains private. Old transaction
records without account attribution are retained as unassigned; the Sandbox
checker backfills attribution from its authorized full-history read. No network
backfill is performed by the MCP. Re-run sync/publication before running the new
server against an old-format snapshot. Merchant migrations rebuild existing Items without Link or new credentials.

Identity registry commits precede atomic snapshot rename so public ids survive
publication retries. Each MCP call pins one snapshot transaction, including every
section of overview and both comparison periods. An unsuccessful source retains
its last successful aggregates and publishes a plain status; every response
includes all source timestamps and unreliable_sources. Age thresholds describe
local sync freshness, not market-price age or completeness of institution history.

Privacy config is documented in tools.md. Minimum-count folding affects query
results and category discovery; merchant groups are also folded before publication. It does not suppress private storage;
account discovery/balances are not suppressed. It cannot prevent inference from
multiple filters and is not an anonymization guarantee. Last-four display must be
explicitly enabled in both sync and server env; the default snapshot stores none.

## Merchant publication

The private transaction store adds only a verified normalized business label. No
person/unknown counterparty, username or note is retained. Missing business names
may use conservative curated descriptor patterns; raw descriptions are discarded
before persistence. Venmo never supplies merchant labels. A separate `merchants`
snapshot table contains folded, precomputed calendar windows and merchant-level
recurrence statistics. It has no day, provider/account id or individual amount.
The SQL publisher groups across enabled sources and folds counts below max(2,
min_group_count); a singleton Other has null amounts. Fixed windows limit accidental
isolation, but overlapping aggregates are not differential privacy and can permit
inference. Business recognition is conservative and may omit genuine businesses.
