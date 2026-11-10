"""Private ingestion and atomic publication. Only verified normalized business names are retained privately; no descriptions."""
import os
import re
import sqlite3
import tempfile
import hashlib
import uuid
from collections import Counter
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, institution TEXT, as_of TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, item TEXT, name TEXT, mask TEXT, type TEXT, subtype TEXT, currency TEXT, current REAL, available REAL, credit_limit REAL);
CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY, item TEXT, date TEXT, primary_category TEXT, detailed_category TEXT, currency TEXT, amount REAL, pending INTEGER);
CREATE TABLE IF NOT EXISTS venmo_rows(date TEXT, cents INTEGER, direction TEXT CHECK(direction IN ('in','out')), category TEXT CHECK(category IN ('peer_sent','peer_received','purchase','bank_transfer','fee')));
CREATE TABLE IF NOT EXISTS recurring_merchants(item TEXT,merchant TEXT COLLATE NOCASE,currency TEXT,frequency TEXT,last_charged_month TEXT,active INTEGER,PRIMARY KEY(item,merchant));
CREATE TABLE IF NOT EXISTS holdings(item TEXT, account TEXT, ticker TEXT, name TEXT, type TEXT, currency TEXT, quantity REAL, value REAL, cost_basis REAL);
"""

def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SCHEMA)
    if 'active' not in {row[1] for row in db.execute('PRAGMA table_info(accounts)')}: db.execute('ALTER TABLE accounts ADD COLUMN active INTEGER DEFAULT 1')
    columns = {row[1] for row in db.execute('PRAGMA table_info(transactions)')}
    if 'account' not in columns: db.execute('ALTER TABLE transactions ADD COLUMN account TEXT')
    if 'merchant' not in columns: db.execute('ALTER TABLE transactions ADD COLUMN merchant TEXT')
    db.executescript("""
    CREATE TABLE IF NOT EXISTS account_identity(ref TEXT PRIMARY KEY, institution TEXT, type TEXT, subtype TEXT, name_key TEXT);
    CREATE TABLE IF NOT EXISTS account_alias(provider_ref TEXT, item TEXT, ref TEXT, PRIMARY KEY(provider_ref,item));
    CREATE TABLE IF NOT EXISTS source_identity(provider_ref TEXT PRIMARY KEY, ref TEXT);
    """)
    return db

def fingerprint(value):
    return hashlib.sha256(' '.join((value or '').casefold().split()).encode()).hexdigest()


def account_refs(db, item, accounts):
    institution = db.execute('SELECT institution FROM items WHERE id=?',(item,)).fetchone()[0]
    institution = ' '.join(institution.casefold().split())
    keys = [(institution,a['type'],a.get('subtype') or '',fingerprint(a['name'])) for a in accounts]
    counts = Counter(keys)
    refs = {}
    used = set()
    for a,key in zip(accounts,keys):
        existing = db.execute('SELECT ref FROM account_alias WHERE provider_ref=? AND item=?',(a['account_id'],item)).fetchone()
        candidates = db.execute('SELECT ref FROM account_identity WHERE institution=? AND type=? AND subtype=? AND name_key=?',key).fetchall()
        conflict = False
        if len(candidates)==1:
            disabled=set(filter(None,os.environ.get('PLAIDMCP_DISABLED_SOURCES','').split(',')))
            owners=db.execute('SELECT s.ref FROM accounts a JOIN account_alias m ON m.provider_ref=a.id AND m.item=a.item LEFT JOIN source_identity s ON s.provider_ref=a.item WHERE m.ref=? AND a.item!=? AND a.active=1',(candidates[0][0],item)).fetchall()
            conflict=any(owner[0] not in disabled for owner in owners)
        ref = existing[0] if existing else candidates[0][0] if len(candidates)==1 and counts[key]==1 and not conflict else 'acct_'+uuid.uuid4().hex
        if ref in used: ref = 'acct_'+uuid.uuid4().hex
        used.add(ref)
        db.execute('INSERT OR IGNORE INTO account_identity VALUES(?,?,?,?,?)',(ref,*key))
        db.execute('INSERT OR REPLACE INTO account_alias VALUES(?,?,?)',(a['account_id'],item,ref))
        refs[a['account_id']]=ref
    return refs


def ingest_accounts(db, item, accounts):
    account_refs(db,item,accounts)
    db.execute('UPDATE accounts SET current=NULL,available=NULL,credit_limit=NULL,active=0 WHERE item=?',(item,))
    for a in accounts:
        b = a['balances']
        # Provider display names are used only as a fingerprint for identity matching.
        # Public labels are generated from type/subtype, never from owner text.
        label = (a.get('subtype') or a['type']).replace('_',' ').title()
        db.execute('INSERT OR REPLACE INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?,1)', (a['account_id'], item,
            label, (a.get('mask') or '')[-4:] if re.fullmatch(r'[0-9]{4}',a.get('mask') or '') else '',
            a['type'],a.get('subtype'),b.get('iso_currency_code') or b.get('unofficial_currency_code') or 'UNKNOWN',
            b.get('current'),b.get('available'),b.get('limit')))


def ingest_transactions(db, item, page):
    for t in page.get('added', []) + page.get('modified', []):
        p = t.get('personal_finance_category') or {}
        primary = p.get('primary','UNKNOWN')
        detailed = p.get('detailed','UNKNOWN')
        institution = db.execute('SELECT institution FROM items WHERE id=?',(item,)).fetchone()
        # Business eligibility uses structured Plaid evidence; never descriptions/notes.
        from merchants import business_name
        is_venmo = institution and 'venmo' in institution[0].casefold()
        merchant=business_name(t,bool(is_venmo))
        if is_venmo and merchant is None and primary not in ('TRANSFER_IN','TRANSFER_OUT','BANK_FEES'):
            primary='TRANSFER_IN' if t['amount']<0 else 'TRANSFER_OUT'
            detailed=primary+'_PEER_TRANSFER'
        explicit_peer = detailed in ('TRANSFER_OUT_PEER_TO_PEER','TRANSFER_IN_PEER_TO_PEER',
            'TRANSFER_OUT_PEER_TRANSFER','TRANSFER_IN_PEER_TRANSFER')
        if primary in ('TRANSFER_IN','TRANSFER_OUT') and (explicit_peer or
                (is_venmo and detailed in ('TRANSFER_IN_OTHER_TRANSFER','TRANSFER_OUT_OTHER_TRANSFER','UNKNOWN'))):
            detailed = primary + '_PEER_TRANSFER'
        db.execute("INSERT OR REPLACE INTO transactions(id,item,date,primary_category,detailed_category,currency,amount,pending,account,merchant) VALUES(?,?,?,?,?,?,?,?,?,?)", (t['transaction_id'], item,
            t['date'], primary, detailed, t.get('iso_currency_code') or t.get('unofficial_currency_code') or 'UNKNOWN', t['amount'], bool(t.get('pending')),t.get('account_id'),merchant))
    for t in page.get('removed', []):
        db.execute("DELETE FROM transactions WHERE id=? AND item=?", (t['transaction_id'],item))

def ingest_holdings(db, item, data):
    securities = {s['security_id']:s for s in data['securities']}
    db.execute("DELETE FROM holdings WHERE item=?", (item,))
    for h in data['holdings']:
        s = securities.get(h['security_id'], {})
        db.execute("INSERT INTO holdings VALUES(?,?,?,?,?,?,?,?,?)", (item,h['account_id'],s.get('ticker_symbol'),s.get('name'),s.get('type','unknown'),h.get('iso_currency_code') or h.get('unofficial_currency_code') or 'UNKNOWN',h['quantity'],h['institution_value'],h.get('cost_basis')))

def publish(db, path, sources=None):
    """Publish only the common source interface; no provider fields in public schemas."""
    from sources import snapshot_sources, PUBLIC_SCHEMA
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd,temp = tempfile.mkstemp(dir=path.parent,prefix='.aggregate-'); os.close(fd)
    out = sqlite3.connect(temp)
    try:
        from merchants import MERCHANT_SCHEMA, publish_merchants
        out.executescript(PUBLIC_SCHEMA+MERCHANT_SCHEMA)
        for source in snapshot_sources(db) if sources is None else sources:
            snapshot = source.read_snapshot(db)
            for table,rows in snapshot.items():
                if table not in ('sources','accounts','daily','holdings'): raise ValueError('Unknown normalized table')
                for row in rows:
                    verb='INSERT INTO '
                    if table=='accounts':
                        prior=out.execute('SELECT availability,source_ref FROM accounts WHERE id=?',(row[0],)).fetchone()
                        if prior and prior[0]=='available' and row[-1]=='available' and prior[1]!=row[1]:
                            raise ValueError('Conflicting active sources claim the same account; choose one source')
                        if prior and prior[0]=='available' and row[-1]!='available': continue
                        verb='INSERT OR REPLACE INTO '
                    out.execute(verb+table+' VALUES('+','.join('?' for _ in row)+')',row)
        publish_merchants(db,out)
        out.commit(); out.close()
        os.chmod(temp,0o640)
        with open(temp,'rb') as f: os.fsync(f.fileno())
        db.commit()  # identities must survive a crash after the public rename
        os.replace(temp,path)
    finally:
        out.close()
        if os.path.exists(temp): os.unlink(temp)
