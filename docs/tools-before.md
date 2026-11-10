# Tool surface before design review

Captured from the original local prototype before implementation changes. Every tool returns `{as_of: string|null, stale: boolean, data: array}`. Monetary fields are numbers or null; counts are integers. SQL null propagates for unavailable balances, cost basis, percentages and re-link state. No transaction endpoints are exposed.

## list_accounts

Account names/masks and cached balances, grouped currencies remain separate.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {},
  "title": "list_accountsArguments",
  "type": "object"
}
```

Fields in each `data` row: name, mask, type, subtype, currency, current, available, credit_limit, institution.

## balances

Alias for cached account balances. Never refreshes Plaid.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {},
  "title": "balancesArguments",
  "type": "object"
}
```

Fields in each `data` row: name, mask, type, subtype, currency, current, available, credit_limit, institution.

## net_worth

Assets less credit/loan balances, by account type and currency; nulls stay unknown.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {},
  "title": "net_worthArguments",
  "type": "object"
}
```

Fields in each `data` row: currency, type (including total), net, unknown_balances.

## spending_by_category

Net spending/refunds, end exclusive; optionally include sent peer payments, never notes.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {
    "month": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Month"
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
    "level": {
      "default": "primary",
      "title": "Level",
      "type": "string"
    },
    "exclude_transfers": {
      "default": true,
      "title": "Exclude Transfers",
      "type": "boolean"
    },
    "include_peer_transfers": {
      "default": false,
      "title": "Include Peer Transfers",
      "type": "boolean"
    }
  },
  "title": "spending_by_categoryArguments",
  "type": "object"
}
```

Fields in each `data` row: category, currency, total, count.

## spending_by_month

Monthly net spending; opt in to sent peer payments. Category is a coarse/PFCv2 code.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {
    "months": {
      "default": 6,
      "title": "Months",
      "type": "integer"
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
    "end_month": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "End Month"
    },
    "include_peer_transfers": {
      "default": false,
      "title": "Include Peer Transfers",
      "type": "boolean"
    }
  },
  "title": "spending_by_monthArguments",
  "type": "object"
}
```

Fields in each `data` row: month, currency, total, count.

## cash_flow

Income and spending; opt in to peer receipts/payments. Bank/card transfers excluded.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {
    "months": {
      "default": 6,
      "title": "Months",
      "type": "integer"
    },
    "end_month": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "End Month"
    },
    "include_peer_transfers": {
      "default": false,
      "title": "Include Peer Transfers",
      "type": "boolean"
    }
  },
  "title": "cash_flowArguments",
  "type": "object"
}
```

Fields in each `data` row: month, currency, income, spending, net.

## category_trends

Top spending categories versus previous month; optional sent peer payments, null percent at zero baseline.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {
    "month": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Month"
    },
    "top": {
      "default": 5,
      "title": "Top",
      "type": "integer"
    },
    "include_peer_transfers": {
      "default": false,
      "title": "Include Peer Transfers",
      "type": "boolean"
    }
  },
  "title": "category_trendsArguments",
  "type": "object"
}
```

Fields in each `data` row: category, currency, current, previous, change, percent_change.

## holdings_summary

Security positions plus allocation by security type and account; no trade history.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {},
  "title": "holdings_summaryArguments",
  "type": "object"
}
```

Fields in each `data` row: allocation (security/type/account), ticker, name, type, account, currency, quantity, value, cost_basis, percent.

## sync_status

Per-institution sync freshness and sanitized error codes; login errors require update Link.

Exact input schema (including defaults/required fields):

```json
{
  "properties": {},
  "title": "sync_statusArguments",
  "type": "object"
}
```

Fields in each `data` row: institution, as_of, error (verbatim private provider code), needs_relink (0/1/null).

`holdings_summary` security rows have null percent; type/account rows have null ticker/name/quantity/cost_basis and percentages computed per currency. `net_worth` includes a total row per currency. Date ranges use inclusive start/exclusive end. Peer inclusion defaults false. No input is required for any tool.
