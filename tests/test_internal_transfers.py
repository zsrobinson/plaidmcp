"""Synthetic own-account, ambiguous, peer, refund and payroll activity."""
import json
from store import ingest_accounts,ingest_transactions,publish
from queries import Queries


def activity(key,day,amount,account='check',primary='TRANSFER_OUT',detail='TRANSFER_OUT_ACCOUNT_TRANSFER',**extra):
 return {'transaction_id':key,'account_id':account,'date':day,'amount':amount,'iso_currency_code':'USD',
 'personal_finance_category':{'primary':primary,'detailed':detail},**extra}


def prepare(snapshot):
 db,public=snapshot;db.execute('DELETE FROM transactions')
 # Different linked source, using the existing stable registry.
 ingest_accounts(db,'broker',[{'account_id':'invest','name':'Synthetic savings','type':'depository','subtype':'savings','balances':{'current':3000,'iso_currency_code':'USD'}}])
 return db,public


def test_internal_transfer_and_income_buckets(snapshot):
 db,public=prepare(snapshot)
 ingest_transactions(db,'bank',{'added':[
  activity('out','2026-11-02',250),
  activity('salary','2026-11-03',-1000,primary='INCOME',detail='INCOME_WAGES'),
  activity('interest','2026-11-03',-5,primary='INCOME',detail='INCOME_INTEREST_EARNED'),
  activity('refund','2026-11-04',-15,primary='GENERAL_MERCHANDISE',detail='GENERAL_MERCHANDISE_OTHER'),
  activity('peer','2026-11-05',-20,primary='TRANSFER_IN',detail='TRANSFER_IN_PEER_TRANSFER')]})
 ingest_transactions(db,'broker',{'added':[activity('in','2026-11-04',-250,'invest','TRANSFER_IN','TRANSFER_IN_ACCOUNT_TRANSFER')]})
 db.commit();publish(db,public)
 r=Queries(public).cash_flow('month','2026-11-01','2026-12-01',include_peer_transfers=True)['data']['totals'][0]
 assert r['income']==1025 and r['spending']==-15
 assert r['payroll']==1000 and r['interest']==5 and r['refunds']==15 and r['p2p_received']==20
 assert r['internal_transfers_in']==r['internal_transfers_out']==250
 assert r['internal_transfers_net']==0 and r['net']==1040
 assert r['transfers_in']==r['transfers_out']==0
 assert db.execute('SELECT COUNT(*) FROM internal_matches').fetchone()[0]==2
 account=next(a['id'] for a in Queries(public).list_accounts()['data'] if a['institution']=='Example Bank' and a['subtype']=='checking')
 filtered=Queries(public).cash_flow('month','2026-11-01','2026-12-01',[account])['data']['totals'][0]
 assert filtered['internal_transfers_out']==250 and filtered['internal_transfers_in']==0
 assert 'transaction_ref' not in json.dumps(r)


def test_matching_does_not_consume_payroll_ambiguity_or_peer(snapshot):
 db,public=prepare(snapshot)
 ingest_transactions(db,'bank',{'added':[activity('out1','2026-11-02',100),activity('out2','2026-11-02',100)]})
 ingest_transactions(db,'broker',{'added':[activity('in1','2026-11-02',-100,'invest','TRANSFER_IN','TRANSFER_IN_ACCOUNT_TRANSFER'),
  activity('payroll','2026-11-02',-100,'invest','INCOME','INCOME_WAGES')]})
 db.commit();publish(db,public)
 assert not db.execute('SELECT 1 FROM internal_matches').fetchone()
 r=Queries(public).cash_flow('month','2026-11-01','2026-12-01',include_transfers=True)['data']['totals'][0]
 assert r['income']==100 and r['transfers_in']==100 and r['transfers_out']==200
 db.execute("DELETE FROM transactions WHERE id='out2'");db.commit();publish(db,public)
 assert db.execute('SELECT COUNT(*) FROM internal_matches').fetchone()[0]==2
 # Removing or changing a row recomputes matches; no stale attribution survives.
 db.execute("UPDATE transactions SET date='2026-11-20' WHERE id='in1'");db.commit();publish(db,public)
 assert not db.execute('SELECT 1 FROM internal_matches').fetchone()


def test_disabled_sources_and_currency_never_false_match(snapshot,monkeypatch):
 db,public=prepare(snapshot)
 ingest_transactions(db,'bank',{'added':[activity('out','2026-11-02',100)]})
 incoming=activity('in','2026-11-02',-100,'invest','TRANSFER_IN','TRANSFER_IN_ACCOUNT_TRANSFER')
 incoming['iso_currency_code']='EUR'
 ingest_transactions(db,'broker',{'added':[incoming]});db.commit();publish(db,public)
 assert not db.execute('SELECT 1 FROM internal_matches').fetchone()
 incoming['iso_currency_code']='USD';ingest_transactions(db,'broker',{'modified':[incoming]});db.commit();publish(db,public)
 assert db.execute('SELECT COUNT(*) FROM internal_matches').fetchone()[0]==2
 source=db.execute("SELECT ref FROM source_identity WHERE provider_ref='broker'").fetchone()[0]
 monkeypatch.setenv('PLAIDMCP_DISABLED_SOURCES',source);publish(db,public)
 assert not db.execute('SELECT 1 FROM internal_matches').fetchone()


def test_audit_rejects_internal_money_counted_as_income(snapshot):
 import sqlite3,pytest
 from production_audit import audit_snapshot
 db,public=prepare(snapshot)
 ingest_transactions(db,'bank',{'added':[activity('out','2026-11-02',100)]})
 ingest_transactions(db,'broker',{'added':[activity('in','2026-11-03',-100,'invest','TRANSFER_IN','TRANSFER_IN_ACCOUNT_TRANSFER')]})
 db.commit();publish(db,public)
 audit_snapshot(public,db,set(),set(),set())
 with sqlite3.connect(public) as out:out.execute("UPDATE daily SET income=100 WHERE category='internal_transfer'")
 with pytest.raises(ValueError,match='entered income'):audit_snapshot(public,db,set(),set(),set())
