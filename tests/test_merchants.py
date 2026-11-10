"""Invented businesses and people only; no live financial samples."""
from datetime import date
import json
import sqlite3
from pathlib import Path
import pytest
from merchants import business_name,ingest_recurring
from production_audit import audit_snapshot
from queries import Queries
from store import ingest_transactions,publish

@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr('merchants.today_utc',lambda:date(2026,11,10))


def transaction(key,day,amount=20,merchant='Fixture Software',**extra):
    return {'transaction_id':key,'account_id':'check','date':day,'amount':amount,'pending':False,
        'merchant_name':merchant,'merchant_entity_id':'synthetic-entity','payment_channel':'online',
        'counterparties':[{'type':'merchant','name':merchant,'entity_id':'synthetic-entity','confidence_level':'VERY_HIGH'}],
        'personal_finance_category':{'primary':'GENERAL_MERCHANDISE','detailed':'GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE'},
        'iso_currency_code':'USD',**extra}


def add(db,rows,item='bank'):
    ingest_transactions(db,item,{'added':rows});db.commit()


def rows(public,window='last_6_months',**args):
    return Queries(public).merchant_totals(window,**args)['data']['merchants']


def test_multi_purchase_folding_and_no_per_purchase_fields(snapshot):
    db,public=snapshot
    add(db,[transaction('m1','2026-09-01',20),transaction('m2','2026-10-01',22),
        transaction('m3','2026-10-15',-2),transaction('single','2026-10-11',17,'SINGLE BUSINESS SENTINEL')])
    publish(db,public)
    merchant=next(r for r in rows(public) if r['merchant']=='Fixture Software')
    assert merchant['total']==40 and merchant['purchase_count']==2
    assert merchant['monthly_average']==6.67
    assert merchant['typical_charge']==21 and merchant['recurring']
    assert merchant['frequency']=='monthly' and merchant['last_charged_month']=='2026-10'
    other=next(r for r in rows(public) if r['merchant']=='Other')
    assert other['purchase_count']==1 and other['total'] is None and other['monthly_average'] is None
    assert other['typical_charge'] is None and other['frequency'] is None
    assert b'SINGLE BUSINESS SENTINEL' not in public.read_bytes()
    assert 'SINGLE BUSINESS SENTINEL' not in json.dumps(rows(public))
    for r in rows(public):
        assert not ({'date','last_date','transaction_id','account_id','merchant_name','description','amount'}&r.keys())
    assert not rows(public,'month_to_date')
    assert all(r['merchant']=='Other' for r in rows(public,'last_month'))
    assert len(rows(public,recurring_only=True))==1


def test_other_totals_only_after_two_purchases_and_config_folding(snapshot,monkeypatch):
    db,public=snapshot
    add(db,[transaction('o1','2026-10-01',5,'Synthetic One'),transaction('o2','2026-10-02',7,'Synthetic Two')])
    publish(db,public)
    other=rows(public)[0]
    assert other['merchant']=='Other' and other['total']==12 and other['purchase_count']==2
    add(db,[transaction('d1','2026-10-03',10,'Synthetic Double'),transaction('d2','2026-10-08',10,'Synthetic Double')])
    monkeypatch.setenv('PLAIDMCP_MIN_GROUP_COUNT','3');publish(db,public)
    assert len(rows(public))==1 and rows(public)[0]['merchant']=='Other'
    assert b'Synthetic Double' not in public.read_bytes()


def test_venmo_business_evidence_and_person_fields_never_persist(snapshot):
    db,public=snapshot
    db.execute("INSERT INTO items VALUES('wallet','Venmo - Personal','2026-11-10T00:00:00+00:00',NULL)")
    person='PRIVATE PERSON SENTINEL';note='PRIVATE PAYMENT NOTE SENTINEL';username='PRIVATE USERNAME SENTINEL'
    peer=transaction('peer1','2026-10-02',13,person,payment_note=note,username=username,
        counterparties=[{'type':'person','name':person,'username':username}])
    uncertain=transaction('peer2','2026-10-03',14,person,payment_note=note,counterparties=[])
    add(db,[peer,uncertain,transaction('biz1','2026-09-01',20,'Synthetic Wallet Business'),transaction('biz2','2026-10-01',20,'Synthetic Wallet Business')],'wallet')
    publish(db,public)
    assert db.execute("SELECT merchant FROM transactions WHERE id='peer1'").fetchone()[0] is None
    assert db.execute("SELECT detailed_category FROM transactions WHERE id='peer2'").fetchone()[0]=='TRANSFER_OUT_PEER_TRANSFER'
    assert any(r['merchant']=='Synthetic Wallet Business' for r in rows(public))
    for value in (person,note,username):
        assert value.encode() not in public.read_bytes()
        assert value.encode() not in Path(db.execute('PRAGMA database_list').fetchone()[2]).read_bytes()
        assert value not in json.dumps(Queries(public).cash_flow('category','2026-10-01','2026-11-01',include_peer_transfers=True))
    assert not business_name(transaction('personbank','2026-10-01',merchant=person,counterparties=[{'type':'person','name':person}]))


@pytest.mark.parametrize('days,frequency,active',[
    (['2026-11-01','2026-11-08'],'weekly',True),
    (['2026-09-01','2026-10-01'],'monthly',True),
    (['2026-07-01','2026-10-01'],'quarterly',True),
    (['2025-01-01','2026-01-01'],'yearly',True),
    (['2026-07-01','2026-08-01'],'monthly',False)])
def test_subscription_frequency_median_month_and_activity(snapshot,days,frequency,active):
    db,public=snapshot
    add(db,[transaction('cadence'+str(i),day,20+2*i) for i,day in enumerate(days)])
    publish(db,public)
    merchant=next(r for r in rows(public,'all_history') if r['merchant']=='Fixture Software')
    assert merchant['recurring'] and merchant['frequency']==frequency
    assert merchant['typical_charge']==21 and merchant['active']==active
    assert merchant['last_charged_month']==days[-1][:7]
    assert merchant['recurring_method']=='cadence_heuristic'


def test_irregular_and_short_history_do_not_guess_subscription(snapshot):
    db,public=snapshot
    add(db,[transaction('i1','2026-09-01',20),transaction('i2','2026-10-20',40)])
    publish(db,public)
    assert not rows(public)[0]['recurring'] and rows(public)[0]['frequency'] is None
    assert not rows(public,recurring_only=True)
    assert 'Short history cannot establish yearly' in Queries.merchant_totals.__doc__


def test_optional_provider_recurrence_strips_stream_details(snapshot):
    db,public=snapshot
    add(db,[transaction('r1','2026-09-01',20),transaction('r2','2026-10-20',20)])
    ingest_recurring(db,'bank',{'outflow_streams':[{'merchant_name':'Fixture Software','frequency':'MONTHLY',
        'last_date':'2026-10-20','is_active':True,'stream_id':'PRIVATE STREAM SENTINEL',
        'description':'PRIVATE STREAM DESCRIPTION','transaction_ids':['PRIVATE STREAM TRANSACTION']}],
        'inflow_streams':[{'description':'PRIVATE INFLOW'}]})
    db.commit();publish(db,public)
    merchant=rows(public)[0]
    assert merchant['frequency']=='monthly' and merchant['recurring_method']=='provider_pattern'
    for value in ('PRIVATE STREAM SENTINEL','PRIVATE STREAM DESCRIPTION','PRIVATE STREAM TRANSACTION','PRIVATE INFLOW'):
        assert value.encode() not in public.read_bytes()
        assert value.encode() not in Path(db.execute('PRAGMA database_list').fetchone()[2]).read_bytes()


def test_audit_allows_verified_business_but_rejects_people_and_small_groups(snapshot):
    db,public=snapshot
    add(db,[transaction('a1','2026-09-01'),transaction('a2','2026-10-01')]);publish(db,public)
    audit_snapshot(public,db,{'Fixture Software','PRIVATE PERSON SENTINEL','PRIVATE NOTE SENTINEL'},
        {'PRIVATE PERSON SENTINEL','PRIVATE NOTE SENTINEL'},{'Fixture Software'})
    for value in ('PRIVATE PERSON SENTINEL','PRIVATE NOTE SENTINEL'):
        publish(db,public)
        with sqlite3.connect(public) as out:out.execute('UPDATE merchants SET merchant=?',(value,))
        with pytest.raises(ValueError):audit_snapshot(public,db,{value},{value},{'Fixture Software'})
    publish(db,public)
    with sqlite3.connect(public) as out:out.execute("UPDATE merchants SET purchase_count=1 WHERE merchant='Fixture Software'")
    with pytest.raises(ValueError):audit_snapshot(public,db,set(),set(),{'Fixture Software'})


def test_audit_shared_metadata_does_not_hide_a_private_label(snapshot):
    db,public=snapshot
    audit_snapshot(public,db,{'Checking','Example'},set(),set())
    with sqlite3.connect(public) as out:out.execute("UPDATE accounts SET display_name='PRIVATE ACCOUNT SENTINEL'")
    with pytest.raises(ValueError):audit_snapshot(public,db,{'Checking','Example','PRIVATE ACCOUNT SENTINEL'},set(),set())


def test_read_only_server_can_raise_the_folding_threshold(snapshot,monkeypatch):
    db,public=snapshot
    add(db,[transaction('s1','2026-09-01'),transaction('s2','2026-10-01')]);publish(db,public)
    monkeypatch.setenv('PLAIDMCP_MIN_GROUP_COUNT','3')
    result=rows(public)[0]
    assert result['merchant']=='Other' and result['total'] is None and result['typical_charge'] is None


def test_audit_allows_description_alias_only_inside_verified_merchant(snapshot):
    db,public=snapshot
    add(db,[transaction('alias1','2026-09-01',merchant='Fixture Software'),transaction('alias2','2026-10-01',merchant='Fixture Software')]);publish(db,public)
    # A provider description can be a substring of its normalized business name.
    audit_snapshot(public,db,{'Fixture','Fixture Software'},set(),{'Fixture Software'})
    with sqlite3.connect(public) as out:out.execute("UPDATE accounts SET display_name='Fixture'")
    with pytest.raises(ValueError):audit_snapshot(public,db,{'Fixture','Fixture Software'},set(),{'Fixture Software'})
