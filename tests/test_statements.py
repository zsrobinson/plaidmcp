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
