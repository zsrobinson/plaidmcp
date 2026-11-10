import os
import pytest
from plaid_client import Client,PlaidError,ALLOWED
from sync import sync_item
from store import ingest_transactions

def test_allowlist(monkeypatch):
    monkeypatch.setenv('PLAID_CLIENT_ID','fake'); monkeypatch.setenv('PLAID_SECRET','fake')
    c=Client()
    assert c.base=='https://sandbox.plaid.com'
    assert len(ALLOWED)==9
    for path in ('/transfer/create','/auth/get','/identity/get','/accounts/balance/get'):
        with pytest.raises(ValueError): c.call(path)
    with pytest.raises(ValueError): c.call('/link/token/create',products=['auth'])
    monkeypatch.setenv('PLAID_ENV','evil')
    with pytest.raises(ValueError): Client()

def test_sync_pagination_rollback(snapshot):
    db,_=snapshot
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY,institution_id TEXT,token TEXT,product TEXT,cursor TEXT)')
    db.execute("INSERT INTO credentials VALUES('bank','ins','fake','transactions','old')"); db.commit()
    class Fake:
        calls=0
        cursors=[]
        def call(self,path,**kw):
            if path=='/item/get': return {'item':{}}
            if path=='/institutions/get_by_id': return {'institution':{'name':'Example Bank'}}
            if path=='/accounts/get': return {'accounts':[]}
            if path=='/transactions/recurring/get':raise PlaidError('PRODUCT_NOT_ENABLED')
            self.cursors.append(kw.get('cursor')); self.calls+=1
            assert kw['options']['personal_finance_category_version']=='v2'
            if self.calls==2: raise PlaidError('TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION')
            return {'added':[], 'modified':[], 'removed':[{'transaction_id':'1'}], 'next_cursor':'next' if self.calls==1 else 'done','has_more':self.calls==1}
    fake=Fake(); sync_item(db,fake,('bank','ins','fake','transactions','old'))
    assert fake.cursors==['old','next','old']
    assert db.execute('SELECT cursor FROM credentials').fetchone()[0]=='done'
    class Broken:
        def call(self,*a,**kw): raise PlaidError('ITEM_LOGIN_REQUIRED')
    sync_item(db,Broken(),('bank','ins','fake','transactions','done'))
    assert db.execute("SELECT error FROM items WHERE id='bank'").fetchone()[0]=='ITEM_LOGIN_REQUIRED'

def test_modified_removed(snapshot):
    db,_=snapshot
    ingest_transactions(db,'bank',{'modified':[{'transaction_id':'2','date':'2026-11-03','amount':42,'personal_finance_category':{'primary':'FOOD_AND_DRINK','detailed':'FOOD_AND_DRINK_GROCERIES'}}],'removed':[{'transaction_id':'3'}]})
    assert db.execute("SELECT amount FROM transactions WHERE id='2'").fetchone()[0]==42
    assert db.execute("SELECT count(*) FROM transactions WHERE id='3'").fetchone()[0]==0


def test_sandbox_create_fails_closed(monkeypatch):
    import io
    import json
    from plaid_client import SANDBOX_CREATE
    monkeypatch.setenv('PLAID_CLIENT_ID','fake'); monkeypatch.setenv('PLAID_SECRET','fake')
    def fake_open(request,timeout):
        assert request.full_url=='https://sandbox.plaid.com'+SANDBOX_CREATE
        assert set(json.loads(request.data)['initial_products'])=={'transactions','investments'}
        return io.BytesIO(b'{"public_token":"fake-public"}')
    monkeypatch.setattr('urllib.request.urlopen',fake_open)
    monkeypatch.setenv('PLAID_ENV','sandbox')
    client=Client()
    assert client.call(SANDBOX_CREATE,initial_products=['transactions','investments'])=={'public_token':'fake-public'}
    with pytest.raises(ValueError): client.call(SANDBOX_CREATE,initial_products=['auth'])
    monkeypatch.setenv('PLAID_ENV','production')
    with pytest.raises(ValueError,match='Sandbox'): Client().call(SANDBOX_CREATE,initial_products=['transactions'])
    with pytest.raises(ValueError,match='Sandbox'): client.call(SANDBOX_CREATE,initial_products=['transactions'])
    monkeypatch.delenv('PLAID_ENV')
    with pytest.raises(ValueError,match='explicit'): Client().call(SANDBOX_CREATE,initial_products=['transactions'])
    monkeypatch.setenv('PLAID_ENV','development')
    with pytest.raises(ValueError): Client()


def test_combined_products_syncs_both_and_cursor(snapshot):
    db,_=snapshot
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY,institution_id TEXT,token TEXT,product TEXT,cursor TEXT)')
    db.execute("INSERT INTO credentials VALUES('bank','ins','fake','transactions,investments',NULL)");db.commit()
    class Fake:
        calls=[]
        def call(self,path,**kwargs):
            self.calls.append(path)
            if path=='/item/get': return {'item':{}}
            if path=='/institutions/get_by_id': return {'institution':{'name':'Fake Sandbox'}}
            if path=='/accounts/get': return {'accounts':[]}
            if path=='/transactions/recurring/get':raise PlaidError('PRODUCT_NOT_ENABLED')
            if path=='/transactions/sync': return {'added':[],'modified':[],'removed':[],'next_cursor':'done','has_more':False}
            if path=='/investments/holdings/get': return {'holdings':[],'securities':[]}
            raise AssertionError('Unexpected endpoint')
    fake=Fake()
    sync_item(db,fake,('bank','ins','fake','transactions,investments',None))
    assert '/transactions/sync' in fake.calls and '/investments/holdings/get' in fake.calls
    assert db.execute('SELECT cursor FROM credentials').fetchone()[0]=='done'
    assert db.execute("SELECT error FROM items WHERE id='bank'").fetchone()[0] is None


def test_link_requests_only_read_products(monkeypatch):
    import io,json
    monkeypatch.setenv('PLAID_CLIENT_ID','fake');monkeypatch.setenv('PLAID_SECRET','fake')
    monkeypatch.setenv('PLAID_ENV','sandbox')
    def fake_open(request,timeout):
        body=json.loads(request.data)
        assert set(body['products']) <= {'transactions','investments'}
        assert not (set(body) & {'additional_consented_products','required_if_supported_products'})
        return io.BytesIO(b'{"link_token":"fake"}')
    monkeypatch.setattr('urllib.request.urlopen',fake_open)
    client=Client()
    assert client.call('/link/token/create',products=['transactions','investments'])=={'link_token':'fake'}
    for bad in ('auth','identity','transfer','payment_initiation','signal'):
        with pytest.raises(ValueError): client.call('/link/token/create',products=[bad])
        with pytest.raises(ValueError): client.call('/link/token/create',products=['transactions'],optional_products=[bad])


def test_sandbox_only_deployment_rejects_production(monkeypatch):
    monkeypatch.setenv('PLAID_CLIENT_ID','fake');monkeypatch.setenv('PLAID_SECRET','fake')
    monkeypatch.setenv('PLAIDMCP_SANDBOX_ONLY','true');monkeypatch.setenv('PLAID_ENV','production')
    with pytest.raises(ValueError,match='Sandbox only'): Client()
