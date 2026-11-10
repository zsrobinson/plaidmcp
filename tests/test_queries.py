import asyncio
import json
import sqlite3
import pytest
from queries import Queries
from server import create_server,TOOLS
from store import publish, ingest_accounts, ingest_transactions
from tests.fixtures import MERCHANT,AS_OF

ARGS={'cash_flow':{'start':'2026-11-01','end':'2026-12-01'}}

async def test_all_tools_privacy_and_concurrency(snapshot):
    db,path=snapshot;mcp=create_server(path)
    assert {t.name for t in await mcp.list_tools()}==set(TOOLS)
    async def call(name):
        result=await mcp.call_tool(name,ARGS.get(name,{}))
        text=json.dumps(result,default=str)
        for forbidden in (MERCHANT,'PRIVATE DESCRIPTION','PRIVATE COUNTERPARTY','transaction_id','merchant_name','counterparties','access_token','public_token','account_id"','routing_number','owner_name','PLAID_ERROR','ITEM_LOGIN_REQUIRED'):
            assert forbidden not in text
        assert AS_OF in text
    await asyncio.wait_for(asyncio.gather(*(call(n) for _ in range(5) for n in TOOLS)),10)
    assert MERCHANT.encode() not in path.read_bytes()
    with sqlite3.connect(path) as db2:
        assert {r[0] for r in db2.execute("SELECT name FROM sqlite_master WHERE type='table'")}=={'sources','accounts','daily','holdings','merchants'}
        assert not any(r[0] for r in db2.execute('SELECT last_four FROM accounts'))
    with pytest.raises(sqlite3.OperationalError): Queries(path)._read(lambda db,sources:db.execute('DELETE FROM accounts'))


def test_discovery_totals_and_comparison(snapshot):
    _,path=snapshot;q=Queries(path)
    accounts=q.list_accounts()['data'];assert len(accounts)==3
    assert all(a['id'].startswith('acct_') and 'last_four' not in a for a in accounts)
    assert all(a['source_kind']=='connection' and a['data_kinds'] for a in accounts)
    sources=q.list_sources()['data'];assert all(s['id'].startswith('src_') for s in sources)
    categories=q.list_categories()['data'];assert any(c['category_detailed']=='groceries' and c['count']==4 for c in categories)
    assert len(q.list_categories(False)['data'])>len(categories)
    result=q.cash_flow('category','2026-11-01','2026-12-01',compare_start='2026-10-01',compare_end='2026-11-01')['data']
    assert result['totals'][0]['spending']==110 and result['totals'][0]['income']==1000
    assert result['totals'][0]['average_per_month']==110
    assert result['comparison']['deltas'][0]['spending_delta']==30
    assert result['comparison']['deltas'][0]['spending_percent_change']==37.5
    assert q.balance_summary()['data']['totals'][0]['net_worth']==4800
    holding=q.holdings()['data']['groups'][0]
    assert holding['value']==3000 and holding['unrealized_gain']==500 and holding['allocation_percent']==100
    assert q.holdings('asset_class')['data']['groups'][0]['group']=='fund'
    for group in ('category','category_detailed','month','week','account','institution'):
        assert q.cash_flow(group,'2026-11-01','2026-12-01')['data']['totals'][0]['spending']==110
    check=next(a for a in accounts if a['type']=='depository')
    assert q.cash_flow('account','2026-11-01','2026-12-01',[check['id']])['data']['totals'][0]['spending']==110
    assert q.cash_flow('institution','2026-11-01','2026-12-01',institution='unknown')['data']['groups']==[]
    assert q.cash_flow('category','2026-11-01','2026-12-01',category='groceries')['data']['totals'][0]['spending']==110
    assert q.cash_flow('category','2026-11-01','2026-12-01',include_transfers=True)['data']['totals'][0]['transfers_out']==700
    overview=q.overview()['data'];assert 'typical_month' in overview and 'last_month_same_elapsed_days' in overview


def test_atomic_snapshot_and_health(snapshot):
    db,path=snapshot
    old=sqlite3.connect(path);old.execute('BEGIN');old.execute('SELECT * FROM sources').fetchall()
    db.execute("UPDATE items SET error='ITEM_LOGIN_REQUIRED' WHERE id='bank'");db.commit();publish(db,path)
    assert old.execute("SELECT status FROM sources WHERE institution='Example Bank'").fetchone()[0]=='healthy';old.close()
    result=Queries(path).list_sources()
    assert any(s['status']=='needs re-link' for s in result['data'])
    assert result['unreliable_sources'] and isinstance(result['as_of'],dict)
    assert 'ITEM_LOGIN_REQUIRED' not in path.read_bytes().decode('latin1')


def test_relink_identity_and_ambiguous_matches(snapshot):
    db,path=snapshot
    before=next(a['id'] for a in Queries(path).list_accounts()['data'] if a['subtype']=='checking')
    def account(id,name='Checking'):
        return {'account_id':id,'name':name,'type':'depository','subtype':'checking','balances':{'current':123,'iso_currency_code':'USD'}}
    ingest_accounts(db,'bank',[account('new-provider-id')]);db.commit();publish(db,path)
    after=Queries(path).list_accounts()['data'];assert next(a['id'] for a in after if a['subtype']=='checking')==before
    ingest_accounts(db,'bank',[account('ambiguous1'),account('ambiguous2')]);db.commit();publish(db,path)
    refs=[a['id'] for a in Queries(path).list_accounts()['data'] if a['subtype']=='checking' and 'balances' in a['data_kinds']]
    assert len(set(refs))==2 and before not in refs


def test_new_source_does_not_change_tool_schema(snapshot):
    db,path=snapshot
    class NewSource:
        def refresh(self,db,public): pass
        def read_snapshot(self,db):
            return {'sources':[('src_example','New credit union','file_import',AS_OF,'healthy','["spending"]')],
                'accounts':[('acct_example','src_example','Statement wallet','New credit union','other','wallet','USD',None,None,None,'','["spending"]','available')],
                'daily':[('2026-11-01','acct_example','src_example','shopping','general_shopping','USD',0,42,0,0,0,0,1)],'holdings':[]}
    before=asyncio.run(create_server(path).list_tools())
    from sources import snapshot_sources
    publish(db,path,[*snapshot_sources(db),NewSource()])
    after=asyncio.run(create_server(path).list_tools())
    assert [t.model_dump() for t in before]==[t.model_dump() for t in after]
    q=Queries(path)
    assert len(q.list_sources()['data'])==3
    assert q.cash_flow('institution','2026-11-01','2026-12-01',institution='New credit union')['data']['totals'][0]['spending']==42


def test_optional_mask_minimum_and_disable(snapshot,monkeypatch):
    db,path=snapshot
    monkeypatch.setenv('PLAIDMCP_INCLUDE_LAST_FOUR','true');publish(db,path)
    assert any(a['last_four']=='1234' for a in Queries(path).list_accounts()['data'])
    monkeypatch.setenv('PLAIDMCP_MIN_GROUP_COUNT','4');q=Queries(path)
    flow=q.cash_flow('category','2026-11-01','2026-12-01')['data']
    assert all(r['group']=='__other__' for r in flow['groups']) and flow['totals'][0]['spending']==110
    assert q.holdings()['data']['groups'][0]['group']=='__other__'
    source=q.list_sources()['data'][0]['id'];monkeypatch.setenv('PLAIDMCP_DISABLED_SOURCES',source);publish(db,path)
    assert source not in Queries(path).list_sources()['as_of']


def test_sensitive_fields_never_persist_or_publish(snapshot):
    db,path=snapshot
    secrets={'routing_number':'021000021','account_number':'7777888899990000','owner_name':'CONFIDENTIAL OWNER SENTINEL',
        'address':'PRIVATE ADDRESS SENTINEL','email':'private-person@example.invalid','phone_number':'+1-202-555-0199'}
    account={'account_id':'provider-id-sensitive','name':secrets['owner_name'],'type':'depository','subtype':'checking',
        'mask':'9999','balances':{'current':10,'iso_currency_code':'USD'},**secrets,
        'owners':[{'names':[secrets['owner_name']],'emails':[secrets['email']]}],'numbers':secrets}
    ingest_accounts(db,'bank',[account]);db.commit();publish(db,path)
    q=Queries(path)
    for name in TOOLS:
        encoded=json.dumps(getattr(q,name)(**ARGS.get(name,{})),ensure_ascii=False).encode()
        for key,value in secrets.items(): assert key.encode() not in encoded and value.encode() not in encoded
    for data in (path.read_bytes(),__import__('pathlib').Path(db.execute('PRAGMA database_list').fetchone()[2]).read_bytes()):
        for key,value in secrets.items(): assert key.encode() not in data and value.encode() not in data
    assert b'provider-id-sensitive' not in path.read_bytes()


def test_invalid_inputs(snapshot):
    _,path=snapshot;q=Queries(path)
    for kwargs in ({'start':'bad','end':'2026-12-01'},{'start':'2026-11-01'}, {'group_by':'merchant'}, {'compare_start':'2026-10-01'}, {'account_ids':['provider-id']}):
        with pytest.raises(ValueError): q.cash_flow(**kwargs)


def test_closed_account_history_is_kept(snapshot):
    db,path=snapshot
    before=Queries(path).cash_flow('month','2026-11-01','2026-12-01')['data']['totals'][0]['spending']
    ingest_accounts(db,'bank',[]);db.commit();publish(db,path)
    q=Queries(path)
    historical=[a for a in q.list_accounts()['data'] if a['institution']=='Example Bank']
    assert historical and all('balances' not in a['data_kinds'] for a in historical)
    assert q.cash_flow('month','2026-11-01','2026-12-01')['data']['totals'][0]['spending']==before
    assert q.balance_summary()['data']['totals'][0]['net_worth']==3000


def test_relink_to_replacement_source_preserves_identity(snapshot):
    db,path=snapshot
    before=next(a['id'] for a in Queries(path).list_accounts()['data'] if a['subtype']=='checking')
    ingest_accounts(db,'bank',[])
    # Removing the old source from publication does not discard identity registry entries.
    db.execute("DELETE FROM items WHERE id='bank'")
    db.execute('INSERT INTO items VALUES(?,?,?,NULL)',('replacement','Example Bank',AS_OF))
    ingest_accounts(db,'replacement',[{'account_id':'new-remote-account','name':'Checking','type':'depository',
        'subtype':'checking','balances':{'current':2000,'iso_currency_code':'USD'}}])
    db.commit();publish(db,path)
    assert next(a['id'] for a in Queries(path).list_accounts()['data'] if a['subtype']=='checking')==before


def test_overview_median_and_deltas(snapshot,monkeypatch):
    from datetime import date
    db,path=snapshot
    monkeypatch.setattr('queries.today_utc',lambda:date(2026,11,10))
    for month in range(5,11):
        ingest_transactions(db,'bank',{'added':[{'transaction_id':'median-'+str(month),'account_id':'check',
            'date':f'2026-{month:02d}-01','amount':month*10,'personal_finance_category':{'primary':'FOOD_AND_DRINK','detailed':'FOOD_AND_DRINK_GROCERIES'},'iso_currency_code':'USD'}]})
    db.commit();publish(db,path)
    result=Queries(path).overview()['data']
    # May–Sep 50,60,70,80,90; October 100+existing 80.
    assert result['typical_month'][0]['median_monthly_spending']==75
    assert result['month_to_date'][0]['spending']==110
    assert result['spending_comparisons'][0]['typical_month']['delta']==35


def test_freshness_appears_in_every_response(snapshot):
    db,path=snapshot
    db.execute("UPDATE items SET as_of='2000-01-01T00:00:00+00:00',error='SOME_PRIVATE_BACKEND_ERROR' WHERE id='bank'");db.commit();publish(db,path)
    q=Queries(path)
    for name in TOOLS:
        result=getattr(q,name)(**ARGS.get(name,{}))
        assert result['as_of'] and result['sources'] and result['unreliable_sources']
        assert any(s['freshness']=='outdated' for s in result['unreliable_sources'])
        assert 'SOME_PRIVATE_BACKEND_ERROR' not in json.dumps(result)


def test_server_imports_no_sync_or_provider_adapter():
    import subprocess,sys
    script="import server,sys;server.create_server();assert not ({'sources','sync','plaid_client','venmo_import'} & sys.modules.keys())"
    subprocess.run([sys.executable,'-c',script],check=True)


def test_disabled_source_replacement_preserves_account_id(snapshot,monkeypatch):
    db,path=snapshot
    q=Queries(path);before=next(a for a in q.list_accounts()['data'] if a['subtype']=='checking')
    monkeypatch.setenv('PLAIDMCP_DISABLED_SOURCES',before['source_id'])
    db.execute('INSERT INTO items VALUES(?,?,?,NULL)',('replacement','Example Bank',AS_OF))
    ingest_accounts(db,'replacement',[{'account_id':'brand-new-provider-id','name':'Checking','type':'depository',
        'subtype':'checking','balances':{'current':42,'iso_currency_code':'USD'}}])
    db.commit();publish(db,path)
    after=next(a for a in Queries(path).list_accounts()['data'] if a['subtype']=='checking')
    assert after['id']==before['id'] and after['source_id']!=before['source_id']
    old_snapshot=path.read_bytes()
    monkeypatch.delenv('PLAIDMCP_DISABLED_SOURCES')
    with pytest.raises(ValueError,match='Conflicting active sources'): publish(db,path)
    assert path.read_bytes()==old_snapshot


def test_disabling_all_connections_needs_no_provider_credentials(snapshot,monkeypatch,tmp_path):
    from sources import refresh_sources
    db,path=snapshot
    source=next(s['id'] for s in Queries(path).list_sources()['data'] if s['institution']=='Example Bank')
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY, institution_id TEXT, token TEXT, product TEXT, cursor TEXT)')
    db.execute("INSERT INTO credentials VALUES('bank','fake','fake','transactions',NULL)")
    monkeypatch.setenv('PLAIDMCP_DISABLED_SOURCES',source)
    monkeypatch.setenv('PLAIDMCP_VENMO_IMPORT_DIR',str(tmp_path/'unused'))
    def forbidden(): raise AssertionError('Disabled connections must not instantiate provider client')
    refresh_sources(db,path,forbidden)
