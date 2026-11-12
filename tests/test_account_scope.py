"""Synthetic account ownership scopes; no real account data."""
from datetime import date
import sqlite3
import pytest
from queries import Queries
from store import ingest_transactions, publish
from tests.test_merchants import transaction


def refs(db):
    return dict(db.execute('SELECT provider_ref,ref FROM account_alias'))


def test_scoped_balances_flow_overview_and_snapshot_membership(snapshot,monkeypatch):
    db,path=snapshot;ids=refs(db)
    monkeypatch.setattr('queries.today_utc',lambda:date(2026,11,10))
    monkeypatch.setenv('PLAIDMCP_SEPARATE_ACCOUNTS',ids['card'])
    ingest_transactions(db,'bank',{'added':[transaction('separate-charge','2026-11-02',30,account_id='card')]})
    db.commit();publish(db,path);q=Queries(path)
    assert q.balance_summary()['data']==q.balance_summary(scope='all')['data']
    assert q.balance_summary(scope='own')['data']['totals'][0]['net_worth']==5000
    assert q.balance_summary(scope='separate')['data']['totals'][0]['net_worth']==-200
    kwargs={'start':'2026-11-01','end':'2026-12-01','compare_start':'2026-10-01','compare_end':'2026-11-01'}
    assert q.cash_flow(scope='own',**kwargs)['data']['totals'][0]['spending']==110
    separate=q.cash_flow(scope='separate',**kwargs)['data']
    assert separate['totals'][0]['spending']==30 and not separate['comparison']['totals']
    assert not q.cash_flow(scope='own',account_ids=[ids['card']],**kwargs)['data']['totals']
    assert q.overview(scope='own')['data']['month_to_date'][0]['spending']==110
    assert q.overview(scope='separate')['data']['month_to_date'][0]['spending']==30
    assert q.overview(scope='separate')['data']['balances']['totals'][0]['net_worth']==-200
    monkeypatch.delenv('PLAIDMCP_SEPARATE_ACCOUNTS')
    assert q.balance_summary(scope='separate')['data']['totals'][0]['net_worth']==-200
    publish(db,path)
    assert not q.balance_summary(scope='separate')['data']['totals']


def test_merchant_scope_folding_happens_before_publication(snapshot,monkeypatch):
    db,path=snapshot;ids=refs(db)
    db.execute('DELETE FROM transactions')
    monkeypatch.setenv('PLAIDMCP_SEPARATE_ACCOUNTS',ids['card'])
    monkeypatch.setattr('merchants.today_utc',lambda:date(2026,11,10))
    ingest_transactions(db,'bank',{'added':[
        transaction('own-one','2026-09-01',20,'Synthetic Shared'),
        transaction('separate-one','2026-10-01',22,'Synthetic Shared',account_id='card')]})
    db.commit();publish(db,path);q=Queries(path)
    assert q.merchant_totals()['data']['merchants'][0]['merchant']=='Synthetic Shared'
    for scope in ('own','separate'):
        row=q.merchant_totals(scope=scope)['data']['merchants'][0]
        assert row['merchant']=='Other' and row['total'] is None and row['purchase_count']==1
    with sqlite3.connect(path) as out:
        assert not out.execute("SELECT 1 FROM merchants WHERE scope!='all' AND merchant!='Other'").fetchone()
        assert [r[1] for r in out.execute('PRAGMA table_info(merchants)') if r[5]]==['window','merchant','currency','scope']
    # Raising the threshold must fold separately within the chosen scope as well.
    ingest_transactions(db,'bank',{'added':[transaction('own-two','2026-10-01',20,'Synthetic Shared')]})
    db.commit();publish(db,path)
    assert q.merchant_totals(scope='own')['data']['merchants'][0]['total']==40
    monkeypatch.setenv('PLAIDMCP_MIN_GROUP_COUNT','3');publish(db,path)
    assert Queries(path).merchant_totals(scope='own')['data']['merchants'][0]['total'] is None
    assert Queries(path).merchant_totals(scope='all')['data']['merchants'][0]['total']==62


def test_unknown_config_preserves_snapshot_and_invalid_scopes_fail(snapshot,monkeypatch):
    db,path=snapshot;old=path.read_bytes()
    monkeypatch.setenv('PLAIDMCP_SEPARATE_ACCOUNTS','acct_unknown')
    with pytest.raises(ValueError,match='^Unknown separate account configuration$'): publish(db,path)
    assert path.read_bytes()==old
    q=Queries(path)
    for name in ('merchant_totals','cash_flow','balance_summary','overview'):
        with pytest.raises(ValueError,match='Invalid account scope'): getattr(q,name)(scope='bad')


async def test_tool_scope_schema(snapshot):
    from server import create_server
    tools=await create_server(snapshot[1]).list_tools()
    for tool in tools:
        if tool.name in ('merchant_totals','cash_flow','balance_summary','overview'):
            scope=tool.inputSchema['properties']['scope']
            assert scope['default']=='all' and scope['enum']==['all','own','separate']
            assert 'Scope all' in tool.description
