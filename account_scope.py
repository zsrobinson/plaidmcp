"""Private scope configuration; public snapshots contain only fixed scope labels."""
import os

SCOPES=('all','own','separate')


def validate_scope(scope):
    if scope not in SCOPES: raise ValueError('Invalid account scope')
    return scope


def separate_accounts(db):
    refs=set(filter(None,(value.strip() for value in os.environ.get('PLAIDMCP_SEPARATE_ACCOUNTS','').split(','))))
    known={r[0] for r in db.execute('SELECT ref FROM account_identity')}
    if refs-known: raise ValueError('Unknown separate account configuration')
    return refs
