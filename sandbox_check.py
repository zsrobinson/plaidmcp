"""Explicit opt-in live Sandbox check; never prints raw responses, credentials or tokens."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

from plaid_client import Client, PlaidError, SANDBOX_CREATE
import sync
from queries import Queries, month_bounds
from sources import category as plaidmcp_category
from store import connect, ingest_transactions, publish
from server import TOOLS

class CheckError(RuntimeError):
    pass


ROOT = Path.home()/'.local/state/plaidmcp-sandbox'
ENV = Path.home()/'.config/plaidmcp/sandbox.env'


def configure():
    # Parse data, never source shell text. Do not copy credentials to another file.
    if ENV.stat().st_mode & 0o077 or ENV.parent.stat().st_mode & 0o077:
        raise ValueError('Private env permissions required')
    values = dict(line.split('=',1) for line in ENV.read_text().splitlines() if line)
    if set(values) != {'PLAID_ENV','PLAID_CLIENT_ID','PLAID_SECRET'} or values['PLAID_ENV'] != 'sandbox':
        raise ValueError('Exact Sandbox configuration required')
    os.environ.update(values)
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT,0o700)
    if sys.argv[1:] not in ([],['--resume']): raise ValueError('Use --resume or no arguments')
    run = sorted(ROOT.glob('run-*'))[-1] if sys.argv[1:] else ROOT / datetime.now(timezone.utc).strftime('run-%Y%m%dT%H%M%S%f')
    run.mkdir(mode=0o700,exist_ok=True)
    os.environ['PLAIDMCP_PRIVATE'] = str(run/'private')
    os.environ['PLAIDMCP_PUBLIC'] = str(run/'aggregate.sqlite')
    os.environ['PLAIDMCP_VENMO_IMPORT_DIR'] = str(run/'unused-inbox')
    return run


class AuditClient(Client):
    def __init__(self):
        super().__init__()
        if self.env != 'sandbox': raise ValueError('Sandbox required')
        self.private_strings = set(self.credentials.values())
        self.approved_business=set()
        self.strict_strings=set()
        self.category_versions = set()
        self.categories = set()
        self.saw_v2_request = False
        self.transaction_template = None

    def call(self, endpoint, **payload):
        if self.base != 'https://sandbox.plaid.com': raise ValueError('Sandbox required')
        if endpoint == '/transactions/sync':
            if payload.get('options',{}).get('personal_finance_category_version') != 'v2':
                raise ValueError('PFCv2 request required')
            self.saw_v2_request = True
        result = super().call(endpoint, **payload)
        for key in ('public_token','access_token','item_id'):
            if result.get(key): self.private_strings.add(result[key])
        item=result.get('item') or {}
        if item.get('item_id'): self.private_strings.add(item['item_id'])
        for account in result.get('accounts',[]):
            for key in ('account_id','persistent_account_id','name','official_name'):
                if account.get(key): self.private_strings.add(account[key])
        for transaction in result.get('added',[]) + result.get('modified',[]):
            from merchants import business_name
            business=business_name(transaction)
            if business:self.approved_business.add(business)
            if self.transaction_template is None: self.transaction_template = transaction
            for key in ('name','merchant_name','original_description','description','note','notes','payment_note','transaction_id','account_id'):
                value = transaction.get(key)
                if isinstance(value,str) and value.strip(): self.private_strings.add(value)
            for key in ('note','notes','payment_note','username'):
                if isinstance(transaction.get(key),str) and transaction[key]:self.strict_strings.add(transaction[key])
            for counterparty in transaction.get('counterparties',[]) or []:
                for key in ('name','username'):
                    if counterparty.get(key):
                        self.private_strings.add(counterparty[key])
                        if key=='username' or (counterparty.get('type') in ('person','unknown') and not (business and counterparty.get('type')=='unknown' and __import__('descriptors').descriptor_label({'name':counterparty.get('name')})==business)):self.strict_strings.add(counterparty[key])
            category = transaction.get('personal_finance_category') or {}
            if category.get('primary'): self.categories.add(category['primary'])
            version = transaction.get('personal_finance_category_version')
            if version: self.category_versions.add(version)
        return result


def prepare(client):
    with sync.private_store() as db:
        if not db.execute('SELECT 1 FROM credentials').fetchone():
            token = client.call(SANDBOX_CREATE, institution_id='ins_109508',
                initial_products=['transactions','investments'])['public_token']
            exchanged = client.call('/item/public_token/exchange', public_token=token)
            with db:
                db.execute('INSERT INTO credentials VALUES(?,?,?,?,NULL)',(exchanged['item_id'],
                    'ins_109508',exchanged['access_token'],'transactions,investments'))
                db.execute('INSERT INTO items VALUES(?,?,NULL,?)',(exchanged['item_id'],
                    'Sandbox initialization','INITIAL_SYNC_REQUIRED'))
        for item,token in db.execute('SELECT item,token FROM credentials').fetchall():
            client.private_strings.add(token)
            cursor = None
            for _ in range(100):
                kwargs = {'access_token':token,'options':{'personal_finance_category_version':'v2','include_original_description':True},'count':500}
                if cursor: kwargs['cursor'] = cursor
                response = client.call('/transactions/sync',**kwargs)
                ingest_transactions(db,item,response)
                if not response['has_more']: break
                cursor = response['next_cursor']
            else: raise CheckError('Privacy audit pagination limit')
        db.commit()
    # New Sandbox Items prepare transactions asynchronously. Re-run the real sync,
    # preserving its real cursor, until posted activity and holdings are present.
    original = sync.Client
    sync.Client = lambda: client
    try:
        for attempt in range(30):
            sync.main()
            with sync.private_store() as db:
                errors = [r[0] for r in db.execute('SELECT error FROM items') if r[0]]
                posted = db.execute('SELECT COUNT(*) FROM transactions WHERE pending=0').fetchone()[0]
                holdings = db.execute('SELECT COUNT(*) FROM holdings').fetchone()[0]
                if errors and set(errors) - {'PRODUCT_NOT_READY'}: raise PlaidError(errors[0])
                if posted and holdings and not errors: return
            time.sleep(2)
        raise CheckError('Sandbox preparation did not finish')
    finally: sync.Client = original


def check_private_strings(value, forbidden):
    # Long source strings are exact byte scans. Short names also occur as fragments
    # of legitimate security names/type enums; require a whole token for those.
    for secret in forbidden:
        needle = secret.encode()
        if len(secret) <= 3:
            found = re.search(rb'(?<![A-Za-z0-9])'+re.escape(needle)+rb'(?![A-Za-z0-9])',value,re.I)
        else:
            found = needle.lower() in value.lower()
        if found: raise CheckError('Privacy audit detected a prohibited string')


def check_fields(value):
    banned = {'transaction_id','merchant_name','description','original_description','counterparties',
              'counterparty','note','notes','payment_note','username','access_token','public_token','account_id','item_id','routing_number','routing_numbers','account_number','account_numbers','owner_name','owners','addresses','emails','phones','phone_numbers','identity'}
    if isinstance(value,dict):
        if set(value) & banned: raise CheckError('Privacy audit detected a prohibited field')
        for child in value.values(): check_fields(child)
    elif isinstance(value,list):
        for child in value: check_fields(child)


def check_peer_shape(run, client):
    """Probe classification on real response shape; not a claim of real Venmo support."""
    if not client.transaction_template: raise CheckError('No real response shape for peer probe')
    private = run/'peer-probe.sqlite'
    public = run/'peer-aggregate.sqlite'
    db = connect(private)
    marker = 'PRIVACY_NOTE_SENTINEL_FOR_PEER_PROBE'
    try:
        db.execute("INSERT OR REPLACE INTO items VALUES('peer-probe','Venmo Personal',?,NULL)",
            (datetime.now(timezone.utc).isoformat(),))
        template = client.transaction_template
        month = template['date'][:7]
        for suffix,primary,detailed,amount in (
            ('sent','TRANSFER_OUT','TRANSFER_OUT_OTHER_TRANSFER',12.34),
            ('received','TRANSFER_IN','TRANSFER_IN_OTHER_TRANSFER',-5),
            ('bank','TRANSFER_OUT','TRANSFER_OUT_ACCOUNT_TRANSFER',50)):
            transaction = {**template, 'transaction_id':'peer-probe-'+suffix,'amount':amount,
                'pending':False,'iso_currency_code':'USD','note':marker,'payment_note':marker,
                'counterparties':[{'name':marker,'username':marker}],
                'personal_finance_category':{'primary':primary,'detailed':detailed}}
            ingest_transactions(db,'peer-probe',{'added':[transaction]})
        db.commit(); publish(db,public)
        q = Queries(public)
        start,end=month_bounds(month)
        baseline=q.cash_flow('category',start,end)['data']['groups']
        included=q.cash_flow('category',start,end,include_peer_transfers=True)['data']['totals']
        flow=included
        if baseline or sum(r['spending'] for r in included)!=12.34 or flow[0]['income']!=5:
            raise CheckError('Peer response-shape probe failed')
        prohibited = client.private_strings | {marker}
        check_private_strings(public.read_bytes(),prohibited)
        check_private_strings(json.dumps([baseline,included,flow]).encode(),prohibited)
        for value in (baseline,included,flow): check_fields(value)
        return True
    finally: db.close()


async def check_mcp(run, client):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    # No Plaid credentials are inherited by the separate server process.
    env = {k:v for k,v in os.environ.items() if not k.startswith(('PLAID_','ACCESS_'))}
    with socket.socket() as probe: probe.bind(('127.0.0.1',8767))
    process = subprocess.Popen([sys.executable,'server.py'],cwd=Path(__file__).parent,
        env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if process.poll() is not None: raise CheckError('Local MCP startup failed')
            try:
                with socket.create_connection(('127.0.0.1',8767),timeout=.2): break
            except OSError: await asyncio.sleep(.05)
        else: raise CheckError('Local MCP startup timed out')
        with sync.private_store() as db:
            month = db.execute('SELECT MAX(substr(date,1,7)) FROM transactions WHERE pending=0').fetchone()[0]
        start,end=month_bounds(month)
        args={'cash_flow':{'start':start,'end':end}}
        samples = {}
        async with streamable_http_client('http://127.0.0.1:8767/mcp') as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                if {t.name for t in (await session.list_tools()).tools} != set(TOOLS):
                    raise CheckError('Tool listing mismatch')
                async def call(name,arguments):
                    result = await session.call_tool(name,arguments)
                    if result.isError: raise CheckError('MCP tool failed')
                    value = result.structuredContent or json.loads(result.content[0].text)
                    check_fields(value)
                    check_private_strings(json.dumps(value,ensure_ascii=False).encode(),client.private_strings-client.approved_business)
                    if not value['as_of'] or value['unreliable_sources']: raise CheckError('Snapshot is not ready')
                    return value
                results = await asyncio.wait_for(asyncio.gather(*(call(n,args.get(n,{})) for n in TOOLS)),30)
                samples = dict(zip(TOOLS,results))
                for grouping in ('category','category_detailed','month','week','account','institution'):
                    await call('cash_flow',{'group_by':grouping,'start':start,'end':end,'include_peer_transfers':True,'include_transfers':True})
                for grouping in ('security','account','asset_class','security_type'): await call('holdings',{'group_by':grouping})
                accounts=samples['list_accounts']['data']
                if accounts: await call('cash_flow',{'start':start,'end':end,'account_ids':[accounts[0]['id']]})
        snapshot = run/'aggregate.sqlite'
        from production_audit import audit_snapshot
        with sync.private_store() as private:
            audit_snapshot(snapshot,private,client.private_strings,client.strict_strings,client.approved_business)
        with sqlite3.connect(snapshot) as db:
            if {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} != {'accounts','daily','holdings','sources','merchants'}:
                raise CheckError('Unexpected public tables')
            for table in ('accounts','daily','holdings','sources','merchants'):
                for row in db.execute('SELECT * FROM '+table):
                    for cell in row:
                        if isinstance(cell,str): check_private_strings(cell.encode(),client.private_strings-client.approved_business)
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise CheckError('Snapshot integrity failed')
        if not client.saw_v2_request or not client.categories or 'UNKNOWN' in client.categories:
            raise CheckError('PFCv2 data missing')
        if client.category_versions and client.category_versions != {'v2'}:
            raise CheckError('Unexpected PFC version')
        # Compare aggregate default/opt-in totals with SQL on actual posted Plaid rows.
        with sync.private_store() as db:
            start=month+'-01'
            def expected(condition):
                return round(db.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE pending=0 AND date>=? AND substr(date,1,7)=? AND primary_category!=\'INCOME\' AND {condition}',(start,month)).fetchone()[0],2)
            db.create_function('plaidmcp_category',2,plaidmcp_category)
            baseline=expected("plaidmcp_category(primary_category,detailed_category) NOT IN ('bank_transfer','peer_payment','income')")
            opted_in=expected("(plaidmcp_category(primary_category,detailed_category) NOT IN ('bank_transfer','peer_payment','income') OR (plaidmcp_category(primary_category,detailed_category)='peer_payment' AND amount>=0))")
            q=Queries(snapshot)
            for include,total in ((False,baseline),(True,opted_in)):
                actual=sum(r['spending'] for r in q.cash_flow('category',start,end,include_peer_transfers=include)['data']['totals'])
                if round(actual,2)!=total: raise CheckError('Transfer filtering mismatch')
            peer_count=db.execute("SELECT COUNT(*) FROM transactions WHERE detailed_category LIKE '%PEER_TRANSFER'").fetchone()[0]
            transfer_count=db.execute("SELECT COUNT(*) FROM transactions WHERE primary_category IN ('TRANSFER_IN','TRANSFER_OUT')").fetchone()[0]
        return {'tools_passed':len(samples),'pfc_categories':len(client.categories),
            'pfc_versions':sorted(client.category_versions),'privacy_scan':'passed',
            'transfer_rows_checked':transfer_count,'peer_rows_present':peer_count,'peer_shape_probe':'passed',
            'totals':{'net_worth':round(sum(r['net_worth'] or 0 for r in samples['balance_summary']['data']['totals']),2),
                'month_spending':baseline,
                'holdings_value':round(sum(r['value'] or 0 for r in samples['holdings']['data']['totals']),2)}}
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill();process.wait(timeout=5)


def main():
    logging.disable(logging.CRITICAL)
    run=configure()
    client=AuditClient()
    prepare(client)
    if not check_peer_shape(run,client): raise CheckError('Peer probe failed')
    print(json.dumps(asyncio.run(check_mcp(run,client)),sort_keys=True))

if __name__=='__main__':
    try: main()
    except PlaidError as error: raise SystemExit('Sandbox check failed: '+error.code) from None
    except ExceptionGroup as group:
        def reasons(error):
            if isinstance(error,BaseExceptionGroup): return [reason for child in error.exceptions for reason in reasons(child)]
            return [str(error) if isinstance(error,CheckError) else type(error).__name__]
        raise SystemExit('Sandbox check failed: '+', '.join(sorted(set(reasons(group))))) from None
    except CheckError as error: raise SystemExit('Sandbox check failed: '+str(error)) from None
    except Exception as error: raise SystemExit('Sandbox check failed ('+type(error).__name__+'); no raw response or credential details logged.') from None
