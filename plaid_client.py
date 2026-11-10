"""Sync/link only. Never imported by the MCP server."""
import json
import os
import re
import urllib.request
import urllib.error

ALLOWED = frozenset({"/link/token/create", "/item/public_token/exchange", "/accounts/get",
    "/accounts/balance/get", "/transactions/sync", "/transactions/recurring/get", "/investments/holdings/get",
    "/item/get", "/institutions/get_by_id"})

SANDBOX_CREATE = "/sandbox/public_token/create"

class PlaidError(Exception):
    def __init__(self, code):
        self.code = code if re.fullmatch(r"[A-Z_]{1,80}", str(code)) else "PLAID_ERROR"
        super().__init__(self.code)

class Client:
    def __init__(self):
        env = os.environ.get("PLAID_ENV", "sandbox")
        if env not in ("sandbox", "production"):
            raise ValueError("PLAID_ENV must be sandbox or production")
        if os.environ.get('PLAIDMCP_SANDBOX_ONLY','false').lower()=='true' and env!='sandbox':
            raise ValueError('This deployment permits Sandbox only')
        self.env = env
        self.base = "https://" + env + ".plaid.com"
        self.credentials = {"client_id": os.environ["PLAID_CLIENT_ID"], "secret": os.environ["PLAID_SECRET"]}

    def call(self, endpoint, **payload):
        if endpoint == SANDBOX_CREATE:
            if self.env != 'sandbox' or os.environ.get('PLAID_ENV') != 'sandbox' or self.base != 'https://sandbox.plaid.com':
                raise ValueError('Sandbox Item creation requires explicit PLAID_ENV=sandbox')
            products = payload.get('initial_products', [])
            if not products or set(products) - {'transactions','investments'}:
                raise ValueError('Only transactions and investments may be requested')
        elif endpoint not in ALLOWED:
            raise ValueError("Plaid endpoint is not allowed")
        if endpoint == "/accounts/balance/get":
            raise ValueError("Paid balance refresh disabled; use cached /accounts/get balances")
        if endpoint == "/link/token/create":
            products = payload.get("products", []) + payload.get("optional_products", [])
            if (not products and not payload.get('access_token')) or set(products) - {"transactions", "investments"}:
                raise ValueError("Only transactions and investments may be requested")
            if any(k in payload for k in ("additional_consented_products", "required_if_supported_products")):
                raise ValueError("Unsupported product configuration")
        body = json.dumps({**payload, **self.credentials}).encode()
        request = urllib.request.Request(self.base + endpoint, body,
            {"Content-Type": "application/json", "Plaid-Version": "2020-09-14"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try: code = json.load(error).get("error_code", "PLAID_ERROR")
            except Exception: code = "PLAID_ERROR"
            raise PlaidError(code) from None
        except (OSError, ValueError):
            raise PlaidError("NETWORK_ERROR") from None
