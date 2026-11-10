"""Production-only sync, in-memory source-string audit and gated atomic publication."""
import json,logging,os,re,sqlite3,tempfile
from pathlib import Path

PHASE='starting'
MATCH={}
BANNED={'transaction_id','account_id','item_id','merchant_name','description','original_description','counterparties','note','notes','payment_note','payment_notes','username','user_name','routing_number','account_number','owners','owner_name','addresses','emails','phones','access_token','public_token','raw_descriptor','bank_description'}


def check(value,strings,**context):
    global MATCH
    for text in strings:
        raw=text.encode()
        if (re.search(rb'(?<![A-Za-z0-9])'+re.escape(raw)+rb'(?![A-Za-z0-9])',value,re.I) if len(text)<=3 else raw.lower() in value.lower()):
            MATCH={**context,'match_length':len(text)}
            raise ValueError('Privacy check failed')


def check_fields(value):
    if isinstance(value,dict):
        if set(value)&BANNED:raise ValueError('Prohibited response fields')
        for child in value.values():check_fields(child)
    elif isinstance(value,list):
        for child in value:check_fields(child)


def audit_snapshot(public,private,private_strings,strict_strings,approved_business):
    """Never emit source strings; validate fields, SQL folding, bytes and every tool."""
    global PHASE
    from merchants import MERCHANT_SCHEMA,publish_merchants
    from queries import Queries
    PHASE='public_schema'
    with sqlite3.connect(public.as_uri()+'?mode=ro',uri=True) as db:
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names!={'sources','accounts','daily','holdings','merchants'}:raise ValueError('Unexpected public schema')
        metadata={r[0] for r in db.execute('SELECT institution FROM sources')}|{r[0] for r in db.execute('SELECT name FROM holdings') if r[0]}
        for type_name,subtype in db.execute('SELECT type,subtype FROM accounts'):
            for value in (type_name,subtype):
                if value:metadata.update((value,value.replace('_',' ').title()))
        expected=sqlite3.connect(':memory:')
        try:
            expected.executescript(MERCHANT_SCHEMA);publish_merchants(private,expected)
            if db.execute('SELECT * FROM merchants ORDER BY window,merchant,currency').fetchall()!=expected.execute('SELECT * FROM merchants ORDER BY window,merchant,currency').fetchall():raise ValueError('Merchant aggregates differ from private SQL')
        finally:expected.close()
        PHASE='internal_transfer_audit'
        invalid=private.execute('''SELECT COUNT(*) FROM internal_matches x
            JOIN transactions a ON a.item=x.item AND a.id=x.transaction_ref
            JOIN transactions b ON b.item=x.paired_item AND b.id=x.paired_ref
            JOIN account_alias aa ON aa.item=a.item AND aa.provider_ref=a.account
            JOIN account_alias bb ON bb.item=b.item AND bb.provider_ref=b.account
            WHERE a.pending!=0 OR b.pending!=0 OR a.flow_kind!='bank_transfer' OR b.flow_kind!='bank_transfer'
            OR aa.ref=bb.ref OR a.currency!=b.currency OR CAST(ROUND(a.amount*100) AS INTEGER)!=-CAST(ROUND(b.amount*100) AS INTEGER)
            OR ABS(julianday(a.date)-julianday(b.date))>3''').fetchone()[0]
        if invalid:raise ValueError('Invalid internal transfer match')
        if db.execute("SELECT COUNT(*) FROM daily WHERE category='internal_transfer' AND (income!=0 OR spending!=0 OR transfer_in!=0 OR transfer_out!=0 OR peer_in!=0 OR peer_out!=0)").fetchone()[0]:raise ValueError('Internal transfer entered income or spending')
        for currency,incoming,outgoing in private.execute('''SELECT t.currency,
            ROUND(SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END),2),ROUND(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),2)
            FROM transactions t JOIN internal_matches x ON x.item=t.item AND x.transaction_ref=t.id GROUP BY t.currency'''):
            actual=db.execute('SELECT ROUND(COALESCE(SUM(internal_in),0),2),ROUND(COALESCE(SUM(internal_out),0),2) FROM daily WHERE currency=?',(currency,)).fetchone()
            if actual!=(incoming,outgoing):raise ValueError('Internal transfer aggregate mismatch')
        minimum=max(2,int(os.environ.get('PLAIDMCP_MIN_GROUP_COUNT','1')))
        from descriptors import person_like,rail_kind
        for merchant,count,total in db.execute('SELECT merchant,purchase_count,total FROM merchants'):
            if merchant!='Other' and (count<minimum or merchant not in approved_business):raise ValueError('Unapproved merchant group')
            if count<minimum and total is not None:raise ValueError('Single-purchase amount exposed')
            if merchant!='Other' and (person_like(merchant) or rail_kind(merchant) or re.search(r'\d',merchant)):raise ValueError('Unsafe merchant label')
            if merchant!='Other':check(merchant.encode(),strict_strings,table='merchants',column='merchant')
        # Explicitly permitted metadata can overlap a private provider/merchant label.
        # Scrub only known metadata/business values before scanning SQLite bytes.
        published_business={r[0] for r in db.execute("SELECT DISTINCT merchant FROM merchants WHERE merchant!='Other'")}
        allowed=metadata|published_business
        folded={s.casefold() for s in allowed}
        forbidden={s for s in private_strings if s.casefold() not in folded}
        for table in names:
            cursor=db.execute('SELECT * FROM '+table);columns=[c[0] for c in cursor.description]
            if set(columns)&BANNED:raise ValueError('Prohibited public fields')
            for row in cursor:
                for column,value in zip(columns,row):
                    PHASE='public_fields'
                    if not isinstance(value,str):continue
                    if table=='merchants' and column=='merchant':
                        if value!='Other' and value not in approved_business:raise ValueError('Unapproved merchant label')
                        # Exact approved labels passed the hard person/note check above.
                        # A raw alias may overlap this label, but is forbidden elsewhere.
                        continue
                    elif value not in metadata:check(value.encode(),approved_business,table=table,column=column)
                    if value not in metadata:check(value.encode(),forbidden,table=table,column=column)
        PHASE='public_bytes';content=public.read_bytes()
        for value in sorted(allowed,key=len,reverse=True):content=content.replace(value.encode(),b' '*len(value.encode()))
        check(content,forbidden)
        sources=db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        healthy=db.execute("SELECT COUNT(*) FROM sources WHERE status='healthy'").fetchone()[0]
        accounts=db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
    PHASE='tool_responses'
    from server import TOOLS
    q=Queries(public)
    for name in TOOLS:
        windows=__import__('merchants').WINDOWS if name=='merchant_totals' else (None,)
        for window in windows:
            response=getattr(q,name)(window=window) if window else getattr(q,name)()
            check_fields(response)
            encoded=json.dumps(response,ensure_ascii=False).encode()
            for value in sorted(allowed,key=len,reverse=True):encoded=encoded.replace(value.encode(),b' '*len(value.encode()))
            check(encoded,forbidden)
    return sources,healthy,accounts


def main():
    global PHASE
    logging.disable(logging.CRITICAL)
    import sync
    from plaid_client import Client
    from merchants import business_name
    captured=set();strict=set();approved=set();recurring_support=[]
    private_root=Path(os.environ.get('PLAIDMCP_PRIVATE','/var/lib/plaidmcp-private'))
    public=Path(os.environ.get('PLAIDMCP_PUBLIC','/var/lib/plaidmcp/aggregate.sqlite')).resolve()
    with sqlite3.connect((private_root/'private.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        institutions=dict(db.execute('SELECT c.token,i.institution FROM credentials c JOIN items i ON i.id=c.item'))
    class Audited(Client):
        def __init__(self):
            super().__init__();captured.update(self.credentials.values())
        def call(self,endpoint,**payload):
            if self.env!='production' or self.base!='https://production.plaid.com':raise ValueError('Production configuration required')
            if endpoint not in ('/item/get','/institutions/get_by_id','/accounts/get','/transactions/sync','/transactions/recurring/get','/investments/holdings/get'):raise ValueError('Unexpected sync endpoint')
            if payload.get('access_token'):captured.add(payload['access_token'])
            try:result=super().call(endpoint,**payload)
            except Exception:
                if endpoint=='/transactions/recurring/get':recurring_support.append(False)
                raise
            if endpoint=='/transactions/recurring/get':recurring_support.append(True)
            if result.get('item',{}).get('item_id'):captured.add(result['item']['item_id'])
            for t in result.get('added',[])+result.get('modified',[]):
                is_venmo='venmo' in institutions.get(payload.get('access_token'),'').casefold()
                business=business_name(t,is_venmo)
                if business:approved.add(business)
                for key in ('name','merchant_name','original_description','description','note','notes','payment_note','payment_notes','username','user_name','transaction_id','account_id'):
                    if isinstance(t.get(key),str) and t[key]:
                        captured.add(t[key])
                        if key in ('note','notes','payment_note','payment_notes','username','user_name'):strict.add(t[key])
                for other in t.get('counterparties',[]) or []:
                    for key in ('name','username'):
                        if other.get(key):
                            captured.add(other[key])
                            if key=='username' or other.get('type') not in ('merchant','financial_institution','payment_app','marketplace','payment_terminal','income_source'):strict.add(other[key])
            for stream in result.get('outflow_streams',[])+result.get('inflow_streams',[]):
                for key in ('description','stream_id','account_id'):
                    if stream.get(key):captured.add(stream[key])
                captured.update(stream.get('transaction_ids') or [])
            for a in result.get('accounts',[]):
                for key in ('account_id','name','official_name'):
                    if a.get(key):captured.add(a[key])
            return result
    original_client=sync.Client;sync.Client=Audited
    fd,temporary=tempfile.mkstemp(dir=public.parent,prefix='.audit-');os.close(fd)
    candidate=Path(temporary);previous=os.environ.get('PLAIDMCP_PUBLIC')
    try:
        os.environ['PLAIDMCP_PUBLIC']=str(candidate)
        PHASE='sync';sync.main()
        PHASE='full_history_audit';audit_client=Audited()
        with sqlite3.connect((private_root/'private.sqlite').as_uri()+'?mode=ro',uri=True) as private:
            tokens=private.execute('SELECT token,product FROM credentials').fetchall()
            for token,products in tokens:
                if 'transactions' not in products.split(','):continue
                cursor=None
                for _ in range(1000):
                    payload={'access_token':token,'count':500,'options':{'personal_finance_category_version':'v2','include_original_description':True}}
                    if cursor:payload['cursor']=cursor
                    page=audit_client.call('/transactions/sync',**payload)
                    if not page['has_more']:break
                    cursor=page['next_cursor']
                else:raise ValueError('Audit pagination limit')
            sources,healthy,accounts=audit_snapshot(candidate,private,captured,strict,approved)
            failures=private.execute('SELECT COUNT(*) FROM items WHERE error IS NOT NULL OR as_of IS NULL').fetchone()[0]
            history={institution:month for institution,month in private.execute('SELECT i.institution,MIN(substr(t.date,1,7)) FROM items i LEFT JOIN transactions t ON t.item=i.id AND t.pending=0 GROUP BY i.id')}
        if failures or healthy!=sources:raise ValueError('Source health gate failed; prior snapshot retained')
        PHASE='publication';os.replace(candidate,public)
        print(json.dumps({'privacy_passed':True,'linked_items':len(tokens),'sources':sources,'healthy_sources':healthy,'accounts':accounts,'ready_for_timer':bool(tokens and healthy==sources and not failures and accounts),'history_earliest_month':history,'recurring_provider_available':any(recurring_support)}))
    finally:
        sync.Client=original_client;candidate.unlink(missing_ok=True)
        if previous is None:os.environ.pop('PLAIDMCP_PUBLIC',None)
        else:os.environ['PLAIDMCP_PUBLIC']=previous


def run():
    try:main()
    except Exception as error:
        print(json.dumps({'privacy_passed':False,'phase':PHASE,'error_type':type(error).__name__,**MATCH}))
        return 1
    return 0

if __name__=='__main__':raise SystemExit(run())
