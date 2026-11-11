"""Offline account backfill. Raw cells exist only while parsing; errors are fixed."""
import csv
import hashlib
import json
import os
from pathlib import Path
import stat
from collections import Counter

from descriptors import descriptor_label, rail_kind
from store import publish
from venmo_import import ImportError, norm, parse_amount, parse_date, FOOTERS, file_identity

ALIASES={
    'date':('transactiondate','date','posteddate','postingdate'),
    'description':('description','transactiondescription','memo','name'),
    'amount':('amount','transactionamount'),
    'debit':('debit','debits','withdrawal','withdrawals'),
    'credit':('credit','credits','deposit','deposits'),
    'type':('type','transactiontype','debitcredit'),
}


def indices(row, mapping):
    names=[norm(v) for v in row];result={}
    for field,aliases in ALIASES.items():
        for alias in ((norm(mapping[field]),) if field in mapping else aliases):
            found=[i for i,v in enumerate(names) if v==alias]
            if len(found)>1: raise ImportError('Ambiguous statement columns')
            if found:
                result[field]=found[0];break
    return result if 'date' in result and ('amount' in result or {'debit','credit'}<=result.keys()) else None


def parse_statement(stream, mapping=None):
    mapping=mapping or {}
    if not isinstance(mapping,dict) or set(mapping)-set(ALIASES) or any(not isinstance(v,str) for v in mapping.values()):
        raise ImportError('Invalid statement column mapping')
    header=None;rows=[]
    try:
        for number,row in enumerate(csv.reader(stream)):
            if number>100000: raise ImportError('Statement exceeds row limit')
            if not any(v.strip() for v in row): continue
            if header is None:
                header=indices(row,mapping)
                if header is None and number>=100: raise ImportError('Statement header not found')
                continue
            if next((norm(v) for v in row if v.strip()),'') in FOOTERS: continue
            def cell(key):
                i=header.get(key)
                return row[i].strip() if i is not None and i<len(row) else ''
            when=parse_date(cell('date'))
            if 'debit' in header and 'credit' in header:
                debit=parse_amount(cell('debit'))[0] if cell('debit') else 0
                credit=parse_amount(cell('credit'))[0] if cell('credit') else 0
                if debit and credit: raise ImportError('Ambiguous statement amount')
                cents=debit-credit
            else:
                raw=cell('amount');amount,direction=parse_amount(raw)
                kind=norm(cell('type'))
                if kind and kind not in ('debit','credit','purchase','payment','withdrawal','deposit','refund'):
                    raise ImportError('Unknown statement amount type')
                # Signed amounts use conventional bank CSV signs: negative is outflow.
                explicit=1 if kind in ('debit','purchase','withdrawal') else -1 if kind in ('credit','payment','deposit','refund') else None
                sign=1 if raw.strip().startswith(('-','(')) else -1
                if explicit is not None and direction and explicit!=sign: raise ImportError('Conflicting statement direction')
                cents=amount*(explicit if explicit is not None else sign)
            raw=cell('description')
            rail=rail_kind(raw)
            merchant=descriptor_label({'name':raw}) if not rail else None
            primary='UNKNOWN';detail='UNKNOWN';flow='activity'
            if rail in ('bank_transfer','peer_payment'):
                primary='TRANSFER_OUT' if cents>=0 else 'TRANSFER_IN'
                detail=primary+('_PEER_TRANSFER' if rail=='peer_payment' else '_ACCOUNT_TRANSFER');flow=rail
            elif rail: flow='excluded'
            rows.append((when,cents,primary,detail,merchant,flow))
    except (ValueError,csv.Error,UnicodeError):
        raise ImportError('Invalid statement CSV') from None
    if header is None: raise ImportError('Statement header not found')
    return rows


def target(db,ref):
    rows=db.execute('''SELECT a.item,a.id,a.currency FROM accounts a JOIN account_alias m
        ON m.item=a.item AND m.provider_ref=a.id WHERE m.ref=? AND a.active=1''',(ref,)).fetchall()
    if len(rows)!=1: raise ImportError('Statement account must identify one active linked account')
    cutoff=db.execute('''SELECT MIN(t.date) FROM transactions t JOIN account_alias m
        ON m.item=t.item AND m.provider_ref=t.account WHERE m.ref=? AND t.pending=0 AND t.origin='plaid' ''',(ref,)).fetchone()[0]
    if cutoff is None: raise ImportError('Statement account requires posted linked history')
    return (*rows[0],cutoff)


def import_folder(db,public,folder,ref,mapping=None):
    item,account,currency,cutoff=target(db,ref)
    folder=Path(folder)
    if not folder.exists(): return 0
    if folder.is_symlink() or not folder.is_dir(): raise ImportError('Invalid statement inbox')
    count=0
    try:
        for path in sorted(folder.iterdir()):
            if path.suffix.casefold()!='.csv': continue
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'r',encoding='utf-8-sig',newline='') as stream:
                before=os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size>25*1024*1024: raise ImportError('Invalid statement file')
                rows=parse_statement(stream,mapping)
                if file_identity(before)!=file_identity(os.fstat(stream.fileno())): raise ImportError('Statement changed during import')
            occurrences=Counter()
            with db:
                for row in rows:
                    if row[0]>=cutoff: continue
                    occurrences[row]+=1
                    # Sanitized content only; no raw ID, filename or raw-text hash.
                    key='statement_'+hashlib.sha256(json.dumps((ref,row,occurrences[row])).encode()).hexdigest()
                    when,cents,primary,detail,merchant,flow=row
                    db.execute('''INSERT OR IGNORE INTO transactions
                        (id,item,date,primary_category,detailed_category,currency,amount,pending,account,merchant,merchant_source,flow_kind,origin)
                        VALUES(?,?,?,?,?,?,?,0,?,?,?,?, 'statement')''',
                        (key,item,when,primary,detail,currency,cents/100,account,merchant,'descriptor_fallback' if merchant else 'unknown',flow))
            publish(db,public)
            if file_identity(before)!=file_identity(path.lstat()): raise ImportError('Statement changed before cleanup')
            path.unlink();count+=1
    except OSError:
        raise ImportError('Statement inbox operation failed') from None
    return count


def import_configured(db,public):
    try:
        config=json.loads(os.environ.get('PLAIDMCP_STATEMENT_ACCOUNTS','{}'))
        mapping=json.loads(os.environ.get('PLAIDMCP_STATEMENT_COLUMN_MAP','{}'))
        if not isinstance(config,dict): raise ValueError()
        for folder,ref in config.items():
            if not isinstance(folder,str) or not folder or folder in ('.','..') or '/' in folder or '\\' in folder or not isinstance(ref,str): raise ValueError()
            target(db,ref)
    except (ValueError,TypeError): raise ImportError('Invalid statement import configuration') from None
    root=Path(os.environ.get('PLAIDMCP_STATEMENT_IMPORT_DIR',str(Path(os.environ.get('PLAIDMCP_PRIVATE','private'))/'import'/'statements')))
    if root.is_symlink(): raise ImportError('Invalid statement inbox')
    for folder,ref in config.items(): import_folder(db,public,root/folder,ref,mapping)
