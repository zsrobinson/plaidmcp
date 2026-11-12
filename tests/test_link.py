import json
import threading
import urllib.request
import urllib.error
import pytest
import link
from sync import private_store

@pytest.mark.parametrize('selection',['id','name'])
async def test_link_local_csrf_single_use_and_duplicate(tmp_path,monkeypatch,selection):
    monkeypatch.setenv('PLAIDMCP_PRIVATE',str(tmp_path/'private'))
    calls=[]
    class FakeClient:
        def call(self,path,**payload):
            calls.append((path,payload))
            if path=='/link/token/create': return {'link_token':'fake-link'}
            return {'item_id':'item','access_token':'fake-access'}
    monkeypatch.setattr(link,'Client',FakeClient)
    monkeypatch.setattr('sys.argv',['link.py','--product','transactions']+(['--institution','ins_1'] if selection=='id' else ['--institution-name','Example Bank']))
    import sqlite3
    import sync
    from store import SCHEMA
    def threaded_store(path):
        db=sqlite3.connect(path,check_same_thread=False)
        db.executescript(SCHEMA)
        return db
    monkeypatch.setattr(sync,'connect',threaded_store)
    real=link.http.server.HTTPServer
    class Done(Exception): pass
    class LocalServer(real):
        def __init__(self,address,handler):
            assert address==('127.0.0.1',8768)
            super().__init__(('127.0.0.1',0),handler)
        def serve_forever(self):
            # Exercise the actual helper handler without touching its fixed production port.
            thread=threading.Thread(target=super().serve_forever); thread.start()
            base=f'http://127.0.0.1:{self.server_port}'
            def request(path,csrf=None,body=b'{}'):
                headers={'Host':'127.0.0.1:8768'}
                if csrf: headers['X-CSRF']=csrf
                req=urllib.request.Request(base+path,body,headers)
                try:
                    with urllib.request.urlopen(req) as r: return r.status,json.load(r)
                except urllib.error.HTTPError as e: return e.code,{}
            try:
                with urllib.request.urlopen(urllib.request.Request(base,headers={'Host':'127.0.0.1:8768'})) as r:
                    html=r.read().decode()
                csrf=html.split('const csrf=')[1].split(';')[0].strip('"')
                assert request('/token')[0]==403
                assert request('/token',csrf)[0]==200
                assert calls[-1][1]['products']==['transactions']
                assert request('/token',csrf)[0]==409
                assert request('/exchange',csrf,json.dumps({'public_token':'fake-public','institution_id':'ins_1','institution_name':'Example Bank'}).encode())[0]==200
                assert request('/exchange',csrf)[0]==409
            finally:
                self.shutdown(); thread.join(); self.server_close()
            raise Done()
    monkeypatch.setattr(link.http.server,'HTTPServer',LocalServer)
    with pytest.raises(Done): link.main()
    with private_store() as db:
        assert db.execute('SELECT token FROM credentials').fetchone()[0]=='fake-access'
    with pytest.raises(SystemExit,match='already linked'): link.main()
    assert len(calls)==2


def test_initial_history_payload():
    assert link.token_payload('transactions')['transactions']=={'days_requested':730}
    assert link.token_payload('transactions',days_requested=30)['transactions']=={'days_requested':30}
    assert 'transactions' not in link.token_payload('investments')
    assert 'transactions' not in link.token_payload('transactions',('item','fake-token'))
    for value in (29,731,'bad'):
        with pytest.raises(link.argparse.ArgumentTypeError): link.token_payload('transactions',days_requested=value)
