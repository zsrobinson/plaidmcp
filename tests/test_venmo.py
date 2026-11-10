import io
import json
import sqlite3
from pathlib import Path

import pytest
from queries import Queries
from server import create_server, TOOLS
from store import connect, ingest_transactions, publish
from tests.test_queries import ARGS
from venmo_import import ImportError, parse_statement, import_folder, column_map

PRIVATE = ('ULTRA PRIVATE NOTE 💛', 'Sensitive Person', '@secret_username', 'RAW-ID-998877')
CSV = '''Venmo monthly statement,private profile
,ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (fee),Username
,RAW-ID-998877,2026-11-01T10:11:12,Payment,Complete,ULTRA PRIVATE NOTE 💛,Sensitive Person,Someone,-$20.00,$0.00,@secret_username
,second,2026-11-02 12:00:00,Payment,Completed,ULTRA PRIVATE NOTE 💛,Someone,Sensitive Person,+$8.00,$0.00,@secret_username
,third,2026-11-03,Purchase,Complete,note,name,name,-$10.00,$0.00,user
,fourth,2026-11-04,Standard Transfer,Complete,note,name,name,-$50.00,$0.00,user
,fifth,2026-11-05,Add Funds,Complete,note,name,name,+$100.00,$0.00,user
,sixth,2026-11-06,Fee,Complete,note,name,name,-$1.00,$0.00,user
,seventh,2026-11-07,Payment,Pending,note,name,name,-$900.00,$0.00,user
Ending Balance,,,,,,,,,
'''

@pytest.fixture
def imported(tmp_path):
    folder=tmp_path/'inbox'; folder.mkdir()
    public=tmp_path/'aggregate.sqlite'; private=tmp_path/'private.sqlite'
    db=connect(private)
    (folder/'statement.csv').write_text(CSV,encoding='utf-8-sig')
    assert import_folder(db,public,folder)==1
    yield db,public,private,folder
    db.close()

async def test_privacy_and_peer_options(imported):
    db,public,private,folder=imported
    assert not list(folder.iterdir())
    assert db.execute('SELECT * FROM venmo_rows ORDER BY date').fetchall()==[
        ('2026-11-01',2000,'out','peer_sent'),('2026-11-02',800,'in','peer_received'),
        ('2026-11-03',1000,'out','purchase'),('2026-11-04',5000,'out','bank_transfer'),
        ('2026-11-05',10000,'in','bank_transfer'),('2026-11-06',100,'out','fee')]
    assert [r[1] for r in db.execute('PRAGMA table_info(venmo_rows)')]==['date','cents','direction','category']
    q=Queries(public)
    assert q.cash_flow('month','2026-11-01','2026-12-01')['data']['totals'][0]['spending']==11
    opted=q.cash_flow('month','2026-11-01','2026-12-01',include_peer_transfers=True)['data']['totals'][0]
    assert opted['spending']==31 and opted['income']==8 and opted['net']==-23
    assert q.list_accounts()['data'][0]['data_kinds']==['spending']
    mcp=create_server(public)
    for name in TOOLS:
        for include in (False,True):
            args=dict(ARGS.get(name,{}))
            if name=='cash_flow': args['include_peer_transfers']=include
            result=await mcp.call_tool(name,args)
            text=json.dumps(result,default=str,ensure_ascii=False)
            for value in PRIVATE+('counterparty','username','transaction_id','payment_note'):
                assert value not in text
    for path in (private,public):
        for value in PRIVATE: assert value.encode() not in path.read_bytes()


def test_month_replacement_preserves_multiplicity(imported):
    db,public,private,folder=imported
    # Same month re-export under a different filename, with changed discarded notes/IDs.
    (folder/'new-name.csv').write_text(CSV.replace('ULTRA PRIVATE NOTE 💛','another private note').replace('RAW-ID-998877','new-id'))
    import_folder(db,public,folder)
    assert db.execute('SELECT COUNT(*) FROM venmo_rows').fetchone()[0]==6
    # Revised month replaces rather than adds; genuinely identical rows remain distinct.
    text='Date,Type,Amount\n11/01/2026,Payment,-$5.00\n11/01/2026,Payment,-$5.00\n'
    (folder/'revision.csv').write_text(text)
    import_folder(db,public,folder)
    assert db.execute('SELECT COUNT(*) FROM venmo_rows').fetchone()[0]==2
    assert Queries(public).cash_flow('month','2026-11-01','2026-12-01',include_peer_transfers=True)['data']['totals'][0]['spending']==10
    (folder/'october.csv').write_text(text.replace('11/01/2026','10/01/2026'))
    import_folder(db,public,folder)
    assert db.execute('SELECT COUNT(*) FROM venmo_rows').fetchone()[0]==4


def test_aliases_and_mapping():
    text='Posted On,Kind,Net USD,Flow,Notes\n11/2/2026,Peer Sent,"1,234.56",debit,ULTRA PRIVATE NOTE 💛\n'
    mapping=column_map('{"date":"Posted On","amount":"Net USD","type":"Kind","direction":"Flow"}')
    month,rows=parse_statement(io.StringIO(text),mapping)
    assert (month,rows)==('2026-11',[('2026-11-02',123456,'out','peer_sent')])
    month,rows=parse_statement(io.StringIO('Timestamp,Transaction Type,Total Amount\n2026-11-01,Payment,($5.01)\n'))
    assert rows[0][1:]==(501,'out','peer_sent')
    with pytest.raises(ImportError): column_map('{"note":"Private"}')

@pytest.mark.parametrize('row',[
    '2026-11-01,Payment,20', # direction ambiguous: never infer from names
    'bad-date,Payment,-20',
    '2026-11-01,ULTRA PRIVATE NOTE 💛,-20',
    '2026-11-01,Payment,NaN',
    '2026-11-01,Peer Received,-20',
])
def test_bad_rows_are_generic(row):
    with pytest.raises(ImportError) as error: parse_statement(io.StringIO('Date,Type,Amount\n'+row+'\n'))
    for value in PRIVATE: assert value not in str(error.value)


def test_failed_parse_and_publish_retain_raw(imported,monkeypatch):
    db,public,private,folder=imported
    raw=folder/'broken.csv'; raw.write_text('Date,Type,Amount\n2026-11-01,Payment,ambiguous\n')
    before=public.read_bytes()
    with pytest.raises(ImportError): import_folder(db,public,folder)
    assert raw.exists() and public.read_bytes()==before
    raw.write_text(CSV)
    def broken(*args): raise OSError('fixed test failure')
    with monkeypatch.context() as m:
        m.setattr('venmo_import.publish',broken)
        with pytest.raises(OSError): import_folder(db,public,folder)
    assert raw.exists() and public.read_bytes()==before
    import_folder(db,public,folder)
    assert not raw.exists() and db.execute('SELECT COUNT(*) FROM venmo_rows').fetchone()[0]==6


def test_move_and_source_overlap(imported,tmp_path):
    db,public,private,folder=imported
    raw=folder/'raw.csv'; raw.write_text(CSV)
    archive=tmp_path/'archive'
    import_folder(db,public,folder,action='move',archive=archive)
    assert not raw.exists() and len(list(archive.glob('2026-11-*.csv')))==1
    assert (archive.stat().st_mode & 0o777)==0o700
    raw.write_text(CSV)
    with db: db.execute("INSERT INTO items VALUES('plaid-venmo','Venmo Personal',NULL,NULL)")
    with pytest.raises(ImportError,match='double count'): import_folder(db,public,folder)
    assert raw.exists()
    with pytest.raises(ImportError): import_folder(db,public,folder,action='keep')


def test_plaid_peer_categories_and_discarded_notes(snapshot):
    db,public=snapshot
    with db:
        db.execute("INSERT INTO items VALUES('venmo','Venmo Personal','2026-11-10',NULL)")
        for n,primary,detail,amount in [('p','TRANSFER_OUT','TRANSFER_OUT_OTHER_TRANSFER',20),
                                      ('r','TRANSFER_IN','TRANSFER_IN_OTHER_TRANSFER',-8),
                                      ('b','TRANSFER_OUT','TRANSFER_OUT_ACCOUNT_TRANSFER',50)]:
            ingest_transactions(db,'venmo',{'added':[{'transaction_id':n,'date':'2026-11-03','amount':amount,
                'iso_currency_code':'USD','personal_finance_category':{'primary':primary,'detailed':detail},
                'name':PRIVATE[0],'payment_note':PRIVATE[0],'merchant_name':PRIVATE[1],
                'counterparties':[{'name':PRIVATE[1],'username':PRIVATE[2]}]}]})
    publish(db,public)
    q=Queries(public)
    assert q.cash_flow('month','2026-11-01','2026-12-01')['data']['totals'][0]['spending']==110
    opted=q.cash_flow('month','2026-11-01','2026-12-01',include_peer_transfers=True)['data']['totals'][0]
    assert opted['spending']==130 and opted['income']==1008
    for value in PRIVATE:
        assert value not in json.dumps(q.cash_flow('category','2026-11-01','2026-12-01',include_peer_transfers=True))
        assert value.encode() not in public.read_bytes()
        assert value.encode() not in Path(db.execute('PRAGMA database_list').fetchone()[2]).read_bytes()


def test_fee_column_modes():
    text='Date,Type,Amount (total),Amount (fee)\n2026-11-01,Payment,-$20.00,$1.00\n'
    with pytest.raises(ImportError,match='fee column'): parse_statement(io.StringIO(text))
    _,rows=parse_statement(io.StringIO(text),fee_mode='separate')
    assert rows==[('2026-11-01',100,'out','fee'),('2026-11-01',2000,'out','peer_sent')]
    _,rows=parse_statement(io.StringIO(text),fee_mode='included')
    assert rows[-1][1]==1900


def test_multiple_months_and_symlink_rejected(imported,tmp_path):
    db,public,private,folder=imported
    raw=folder/'raw.csv'
    raw.write_text('Date,Type,Amount\n2026-11-01,Payment,-1\n2026-10-01,Payment,-2\n')
    with pytest.raises(ImportError,match='exactly one'): import_folder(db,public,folder)
    raw.unlink(); raw.symlink_to(private)
    with pytest.raises(OSError): import_folder(db,public,folder)


def test_csv_only_sync_needs_no_keys(imported,monkeypatch):
    db,public,private,folder=imported
    import sync
    from contextlib import contextmanager
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY, institution_id TEXT UNIQUE, token TEXT, product TEXT, cursor TEXT)')
    @contextmanager
    def fake_store(): yield db
    def no_client(): raise AssertionError('CSV-only sync must not construct Plaid Client')
    monkeypatch.setattr(sync,'private_store',fake_store)
    monkeypatch.setattr(sync,'Client',no_client)
    monkeypatch.setenv('PLAIDMCP_VENMO_IMPORT_DIR',str(folder)); monkeypatch.setenv('PLAIDMCP_PUBLIC',str(public))
    (folder/'new.csv').write_text(CSV)
    sync.main()
    assert not list(folder.iterdir())


def test_csv_blocks_new_venmo_link(imported,monkeypatch):
    import link
    db,_,_,_=imported
    from contextlib import contextmanager
    @contextmanager
    def fake_store(): yield db
    db.execute('CREATE TABLE credentials(item TEXT PRIMARY KEY, institution_id TEXT UNIQUE, token TEXT, product TEXT, cursor TEXT)')
    class Fake:
        def call(self,path,**kwargs):
            assert path=='/institutions/get_by_id'
            return {'institution':{'name':'Venmo Personal'}}
    monkeypatch.setattr(link,'private_store',fake_store)
    monkeypatch.setattr(link,'Client',Fake)
    monkeypatch.setattr('sys.argv',['link.py','--institution','unverified-test-id','--product','transactions'])
    with pytest.raises(SystemExit,match='CSV source exists'): link.main()


def test_late_plaid_overlap_does_not_ingest(imported):
    from sync import sync_item
    db,_,_,_=imported
    with db: db.execute("INSERT INTO items VALUES('new-plaid','unverified-test-id',NULL,NULL)")
    class Fake:
        def call(self,path,**kwargs):
            if path=='/item/get': return {'item':{}}
            assert path=='/institutions/get_by_id'
            return {'institution':{'name':'Venmo Personal'}}
    sync_item(db,Fake(),('new-plaid','unverified-test-id','fake-token','transactions',None))
    assert db.execute("SELECT error FROM items WHERE id='new-plaid'").fetchone()[0]=='PLAIDMCP_VENMO_SOURCE_OVERLAP'
    assert db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==0
