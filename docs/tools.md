# plaidmcp tools

Eight generic, read-only tools answer financial questions independently of institutions
and providers. Start with `overview`. Use `list_accounts`, `list_sources` and
`list_categories` to discover supported filters/capabilities instead of guessing.
Adding/removing a source changes records, not names or schemas. No tool accepts
provider identifiers, credentials, product names or transaction selectors.

## Common response and conventions

Every response is `{as_of, sources, unreliable_sources, data}`:

- `as_of`: object mapping opaque source id to ISO sync timestamp or null.
- `sources`: array of `{id, institution, kind, as_of, status, data_kinds, freshness}`.
  `kind` is connection/file_import; `data_kinds` contains balances/spending/holdings.
  Status is healthy / needs re-link / waiting for data / temporarily unavailable /
  source overlap needs review. Freshness is current / outdated / not synced.
- `unreliable_sources`: source records that are non-healthy or older than the
  configured freshness threshold. This list is conservative and includes sources
  outside a requested filter, so an assistant can explain broader coverage gaps.
- `data`: the tool-specific structure below. Absent measurements are null; empty
  arrays mean no observed matching data, not a guarantee of no financial activity.

Amounts are JSON numbers in **decimal units of the row's currency**: USD means
dollars, not cents. Currencies are never added together or converted. Spending is
positive, refunds negative; income/receipts are positive. Liabilities/credit owed
are positive amounts owed (a genuine credit balance can be negative). Net worth
is assets minus liabilities. Cash-flow net is income minus spending plus included
bank-transfer receipts minus included bank-transfer outflows. Balances and market
values are cached institution figures and can predate a successful sync.

Dates are YYYY-MM-DD; start inclusive, end exclusive, default UTC month-to-date
through today's date. Month groups are YYYY-MM; week groups start Monday and use
the Monday's date. Amount and count fields are SQL aggregates, never transactions.
No payment note, person counterparty, provider account id, routing/full account
number or owner/contact information is available. Last four is opt-in only.

Opaque account ids and generated labels are described in [design.md](design.md).
Filters are exact, case-sensitive discovered ids/names/category codes. Unknown
valid ids/names/codes return empty results; malformed ids/dates/groupings fail.

## Output structures

### overview

No inputs. `data`:

- `balances`: the balance_summary data structure, grouped by type.
- `start`, `end`: UTC month-to-date range.
- `month_to_date`, `last_month`, `last_month_same_elapsed_days`: arrays of activity
  totals per currency. Last month is the full prior calendar month; elapsed days
  matches today's day count, capped at the prior month's length.
- `typical_month`: `{currency, observed_months, median_monthly_spending}` array.
  Median uses the six prior full calendar months and is null unless all six have
  observed activity. Missing history is not silently replaced with zero.
- `spending_comparisons`: `{currency, month_to_date, last_month,
  last_month_same_elapsed_days, typical_month}` array. Each baseline is
  `{spending, delta, percent_change}`; delta=current-baseline, percent uses the
  absolute baseline and is null for zero/missing baseline. Comparisons to a full
  month or median are not projections of an unfinished current month.
- `top_categories`: up to five current category group rows, ranked by spending.

### list_accounts

`data` array fields: `id`, `display_name`, `institution`, `type`, `subtype`,
`currency`, `current`, `available`, `credit_limit`, `data_kinds`, `availability`,
`source_id`, `source_kind`, `as_of`, `freshness`, `status`. `last_four` is absent by
default and nullable when opted in. Availability is available/historical/unassigned.
Historical accounts have null balances and no balance data kind; their activity
remains queryable. A statement wallet can provide spending without any balance;
its unknown value makes net worth partial rather than inventing a zero.

### list_sources

`data` is the source-record array defined in the common response. Capability is
separate from observed rows: an account with no holdings/activity yet can still
provide that data kind. No upstream error codes or product names are returned.

### list_categories

`data` array fields: `category`, `label`, `category_detailed`, `detailed_label`,
`count`, `has_data`, `first_date`, `last_date`. By default only observed categories
are returned. With only_with_data=false the entire owned taxonomy is shown, with
zero counts/null coverage for absent or suppressed categories. Counts are posted
items, not distinct merchants. Small categories fold into `__other__`; no amounts
or individual dates are exposed by category discovery.

### cash_flow

Inputs: group_by plus the generic date/account/institution/category/transfer
filters and optional compare_start/compare_end; exact schema follows below.
`data` is `{start, end, group_by, groups, totals, comparison}`.

- Each group: `group`, `label`, `currency`, `income`, `spending`, `transfers_in`,
  `transfers_out`, `net`, `count`, `average_per_month`, `average_per_week`.
  Group is the category code, detailed code, month, Monday, opaque account id or
  institution. `__other__`/Other (small groups) is the suppression bucket.
- Totals have the same metrics without group/label, aggregated per currency.
- Averages are **spending** divided by calendar-month equivalents or seven-day
  weeks. A partial month contributes days-in-range / calendar-days-in-that-month.
  Month/week group averages use that bucket's clipped range; other dimensions and
  totals use the whole requested period. Zero-activity days are included in the
  denominator; this is arithmetic normalization, not a forecast.
- Comparison is null unless both comparison dates are supplied. Otherwise it has
  `{start,end,group_by,groups,totals,deltas}` for period B. Deltas compare period A
  totals to period B per currency: `currency`, `income_delta`,
  `income_percent_change`, `spending_delta`, `spending_percent_change`, `net_delta`,
  `net_percent_change`. Missing currency totals contribute zero; zero denominator
  yields null percentage. Both sets of groups are supplied for further comparison.

Bank transfers and card payments are excluded by default. include_transfers adds
them in transfer columns, independently of include_peer_transfers, which counts
peer receipts as income and sent payments as spending. Neither is a statement of
taxable income. Pending activity is always excluded. Refunds reduce spending.

### balance_summary

`data` is `{group_by, groups, totals}`. Group rows:
`group`, `label`, `currency`, `assets`, `liabilities`, `net_worth`, `cash`, `invested`,
`credit_owed`, `unknown_balances`. Totals have the same metrics without group/label.
Historical accounts are excluded; available/unassigned accounts with missing
values increase unknown_balances, so reported totals are partial. Assets other
than deposit cash and investments remain in assets. Holdings are not added to
investment account balances, preventing double counting. No daily balance history
or on-demand upstream refresh exists.

### holdings

`data` is `{group_by, groups, totals}`. Group rows:
`group`, `label`, `currency`, `ticker`, `name`, `quantity`, `value`, `cost_basis`,
`count`, `unrealized_gain`, `allocation_percent`. ticker/name/quantity are meaningful
only for security grouping and otherwise null. Suppressed Other has no security
identifiers or quantity. Allocation is percent of the filtered portfolio per
currency. Count is constituent positions, not transactions. Cost basis and gain
are null if any constituent's basis is absent; gain=value-cost_basis.

Totals: `{currency, value, cost_basis, unrealized_gain}`. Asset classes are fund,
stock, bond, cash, other. Security types are etf, mutual_fund, stock, bond, cash,
other. Current normalization conservatively maps unknown instruments to other;
no claim of look-through fund allocation. Positions aggregate by security/account;
there is no trade or tax-lot tool.

### merchant_totals

Inputs: `window` (default `last_6_months`): `month_to_date`, `last_month`,
`last_3_months`, `last_6_months`, `last_12_months`, `all_history`;
`recurring_only` (default false). Last N months means completed UTC calendar months;
all_history uses the observed activity span. Windows reflect the latest sync, with
source freshness in the common envelope. No arbitrary date/account/merchant filters.

Returns `window`, `recurring_only`, and `merchants[]`: `merchant`, `currency`,
`total`, `purchase_count`, `monthly_average`, `recurring`, `frequency`,
`typical_charge`, `last_charged_month`, `active`, `recurring_method`, `suppressed`.
Amounts are decimal currency units: spending positive, refunds negative; no FX.
Purchase counts include positive posted charges, not pending charges or refunds.
Monthly average divides net total by calendar-month equivalents in the window.
Only normalized verified business names are allowed; no raw descriptions, people,
notes/usernames, single-purchase amounts or charge dates are available.

Groups below **max(2, min_group_count)** fold into **Other before the snapshot is
written**. If Other still has fewer purchases, its total/average are null and
suppressed=true. Its recurrence fields are null. Unknown merchants are omitted,
so this tool's totals need not reconcile to all spending. Bank transfers, card
payments and peer transfers never become merchants. For peer totals, use cash_flow
with include_peer_transfers=true. Venmo business rows require a matching Plaid
merchant entity, VERY_HIGH counterparty confidence and online/in-store channel;
ambiguous rows are peer transfers. CSV notes/names remain discarded at parse time.

`recurring_only=true` finds **subscription candidates**, not proven subscriptions.
The optional recurring endpoint supplies merchant-level patterns where available;
otherwise the heuristic needs at least **two** positive charges, all amounts
within 25% of the minimum, and all observed gaps in one of these bands: weekly
5–9 days, monthly 25–35, quarterly 80–100, yearly 350–380. Median typical_charge
uses positive charges in the selected window; refunds do not alter cadence.
`frequency` is weekly/monthly/quarterly/yearly. `last_charged_month` is YYYY-MM,
never a day. `active` requires a charge within 1.5 nominal periods (7, 30.4375,
91.3125, 365.25 days); a provider inactive hint also makes active=false.
`recurring_method` is provider_pattern or cadence_heuristic. Unsupported provider
frequencies are not relabeled. Two-charge detection is tentative; merchants with
multiple products or variable bills may not fit. Short histories cannot establish
yearly subscriptions: at least two observed yearly charges are needed. All-history
averages use observed span, not guaranteed complete historical coverage.

## Private configuration

- `PLAIDMCP_INCLUDE_LAST_FOUR=false`: default omits masks in responses **and** the
  published snapshot. Enable in both sync and server env to include last_four.
- `PLAIDMCP_MIN_GROUP_COUNT=1`: default 1 means off. Higher values fold small activity
  and position groups into Other after filters; category discovery also folds
  low-count categories. Balances/accounts/sources are not suppressed. This does
  not hide raw aggregates from the local snapshot reader, prevent inference from
  repeated queries or provide differential privacy.
- `PLAIDMCP_STALE_HOURS=24`: age threshold for unreliable_sources; errors are flagged
  regardless of age. Successful sync time is not provider-data or market-price age.
- `PLAIDMCP_DISABLED_SOURCES`: sync-only comma-separated opaque ids to stop refreshing
  and publishing selected feeds without changing tools or deleting private state.

## Exact input schemas and model-facing descriptions

### overview

Start here for 'how are my finances?': net worth, cash/invested/credit owed, month-to-date spending, prior full month and same elapsed days, trailing-six-full-month median, and top current categories. Decimal amounts per currency; spending positive/refunds negative; liabilities positive owed. Transfers/peer payments excluded. Null median means less than six observed calendar months; no assumption that missing history is zero. Coverage/freshness is not a guarantee every transaction has arrived. Every result includes per-source as_of and unreliable_sources. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {},
  "title": "overviewArguments",
  "type": "object"
}
```

### list_accounts

Discover accounts and reusable opaque ids for filters. Generated display labels never contain owner/provider display text. Returns source kind, data kinds, availability (available/historical/unassigned), per-account as_of/freshness, decimal currency balances and positive amounts owed on credit/loan accounts. Missing values are null, not zero; masks are absent unless privately enabled. Institution is an exact discovered name. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {
    "account_ids": {
      "anyOf": [
        {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Account Ids"
    },
    "institution": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Institution"
    }
  },
  "title": "list_accountsArguments",
  "type": "object"
}
```

### list_sources

Discover connected/imported sources, data kinds (balances/spending/holdings), sync times and plain-language health. No provider identifiers or credentials. All amounts elsewhere are decimal currency units, separated by currency. Check unreliable_sources before trusting totals. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {},
  "title": "list_sourcesArguments",
  "type": "object"
}
```

### list_categories

Learn plaidmcp's source-independent category and detailed-category filter codes. Returns labels, posted-item counts, observed date coverage and has_data; set only_with_data=false for the whole supported taxonomy. Counts below configured min_group_count are folded into Other. Peer payments and bank transfers are distinct categories, excluded from cash_flow by default. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {
    "only_with_data": {
      "default": true,
      "title": "Only With Data",
      "type": "boolean"
    }
  },
  "title": "list_categoriesArguments",
  "type": "object"
}
```

### cash_flow

Answer spending/income questions by category, detailed category, month, ISO Monday week, account or institution. Dates YYYY-MM-DD: start inclusive/end exclusive; default month-to-date. Use ids/names/codes from discovery. Decimal amounts per currency (USD stays USD): spending positive, refunds negative; income/transfer receipts positive; net=inflow-outflow. Bank/card transfers and peer payments default excluded independently. Peer opt-in treats receipts as income and sent payments as spending, not taxable-income classification. Returns counts, spending averages per calendar-month equivalent and seven-day week, totals and optional comparison period with absolute/percent deltas (null at zero baseline). No transaction details. min_group_count folds small groups into Other; it is not differential privacy. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {
    "group_by": {
      "default": "month",
      "enum": [
        "category",
        "category_detailed",
        "month",
        "week",
        "account",
        "institution"
      ],
      "title": "Group By",
      "type": "string"
    },
    "start": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Start"
    },
    "end": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "End"
    },
    "account_ids": {
      "anyOf": [
        {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Account Ids"
    },
    "institution": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Institution"
    },
    "category": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Category"
    },
    "include_transfers": {
      "default": false,
      "title": "Include Transfers",
      "type": "boolean"
    },
    "include_peer_transfers": {
      "default": false,
      "title": "Include Peer Transfers",
      "type": "boolean"
    },
    "compare_start": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Compare Start"
    },
    "compare_end": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Compare End"
    }
  },
  "title": "cash_flowArguments",
  "type": "object"
}
```

### balance_summary

Answer net-worth and balance questions by type, account or institution. Decimal currency units; assets/cash/invested positive, liabilities and credit_owed positive amounts owed (negative for an actual credit balance), net_worth=assets-liabilities. Excludes historical accounts and counts unknown balances (including statement wallets) instead of inventing zero; totals with unknown_balances are partial. Investments use account balances, never balance plus holdings. No currency conversion. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {
    "group_by": {
      "default": "type",
      "enum": [
        "type",
        "account",
        "institution"
      ],
      "title": "Group By",
      "type": "string"
    },
    "account_ids": {
      "anyOf": [
        {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Account Ids"
    },
    "institution": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Institution"
    }
  },
  "title": "balance_summaryArguments",
  "type": "object"
}
```

### holdings

Answer portfolio/allocation questions by security, account, asset_class or security_type. Decimal currency value/cost_basis/unrealized_gain=value-cost_basis; quantity is units and only meaningful per security. Missing basis/gain stays null. Allocation percentages are within the filtered portfolio per currency. Security rows include ticker/name; account ids match list_accounts. No trades, owners or account numbers. Small position groups fold into Other when configured. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {
    "group_by": {
      "default": "security",
      "enum": [
        "security",
        "account",
        "asset_class",
        "security_type"
      ],
      "title": "Group By",
      "type": "string"
    },
    "account_ids": {
      "anyOf": [
        {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Account Ids"
    },
    "institution": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Institution"
    }
  },
  "title": "holdingsArguments",
  "type": "object"
}
```

### merchant_totals

Answer 'which businesses do I spend with, and what is my monthly average?' Returns only normalized business-name aggregates, positive spending/refunds negative, decimal units separated by currency. Windows: month_to_date, last_month, or last_3/6/12_months (completed UTC calendar months), or all_history (observed span). Set recurring_only=true to find subscription candidates. No arbitrary dates, account/merchant filters or single purchases. At least max(2,min_group_count) purchases per named merchant/window; smaller groups fold into Other. Other with too few purchases has null total/average. Monthly average divides the window total by its calendar-month equivalents. Recurring uses provider patterns when available, otherwise at least two regularly spaced charges with amounts within 25%. Returns frequency (weekly/monthly/quarterly/yearly), typical_charge (median positive charges), last_charged_month (YYYY-MM only), active (last charge within 1.5 periods), recurring_method. These are candidates, not guaranteed subscriptions. Short history cannot establish yearly recurrence; never infer a yearly plan from only a few months. Provider BIWEEKLY/SEMI_MONTHLY is not forced into a different frequency. Venmo needs explicit high-confidence business evidence; people, notes, usernames and peer/bank transfers are excluded. Use cash_flow(include_peer_transfers=true) for peer buckets. Snapshot windows are computed at sync; every result includes source freshness. All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.

```json
{
  "properties": {
    "window": {
      "default": "last_6_months",
      "enum": [
        "month_to_date",
        "last_month",
        "last_3_months",
        "last_6_months",
        "last_12_months",
        "all_history"
      ],
      "title": "Window",
      "type": "string"
    },
    "recurring_only": {
      "default": false,
      "title": "Recurring Only",
      "type": "boolean"
    }
  },
  "title": "merchant_totalsArguments",
  "type": "object"
}
```
