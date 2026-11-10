from sync import sync_item
from plaid_client import PlaidError
from tests.test_merchants import transaction


def test_backfill_rebuilds_existing_item_without_link_and_preserves_ids(snapshot,monkeypatch):
    db,_=snapshot
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY,institution_id TEXT,token TEXT,product TEXT,cursor TEXT)')
    db.execute("INSERT INTO credentials VALUES('bank','synthetic-ins','synthetic-token','transactions','old-cursor')");db.commit()
    before=db.execute('SELECT * FROM account_identity ORDER BY ref').fetchall()
    calls=[]
    class Client:
        def call(self,endpoint,**payload):
            calls.append(endpoint)
            if endpoint=='/item/get':return {'item':{}}
            if endpoint=='/institutions/get_by_id':return {'institution':{'name':'Example Bank'}}
            if endpoint=='/accounts/get':return {'accounts':[]}
            if endpoint=='/transactions/recurring/get':raise PlaidError('PRODUCT_NOT_ENABLED')
            assert endpoint=='/transactions/sync' and 'cursor' not in payload
            return {'added':[transaction('backfilled','2026-10-01')],'next_cursor':'new-cursor','has_more':False}
    monkeypatch.setenv('PLAIDMCP_BACKFILL_MERCHANTS','true')
    sync_item(db,Client(),('bank','synthetic-ins','synthetic-token','transactions','old-cursor'))
    assert db.execute("SELECT id,merchant FROM transactions WHERE item='bank'").fetchall()==[('backfilled','Fixture Software')]
    assert db.execute('SELECT cursor FROM credentials').fetchone()[0]=='new-cursor'
    assert db.execute('SELECT * FROM account_identity ORDER BY ref').fetchall()==before
    assert all('/link/' not in endpoint and '/exchange' not in endpoint for endpoint in calls)


def test_failed_backfill_keeps_old_transactions_and_cursor(snapshot,monkeypatch):
    db,_=snapshot
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY,institution_id TEXT,token TEXT,product TEXT,cursor TEXT)')
    db.execute("INSERT INTO credentials VALUES('bank','synthetic-ins','synthetic-token','transactions','old-cursor')");db.commit()
    before=db.execute("SELECT * FROM transactions WHERE item='bank' ORDER BY id").fetchall()
    class Client:
        def call(self,endpoint,**payload):raise PlaidError('NETWORK_ERROR')
    monkeypatch.setenv('PLAIDMCP_BACKFILL_MERCHANTS','true')
    sync_item(db,Client(),('bank','synthetic-ins','synthetic-token','transactions','old-cursor'))
    assert db.execute("SELECT * FROM transactions WHERE item='bank' ORDER BY id").fetchall()==before
    assert db.execute('SELECT cursor FROM credentials').fetchone()[0]=='old-cursor'
