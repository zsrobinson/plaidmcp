"""Two adapters at one source seam. Provider details stop at normalized snapshots."""
from dataclasses import dataclass
import json
import os
import sqlite3
import uuid
from typing import Protocol

PUBLIC_SCHEMA = '''
CREATE TABLE sources(id TEXT PRIMARY KEY,institution TEXT,kind TEXT,as_of TEXT,status TEXT,data_kinds TEXT);
CREATE TABLE accounts(id TEXT PRIMARY KEY,source_ref TEXT,display_name TEXT,institution TEXT,type TEXT,subtype TEXT,currency TEXT,current REAL,available REAL,credit_limit REAL,last_four TEXT,data_kinds TEXT,availability TEXT);
CREATE TABLE daily(day TEXT,account_ref TEXT,source_ref TEXT,category TEXT,category_detailed TEXT,currency TEXT,income REAL,spending REAL,transfer_in REAL,transfer_out REAL,peer_in REAL,peer_out REAL,count INTEGER,payroll REAL DEFAULT 0,interest REAL DEFAULT 0,refunds REAL DEFAULT 0,other_income REAL DEFAULT 0,internal_in REAL DEFAULT 0,internal_out REAL DEFAULT 0);
CREATE TABLE holdings(account_ref TEXT,source_ref TEXT,ticker TEXT,name TEXT,asset_class TEXT,security_type TEXT,currency TEXT,quantity REAL,value REAL,cost_basis REAL,count INTEGER);
'''

from taxonomy import category, detailed


def source_ref(db,key):
    row=db.execute('SELECT ref FROM source_identity WHERE provider_ref=?',(key,)).fetchone()
    if row: return row[0]
    ref='src_'+uuid.uuid4().hex
    db.execute('INSERT INTO source_identity VALUES(?,?)',(key,ref))
    return ref


def virtual_account(db,key,institution,label):
    from store import account_refs
    # Same identity machinery; the synthetic provider key stays private.
    return account_refs(db,key,[{'account_id':'virtual:'+key,'type':'other','subtype':label,'name':label+' '+source_ref(db,key)}])['virtual:'+key]


def status(error,as_of):
    if not error and as_of: return 'healthy'
    if error in ('ITEM_LOGIN_REQUIRED','INVALID_ACCESS_TOKEN','ITEM_NOT_FOUND'): return 'needs re-link'
    if error in (None,'INITIAL_SYNC_REQUIRED','PRODUCT_NOT_READY'): return 'waiting for data'
    if error=='PLAIDMCP_VENMO_SOURCE_OVERLAP': return 'source overlap needs review'
    return 'temporarily unavailable'


class Source(Protocol):
    def refresh(self,db,public): ...
    def read_snapshot(self,db) -> dict[str,list[tuple]]: ...


@dataclass
class PlaidSource:
    key: str
    client: object = None

    def refresh(self,db,public):
        from sync import sync_item
        row=db.execute('SELECT * FROM credentials WHERE item=?',(self.key,)).fetchone()
        if row: sync_item(db,self.client,row)

    def read_snapshot(self,db):
        from store import account_refs
        institution,as_of,error=db.execute('SELECT institution,as_of,error FROM items WHERE id=?',(self.key,)).fetchone()
        ref=source_ref(db,self.key)
        # Upgrade old private stores once, before old provider labels are discarded.
        old=db.execute('SELECT id,name,type,subtype FROM accounts WHERE item=?',(self.key,)).fetchall()
        missing=[{'account_id':a,'name':n,'type':t,'subtype':s} for a,n,t,s in old if not db.execute('SELECT 1 FROM account_alias WHERE provider_ref=? AND item=?',(a,self.key)).fetchone()]
        if missing: account_refs(db,self.key,missing)
        for a,n,t,s in old: db.execute('UPDATE accounts SET name=? WHERE id=?',((s or t).replace('_',' ').title(),a))
        db.create_function('plaidmcp_category',2,category)
        db.create_function('plaidmcp_detail',2,detailed)
        kinds=set()
        try: row=db.execute('SELECT product FROM credentials WHERE item=?',(self.key,)).fetchone()
        except sqlite3.OperationalError: row=None
        if row:
            kinds.add('balances')
            if 'transactions' in row[0].split(','): kinds.add('spending')
            if 'investments' in row[0].split(','): kinds.add('holdings')
        if old: kinds.add('balances')
        if db.execute('SELECT 1 FROM transactions WHERE item=?',(self.key,)).fetchone(): kinds.add('spending')
        if db.execute('SELECT 1 FROM holdings WHERE item=?',(self.key,)).fetchone(): kinds.add('holdings')
        accounts=[];seen=set()
        for a,t,s,c,cur,avail,limit,mask,aid,active in db.execute('SELECT a.id,a.type,a.subtype,a.currency,a.current,a.available,a.credit_limit,a.mask,m.ref,a.active FROM accounts a JOIN account_alias m ON m.provider_ref=a.id AND m.item=a.item WHERE a.item=? ORDER BY a.active DESC',(self.key,)):
            if aid in seen: continue
            seen.add(aid)
            data=['balances'] if active else []
            if 'spending' in kinds and t!='investment': data.append('spending')
            if 'holdings' in kinds and t=='investment': data.append('holdings')
            label=(s or t).replace('_',' ').title()+' '+aid[-6:]+(' (historical)' if not active else '')
            accounts.append((aid,ref,label,institution,t,s,c,cur,avail,limit,mask if os.environ.get('PLAIDMCP_INCLUDE_LAST_FOUR','false').lower()=='true' else '',json.dumps(data),'available' if active else 'historical'))
        unknown=virtual_account(db,self.key,institution,'unassigned activity')
        # Legacy rows without account attribution remain discoverable; never guess an account.
        unmatched=db.execute('SELECT 1 FROM transactions t LEFT JOIN account_alias m ON m.provider_ref=t.account AND m.item=t.item WHERE t.item=? AND m.ref IS NULL LIMIT 1',(self.key,)).fetchone()
        if unmatched: accounts.append((unknown,ref,'Unassigned activity '+unknown[-6:],institution,'other','unassigned activity','UNKNOWN',None,None,None,'',json.dumps(['spending']),'unassigned'))
        sql="""WITH normalized AS (
            SELECT date AS day,COALESCE(m.ref,?) AS account_ref,
              CASE WHEN x.transaction_ref IS NOT NULL THEN 'internal_transfer' ELSE plaidmcp_category(primary_category,detailed_category) END AS category,
              CASE WHEN x.transaction_ref IS NOT NULL THEN 'internal_transfer' ELSE plaidmcp_detail(primary_category,detailed_category) END AS category_detailed,
              t.currency,amount,primary_category,detailed_category FROM transactions t LEFT JOIN account_alias m
              ON m.provider_ref=t.account AND m.item=t.item LEFT JOIN internal_matches x ON x.item=t.item AND x.transaction_ref=t.id WHERE t.item=? AND pending=0)
            SELECT day,account_ref,?,category,category_detailed,currency,
              SUM(CASE WHEN category='income' THEN -amount ELSE 0 END),
              SUM(CASE WHEN category NOT IN ('income','bank_transfer','peer_payment','internal_transfer') THEN amount ELSE 0 END),
              SUM(CASE WHEN category='bank_transfer' AND amount<0 THEN -amount ELSE 0 END),
              SUM(CASE WHEN category='bank_transfer' AND amount>=0 THEN amount ELSE 0 END),
              SUM(CASE WHEN category='peer_payment' AND amount<0 THEN -amount ELSE 0 END),
              SUM(CASE WHEN category='peer_payment' AND amount>=0 THEN amount ELSE 0 END),COUNT(*),
              SUM(CASE WHEN category='income' AND detailed_category='INCOME_WAGES' THEN -amount ELSE 0 END),
              SUM(CASE WHEN category='income' AND detailed_category='INCOME_INTEREST_EARNED' THEN -amount ELSE 0 END),
              SUM(CASE WHEN category NOT IN ('income','bank_transfer','peer_payment','internal_transfer') AND amount<0 THEN -amount ELSE 0 END),
              SUM(CASE WHEN category='income' AND detailed_category NOT IN ('INCOME_WAGES','INCOME_INTEREST_EARNED') THEN -amount ELSE 0 END),
              SUM(CASE WHEN category='internal_transfer' AND amount<0 THEN -amount ELSE 0 END),
              SUM(CASE WHEN category='internal_transfer' AND amount>0 THEN amount ELSE 0 END)
            FROM normalized GROUP BY day,account_ref,category,category_detailed,currency"""
        activity=db.execute(sql,(unknown,self.key,ref)).fetchall()
        holdings=db.execute('''SELECT m.ref,?,ticker,h.name,CASE WHEN h.type IN ('etf','mutual fund') THEN 'fund' WHEN h.type='equity' THEN 'stock' WHEN h.type IN ('bond','fixed income') THEN 'bond' WHEN h.type IN ('cash','cash equivalent') THEN 'cash' ELSE 'other' END,CASE WHEN h.type='etf' THEN 'etf' WHEN h.type='mutual fund' THEN 'mutual_fund' WHEN h.type='equity' THEN 'stock' WHEN h.type IN ('bond','fixed income') THEN 'bond' WHEN h.type IN ('cash','cash equivalent') THEN 'cash' ELSE 'other' END,h.currency,SUM(quantity),SUM(value),CASE WHEN COUNT(cost_basis)=COUNT(*) THEN SUM(cost_basis) END,COUNT(*) FROM holdings h JOIN account_alias m ON m.provider_ref=h.account AND m.item=h.item WHERE h.item=? GROUP BY m.ref,ticker,h.name,h.type,h.currency''',(ref,self.key)).fetchall()
        return {'sources':[(ref,institution,'connection',as_of,status(error,as_of),json.dumps(sorted(kinds)))], 'accounts':accounts,'daily':activity,'holdings':holdings}


@dataclass
class VenmoSource:
    key: str = 'venmo-csv'

    def refresh(self,db,public):
        from venmo_import import import_configured
        import_configured(db,public)

    def read_snapshot(self,db):
        row=db.execute('SELECT institution,as_of,error FROM items WHERE id=?',(self.key,)).fetchone()
        if not row: return {}
        institution,as_of,error=row
        ref=source_ref(db,self.key); account=virtual_account(db,self.key,institution,'statement wallet')
        sql="""SELECT date,?,?,CASE category WHEN 'peer_sent' THEN 'peer_payment' WHEN 'peer_received' THEN 'peer_payment' WHEN 'bank_transfer' THEN 'bank_transfer' WHEN 'fee' THEN 'fees' ELSE 'shopping' END,
          CASE category WHEN 'fee' THEN 'general_fees' WHEN 'purchase' THEN 'general_shopping' ELSE category END,'USD',0,
          SUM(CASE WHEN category IN ('purchase','fee') THEN CASE direction WHEN 'in' THEN -cents/100.0 ELSE cents/100.0 END ELSE 0 END),
          SUM(CASE WHEN category='bank_transfer' AND direction='in' THEN cents/100.0 ELSE 0 END),
          SUM(CASE WHEN category='bank_transfer' AND direction='out' THEN cents/100.0 ELSE 0 END),
          SUM(CASE WHEN category='peer_received' THEN cents/100.0 ELSE 0 END),
          SUM(CASE WHEN category='peer_sent' THEN cents/100.0 ELSE 0 END),COUNT(*)
          FROM venmo_rows GROUP BY date,category,direction"""
        return {'sources':[(ref,institution,'file_import',as_of,status(error,as_of),json.dumps(['spending']))],
          'accounts':[(account,ref,'Statement wallet '+account[-6:],institution,'other','wallet','USD',None,None,None,'',json.dumps(['spending']),'available')],
          'daily':db.execute(sql,(account,ref)).fetchall(),'holdings':[]}


def enabled(db,key):
    disabled=set(filter(None,os.environ.get('PLAIDMCP_DISABLED_SOURCES','').split(',')))
    row=db.execute('SELECT ref FROM source_identity WHERE provider_ref=?',(key,)).fetchone()
    return not row or row[0] not in disabled


def snapshot_sources(db):
    return [VenmoSource() if key=='venmo-csv' else PlaidSource(key) for key, in db.execute('SELECT id FROM items') if enabled(db,key)]


def refresh_sources(db,public,client_factory):
    # Both adapters use the same interface. CSV-only operation never constructs a client.
    if enabled(db,'venmo-csv'): VenmoSource().refresh(db,public)
    credentials=[row for row in db.execute('SELECT item FROM credentials') if enabled(db,row[0])]
    client=client_factory() if credentials else None
    for key, in credentials:
        if enabled(db,key): PlaidSource(key,client).refresh(db,public)
