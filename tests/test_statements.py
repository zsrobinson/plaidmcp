from io import StringIO
import sqlite3
import pytest
from statement_import import parse_statement, import_folder, import_configured, ImportError
from store import publish
from queries import Queries


def test_formats_and_privacy():
    rows=parse_statement(StringIO('Preamble\nTransaction Date,Posted Date,Description,Debit,Credit,Balance\n09/01/2026,09/02/2026,NETFLIX,12,,9988\n2026-09-03,,ZELLE PERSON,,4,9989\nEnding Balance,,,,,\n'))
    assert rows==[('2026-09-01',1200,'UNKNOWN','UNKNOWN','Netflix','activity'),('2026-09-03',-400,'TRANSFER_IN','TRANSFER_IN_PEER_TRANSFER',None,'peer_payment')]
    assert parse_statement(StringIO('Day,Text,Net\n2026-01-01,private account 123456789,-2.10\n'),{'date':'Day','description':'Text','amount':'Net'})[0]==('2026-01-01',210,'UNKNOWN','UNKNOWN',None,'activity')
    assert parse_statement(StringIO('Date,Amount,Type\n2026-01-01,3,Debit\n2026-01-02,2,Credit\n'))[0][1]==300
    with pytest.raises(ImportError,match='Invalid statement CSV'):
        parse_statement(StringIO('Date,Debit,Credit\n2026-01-01,2,3\n'))


def test_cutoff_idempotency_and_rebuild(snapshot,tmp_path,monkeypatch):
    db,public=snapshot
    ref=db.execute("SELECT ref FROM account_alias WHERE provider_ref='check'").fetchone()[0]
    folder=tmp_path/'inbox';folder.mkdir()
    content='Date,Description,Amount,Balance\n2026-09-01,NETFLIX,-12,123456789\n2026-09-01,NETFLIX,-12,123456789\n2026-10-10,PRIVATE PERSON,-99,123456789\n2026-11-01,PRIVATE PERSON,-99,123456789\n'
    for _ in range(2):
        (folder/'private-filename.csv').write_text(content)
        assert import_folder(db,public,folder,ref)==1
        assert db.execute("SELECT COUNT(*) FROM transactions WHERE origin='statement'").fetchone()[0]==2
    assert Queries(public).cash_flow(start='2026-09-01',end='2026-10-01',account_ids=[ref])['data']['totals'][0]['spending']==24
    db.execute("DELETE FROM transactions WHERE item='bank' AND origin='plaid'")
    assert db.execute("SELECT COUNT(*) FROM transactions WHERE origin='statement'").fetchone()[0]==2
    with pytest.raises(ImportError,match='posted linked history'): import_folder(db,public,folder,ref)
    db.commit()
    for path in (public,tmp_path/'private.sqlite'):
        data=path.read_bytes()
        for marker in (b'PRIVATE PERSON',b'123456789',b'private-filename'): assert marker not in data


def test_bad_configuration_and_symlink(snapshot,tmp_path,monkeypatch):
    db,public=snapshot
    monkeypatch.setenv('PLAIDMCP_STATEMENT_ACCOUNTS','{"../escape":"acct_unknown"}')
    with pytest.raises(ImportError,match='Invalid statement import configuration'): import_configured(db,public)
    ref=db.execute("SELECT ref FROM account_alias WHERE provider_ref='check'").fetchone()[0]
    folder=tmp_path/'inbox';folder.mkdir();(folder/'bad.csv').symlink_to(tmp_path/'missing')
    with pytest.raises(ImportError,match='Statement inbox operation failed'): import_folder(db,public,folder,ref)


def test_real_rebuild_preserves_imported_rows(snapshot,tmp_path,monkeypatch):
    from sync import sync_item
    db,public=snapshot
    ref=db.execute("SELECT ref FROM account_alias WHERE provider_ref='check'").fetchone()[0]
    folder=tmp_path/'inbox';folder.mkdir()
    (folder/'history.csv').write_text('Date,Amount\n2026-09-01,-2\n')
    import_folder(db,public,folder,ref)
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY,institution_id TEXT,token TEXT,product TEXT,cursor TEXT)')
    db.execute("INSERT INTO credentials VALUES('bank','fake-ins','fake-token','transactions','old')");db.commit()
    class Client:
        def call(self,endpoint,**payload):
            if endpoint=='/item/get': return {'item':{}}
            if endpoint=='/institutions/get_by_id': return {'institution':{'name':'Example Bank'}}
            if endpoint=='/accounts/get': return {'accounts':[]}
            if endpoint=='/transactions/recurring/get': return {}
            return {'added':[],'has_more':False,'next_cursor':'new'}
    monkeypatch.setenv('PLAIDMCP_BACKFILL_MERCHANTS','true')
    sync_item(db,Client(),('bank','fake-ins','fake-token','transactions','old'))
    assert db.execute("SELECT amount FROM transactions WHERE origin='statement'").fetchall()==[(2.0,)]
    publish(db,public)
    assert Queries(public).cash_flow(start='2026-09-01',end='2026-10-01',account_ids=[ref])['data']['totals'][0]['spending']==2


@pytest.mark.parametrize('description',[
    'PAYMENT THANK YOU','AUTOPAY','MOBILE PYMT','ONLINE PAYMENT',
    'CREDIT CARD PAYMENT','CRCARDPMT','PAYMENT - THANK YOU #1234',
    'Auto Pay 1234','CRCARDPMT: 1234',
])
@pytest.mark.parametrize('direction',['Debit','Credit'])
def test_card_payment_descriptors(description,direction):
    row=parse_statement(StringIO(f'Date,Description,Debit,Credit\n2026-09-01,{description},'+('25,\n' if direction=='Debit' else ',25\n')))[0]
    assert row[1]==(2500 if direction=='Debit' else -2500)
    assert row[2:]==('LOAN_PAYMENTS','LOAN_PAYMENTS_CREDIT_CARD_PAYMENT',None,'bank_transfer')


@pytest.mark.parametrize('category,primary',[
    ('Dining','FOOD_AND_DRINK'),('Restaurants','FOOD_AND_DRINK'),('Groceries','FOOD_AND_DRINK'),
    ('Gas','TRANSPORTATION'),('Fuel','TRANSPORTATION'),('Automotive','TRANSPORTATION'),('Transportation','TRANSPORTATION'),
    ('Merchandise','GENERAL_MERCHANDISE'),('Shopping','GENERAL_MERCHANDISE'),
    ('Entertainment','ENTERTAINMENT'),('Travel','TRAVEL'),('Airfare','TRAVEL'),('Lodging','TRAVEL'),
    ('Health','MEDICAL'),('Medical','MEDICAL'),('Fee','BANK_FEES'),('Interest','BANK_FEES'),
    ('Payment','LOAN_PAYMENTS'),('Credit','LOAN_PAYMENTS'),('Private category sentinel','UNKNOWN'),
])
def test_generic_export_category_mapping(category,primary):
    row=parse_statement(StringIO(f'Date,Description,Amount,Transaction Category\n2026-09-01,unknown text,-3,{category}\n'))[0]
    assert row[2]==primary
    if primary=='LOAN_PAYMENTS':
        assert row[3:]==('LOAN_PAYMENTS_CREDIT_CARD_PAYMENT',None,'bank_transfer')
    else:
        assert row[3]=='UNKNOWN' and row[5]=='activity'


@pytest.mark.parametrize('description',[
    'NETFLIX AUTOPAY','PAYMENT TO JANE','CREDIT CARD PURCHASE','ONLINE PAYMENT STORE',
    'MYCRCARDPMT SHOP','AUTOPAYMENT SERVICES',
])
def test_payment_pattern_does_not_promote_arbitrary_text(description):
    row=parse_statement(StringIO(f'Date,Description,Amount\n2026-09-01,{description},-3\n'))[0]
    assert row[2:4]==('UNKNOWN','UNKNOWN') and row[5]=='activity'


def test_category_column_mapping_and_peer_precedence():
    row=parse_statement(StringIO('Date,Description,Amount,Export Kind\n2026-09-01,NETFLIX,-3, Dining \n'),{'category':'Export Kind'})[0]
    assert row[2]=='FOOD_AND_DRINK' and row[4]=='Netflix'
    row=parse_statement(StringIO('Date,Description,Amount,Category\n2026-09-01,ZELLE PERSON,-3,Payment\n'))[0]
    assert row[2:]==('TRANSFER_OUT','TRANSFER_OUT_PEER_TRANSFER',None,'peer_payment')


@pytest.mark.parametrize('use_category',[False,True])
def test_imported_card_payments_match_and_do_not_distort_spending(snapshot,tmp_path,use_category):
    from store import ingest_transactions
    db,public=snapshot
    # Both linked accounts have later posted history; September is CSV backfill.
    ingest_transactions(db,'bank',{'added':[{
        'transaction_id':'synthetic-card-history','account_id':'card','date':'2026-10-10',
        'amount':1,'iso_currency_code':'USD'}]})
    db.commit()
    ids=dict(db.execute('SELECT provider_ref,ref FROM account_alias'))
    publish(db,public)
    def merchant_amounts():
        return [(r['merchant'],r['total'],r['purchase_count']) for r in Queries(public).merchant_totals(window='all_history')['data']['merchants']]
    before_merchants=merchant_amounts()
    folder=tmp_path/'inbox';folder.mkdir()
    headers='Date,Description,Debit,Credit,Category\n'
    # First publish one leg: it must be an unmatched bank transfer, never spending.
    out_description='unknown descriptor' if use_category else 'CREDIT CARD PAYMENT'
    in_description='unknown descriptor' if use_category else 'PAYMENT THANK YOU'
    category='Payment' if use_category else ''
    (folder/'checking.csv').write_text(headers+f'2026-09-01,{out_description},25,,{category}\n')
    import_folder(db,public,folder,ids['check'])
    q=Queries(public)
    period={'start':'2026-09-01','end':'2026-10-01'}
    assert not q.cash_flow(**period)['data']['totals']
    unmatched=q.cash_flow(include_transfers=True,**period)['data']['totals'][0]
    assert unmatched['spending']==0 and unmatched['transfers_out']==25 and unmatched['refunds']==0
    (folder/'card.csv').write_text(headers+f'2026-09-02,{in_description},,25,{category}\n')
    import_folder(db,public,folder,ids['card'])
    assert db.execute('SELECT COUNT(*) FROM internal_matches').fetchone()[0]==2
    rows=db.execute("SELECT primary_category,detailed_category,flow_kind,merchant FROM transactions WHERE origin='statement'").fetchall()
    assert rows==[('LOAN_PAYMENTS','LOAN_PAYMENTS_CREDIT_CARD_PAYMENT','bank_transfer',None)]*2
    total=q.cash_flow(include_transfers=True,**period)['data']['totals'][0]
    assert total['spending']==total['income']==total['refunds']==total['transfers_out']==total['transfers_in']==total['net']==0
    assert total['internal_transfers_in']==total['internal_transfers_out']==25
    assert merchant_amounts()==before_merchants
    for path in (public,tmp_path/'private.sqlite'):
        data=path.read_bytes()
        for marker in (out_description,in_description,category):
            if marker: assert marker.encode() not in data


def test_imported_categories_are_fixed_codes_not_raw_cells(snapshot,tmp_path):
    db,public=snapshot
    ref=db.execute("SELECT ref FROM account_alias WHERE provider_ref='check'").fetchone()[0]
    folder=tmp_path/'inbox';folder.mkdir()
    (folder/'categories.csv').write_text('Date,Amount,Category\n2026-09-01,-3,rEsTaUrAnTs\n2026-09-02,-4,PRIVATE CATEGORY SENTINEL\n')
    import_folder(db,public,folder,ref)
    assert db.execute("SELECT primary_category FROM transactions WHERE origin='statement' ORDER BY date").fetchall()==[('FOOD_AND_DRINK',),('UNKNOWN',)]
    groups=Queries(public).cash_flow(group_by='category',start='2026-09-01',end='2026-10-01')['data']['groups']
    assert any(r['group']=='food' and r['spending']==3 for r in groups)
    for path in (public,tmp_path/'private.sqlite'):
        for marker in (b'rEsTaUrAnTs',b'PRIVATE CATEGORY SENTINEL'): assert marker not in path.read_bytes()
