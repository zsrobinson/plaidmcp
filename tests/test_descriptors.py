"""Invented billing descriptions only; common brands are public examples."""
import pytest
from descriptors import descriptor_label,clean_business_label,rail_kind

@pytest.mark.parametrize('raw,label',[
 ('CHECKCARD 0815 ADOBE *CREATIVE CLOUD 4312 SAN FRANCISCO CA','Adobe'),
 ('POS PURCHASE NETFLIX.COM 2026-08-12 REF 007654321','Netflix'),
 ('RECURRING MICROSOFT *365 800-555-0199 STORE #5566','Microsoft'),
 ('GITHUB *COPILOT AUTH XZ998877 09/01','GitHub')])
def test_normalized_fallback(raw,label):
 assert descriptor_label({'name':raw})==label

@pytest.mark.parametrize('raw',[
 'ZELLE JOHN SMITH','VENMO ADOBE','CASH APP NETFLIX','PAYPAL FRIENDS AND FAMILY JOHN DOE',
 'APPLE CASH TO JANE DOE','ATM WITHDRAWAL','CHECK 1234','BANK TRANSFER TO SAVINGS',
 'ADOBE JOHN SMITH CA','MR ALEX JONES LLC','UNKNOWN SOFTWARE 5559998888',
 'ADOBE mystery words','PERSONAL PAYMENT ADOBE','JOHN SMITH',
 'PAYPAL *UNKNOWN PERSON','ADOBE jane@example.invalid'])
def test_uncertain_people_and_rails_not_named(raw):
 assert descriptor_label({'name':raw}) is None


def test_repetition_location_and_truncation():
 assert clean_business_label('Example SoftwareExample SoftwareExam')=='Example Software'
 assert clean_business_label('Fixture Software 00123 Boston MA',{'city':'Boston','region':'MA'})=='Fixture Software'
 assert clean_business_label('Boston',{'city':'Boston','region':'MA'}) is None


def test_only_missing_names_use_fallback():
 from merchants import business_name
 t={'merchant_name':None,'name':'ADOBE *CREATIVE CLOUD','personal_finance_category':{'primary':'GENERAL_MERCHANDISE'}}
 assert business_name(t)=='Adobe'
 assert business_name(t,True) is None
 t['merchant_name']='Unverified Merchant'
 assert business_name(t) is None


def test_transfer_rail_classification_never_needs_person_storage():
 assert rail_kind('ONLINE TRANSFER FROM SAVINGS 001234')=='bank_transfer'
 assert rail_kind('ZELLE PAYMENT FROM JOHN SMITH')=='peer_payment'
 assert rail_kind('NETFLIX.COM') is None


def test_raw_descriptor_not_in_private_public_or_any_response(snapshot):
 import json
 from pathlib import Path
 from store import ingest_transactions,publish
 from queries import Queries
 from server import TOOLS
 from production_audit import audit_snapshot
 db,public=snapshot
 raw='CHECKCARD 0815 ADOBE *CREATIVE CLOUD 4312 SAN FRANCISCO CA AUTH 987654321'
 page={'added':[{'transaction_id':'fallback-'+str(i),'account_id':'check','date':day,'amount':13,
 'merchant_name':None,'original_description':raw,'personal_finance_category':{'primary':'GENERAL_MERCHANDISE'},'iso_currency_code':'USD'} for i,day in enumerate(('2026-09-01','2026-10-01'))]}
 ingest_transactions(db,'bank',page);db.commit();publish(db,public)
 row=next(r for r in Queries(public).merchant_totals()['data']['merchants'] if r['merchant']=='Adobe')
 assert row['merchant_source']=='descriptor_fallback' and row['purchase_count']==2
 assert row['frequency']=='monthly'
 for name in TOOLS:
  encoded=json.dumps(getattr(Queries(public),name)())
  assert raw not in encoded and '987654321' not in encoded
 assert raw.encode() not in public.read_bytes()
 assert raw.encode() not in Path(db.execute('PRAGMA database_list').fetchone()[2]).read_bytes()
 audit_snapshot(public,db,{raw},set(),{'Adobe'})


@pytest.mark.parametrize('descriptor',[
 'ADOBE SUBSCR', 'https://ADOBE.com/billing', 'ADOBE REF AA12345678',
 'ADOBE *CREATIVE CLOUD STORE 1234 2026-08-01',
])
def test_generic_billing_variations(descriptor):
 from merchants import business_name
 assert business_name({'name':descriptor,'merchant_name':None})=='Adobe'
