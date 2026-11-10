"""One-shot sync, private credentials and SQLite; logs only fixed status messages."""
import telemetry
import fcntl
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from plaid_client import Client, PlaidError
from store import connect, ingest_accounts, ingest_transactions, ingest_holdings, publish

ingest_accounts = telemetry.timed('store')(ingest_accounts)
ingest_transactions = telemetry.timed('store')(ingest_transactions)
ingest_holdings = telemetry.timed('store')(ingest_holdings)

@contextmanager
def private_store():
    os.umask(0o027)
    root = Path(os.environ.get('PLAIDMCP_PRIVATE','private'))
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(root,0o700)
    with open(root/'lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        db = connect(root/'private.sqlite')
        os.chmod(root/'private.sqlite',0o600)
        db.execute('CREATE TABLE IF NOT EXISTS credentials(item TEXT PRIMARY KEY, institution_id TEXT UNIQUE, token TEXT, product TEXT, cursor TEXT)')
        try: yield db
        finally: db.close()

def sync_item(db, client, row):
    original = client
    class MeasuredClient:
        @telemetry.timed('fetch')
        def call(self, *args, **kwargs):
            return original.call(*args, **kwargs)
    client = MeasuredClient()
    telemetry.count('items_count')
    item,institution_id,token,product,cursor = row
    products = set(product.split(','))
    if not products or products - {'transactions','investments'}:
        raise ValueError('Unsupported stored sync products')
    backfill=os.environ.get('PLAIDMCP_BACKFILL_MERCHANTS','false').lower()=='true' and 'transactions' in products
    if backfill:cursor=None
    # Whole pagination transaction rolls back on mutation, leaving the old cursor intact.
    for attempt in range(3):
        try:
            with db:
                if backfill:db.execute('DELETE FROM transactions WHERE item=?',(item,))
                meta = client.call('/item/get',access_token=token)['item']
                if meta.get('error'): raise PlaidError(meta['error'].get('error_code','PLAID_ERROR'))
                institution = client.call('/institutions/get_by_id',institution_id=institution_id,country_codes=['US'])['institution']['name']
                if 'venmo' in institution.casefold() and db.execute("SELECT 1 FROM items WHERE id='venmo-csv'").fetchone():
                    raise PlaidError('PLAIDMCP_VENMO_SOURCE_OVERLAP')
                db.execute('UPDATE items SET institution=? WHERE id=?',(institution,item))
                accounts = client.call('/accounts/get',access_token=token)['accounts']
                ingest_accounts(db,item,accounts)
                if 'transactions' in products:
                    next_cursor = cursor
                    for _ in range(1000):
                        payload = {'access_token':token,'count':500,'options':{'personal_finance_category_version':'v2','include_original_description':True}}
                        if next_cursor: payload['cursor'] = next_cursor
                        page = client.call('/transactions/sync',**payload)
                        telemetry.count('pages')
                        ingest_transactions(db,item,page)
                        next_cursor = page['next_cursor']
                        if not page['has_more']: break
                    else: raise PlaidError('PAGINATION_LIMIT')
                    db.execute('UPDATE credentials SET cursor=? WHERE item=?',(next_cursor,item))
                    from merchants import ingest_recurring
                    try:recurring=client.call('/transactions/recurring/get',access_token=token)
                    except PlaidError:recurring={}
                    ingest_recurring(db,item,recurring)
                if 'investments' in products:
                    ingest_holdings(db,item,client.call('/investments/holdings/get',access_token=token))
                now = datetime.now(timezone.utc).isoformat()
                db.execute('UPDATE items SET as_of=?,error=NULL WHERE id=?',(now,item))
            return
        except PlaidError as error:
            if error.code == 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION' and attempt < 2:
                telemetry.count('retries')
                continue
            telemetry.failure(error)
            with db: db.execute('UPDATE items SET error=? WHERE id=?',(error.code,item))
            return

@telemetry.sync_run()
def main():
    from sources import refresh_sources
    with private_store() as db:
        refresh_sources(db,os.environ.get('PLAIDMCP_PUBLIC','aggregate.sqlite'),Client)
        with telemetry.phase('publish'):
            publish(db,os.environ.get('PLAIDMCP_PUBLIC','aggregate.sqlite'))
        db.commit()  # persist opaque identities generated during publication

if __name__ == '__main__':
    try: main()
    except Exception:
        raise SystemExit('Sync failed; inspect private configuration. No request details logged.') from None
