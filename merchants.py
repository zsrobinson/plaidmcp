"""Private business-name policy and SQL-only publication of multi-purchase windows."""
from datetime import date,datetime,timedelta,timezone
import os
import re
import unicodedata

WINDOWS=('month_to_date','last_month','last_3_months','last_6_months','last_12_months','all_history')
MERCHANT_SCHEMA='''CREATE TABLE merchants(window TEXT,merchant TEXT,currency TEXT,total REAL,purchase_count INTEGER,months REAL,recurring INTEGER,frequency TEXT,typical_charge REAL,last_charged_month TEXT,active INTEGER,recurring_method TEXT,merchant_source TEXT,scope TEXT,PRIMARY KEY(window,scope,merchant,currency));'''


def normalized_name(value):
    if not isinstance(value,str):return None
    value=' '.join(unicodedata.normalize('NFKC',value).split())
    if not value or len(value)>160 or any(unicodedata.category(c).startswith('C') for c in value):return None
    if '@' in value or re.search(r'\d{7}',value):return None
    return value


def business_name(transaction,is_venmo=False):
    """Normalize provider business names; only curated missing-name fallback. No Venmo."""
    if is_venmo:return None
    from descriptors import clean_business_label,descriptor_label,rail_kind,person_like
    raw_name=transaction.get('merchant_name')
    raw=transaction.get('original_description') or transaction.get('name') or ''
    if any(rail_kind(value) or person_like(value) for value in (transaction.get('original_description'),transaction.get('name'),raw_name) if value):return None
    known_name=descriptor_label({'name':raw_name}) if raw_name else None
    name=(known_name or clean_business_label(raw_name,transaction.get('location'))) if raw_name else descriptor_label(transaction)
    pfc=transaction.get('personal_finance_category') or {}
    primary=pfc.get('primary','UNKNOWN');detail=pfc.get('detailed','UNKNOWN')
    if not name or (primary in ('INCOME','TRANSFER_IN','TRANSFER_OUT') or (primary=='UNKNOWN' and raw_name)) or detail=='LOAN_PAYMENTS_CREDIT_CARD_PAYMENT':return None
    parties=transaction.get('counterparties') or []
    if any(p.get('type') not in ('merchant','financial_institution','payment_app','marketplace','payment_terminal','income_source') and not (p.get('type') in (None,'unknown') and known_name and descriptor_label({'name':p.get('name')})==known_name) for p in parties):return None
    matches=[p for p in parties if raw_name and p.get('type')=='merchant' and normalized_name(p.get('name')) and normalized_name(p['name']).casefold()==normalized_name(raw_name).casefold() and p.get('confidence_level') in ('HIGH','VERY_HIGH')]
    entity=transaction.get('merchant_entity_id')
    if raw_name and not (matches or entity or known_name):return None
    return name


def today_utc():
    return datetime.now(timezone.utc).date()


def window_bounds(today=None,earliest=None):
    today=today or today_utc()
    current=today.replace(day=1)
    yield 'month_to_date',current,today+timedelta(days=1),(today.day/__import__('calendar').monthrange(today.year,today.month)[1])
    for months in (1,3,6,12):
        n=current.year*12+current.month-1-months
        yield 'last_month' if months==1 else f'last_{months}_months',date(n//12,n%12+1,1),current,float(months)

    if earliest:
        first=date.fromisoformat(earliest);end=today+timedelta(days=1)
        if first<end:
            from queries import units
            yield 'all_history',first,end,units(first.isoformat(),end.isoformat())[0]


def ingest_recurring(db,item,data):
    # These are merchant-level hints only: no stream ids, descriptions, raw dates
    # or stream transaction-id lists are ever copied to the private DB.
    frequencies={'WEEKLY':'weekly','MONTHLY':'monthly','QUARTERLY':'quarterly','ANNUALLY':'yearly'}
    db.execute('DELETE FROM recurring_merchants WHERE item=?',(item,))
    for stream in data.get('outflow_streams',[]):
        from descriptors import descriptor_label,clean_business_label
        raw_name=stream.get('merchant_name')
        merchant=descriptor_label({'name':raw_name}) or clean_business_label(raw_name);frequency=frequencies.get(stream.get('frequency'))
        month=(stream.get('last_date') or '')[:7]
        if not merchant or not frequency or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])',month):continue
        rows=db.execute('SELECT currency FROM transactions WHERE item=? AND merchant=? COLLATE NOCASE AND pending=0 AND amount>0 GROUP BY currency HAVING COUNT(*)>=2',(item,merchant)).fetchall()
        if len(rows)!=1:continue
        # Conflicting streams under one merchant do not establish a single cadence.
        previous=db.execute('SELECT frequency FROM recurring_merchants WHERE item=? AND merchant=? COLLATE NOCASE',(item,merchant)).fetchone()
        if previous and previous[0]!=frequency:
            db.execute('UPDATE recurring_merchants SET frequency=NULL WHERE item=? AND merchant=? COLLATE NOCASE',(item,merchant));continue
        db.execute('INSERT OR REPLACE INTO recurring_merchants VALUES(?,?,?,?,?,?)',(item,merchant,rows[0][0],frequency,month,bool(stream.get('is_active'))))


def publish_merchants(db,out,today=None):
    # No transaction date, individual amount, provider id or unfurled merchant group
    # crosses into the readable snapshot. Aggregate before the DB layer returns rows.
    threshold=max(2,int(os.environ.get('PLAIDMCP_MIN_GROUP_COUNT','1')))
    disabled=list(filter(None,os.environ.get('PLAIDMCP_DISABLED_SOURCES','').split(',')))
    excluded=' AND COALESCE(s.ref,\'\') NOT IN ('+','.join('?' for _ in disabled)+')' if disabled else ''
    today=today or today_utc()
    earliest=db.execute('SELECT MIN(date) FROM transactions WHERE pending=0').fetchone()[0]
    from account_scope import separate_accounts, SCOPES
    separate=separate_accounts(db)
    db.execute('CREATE TEMP TABLE IF NOT EXISTS separate_scope(ref TEXT PRIMARY KEY)')
    db.execute('DELETE FROM separate_scope')
    db.executemany('INSERT INTO separate_scope VALUES(?)',[(ref,) for ref in separate])
    for scope in SCOPES:
        scope_filter="" if scope=='all' else " AND "+("" if scope=='separate' else "NOT ")+"EXISTS (SELECT 1 FROM separate_scope z WHERE z.ref=COALESCE(m.ref,u.ref))"
        # Provider streams have no safe account attribution; scoped cadence uses
        # only charges inside that scope rather than borrowing a provider hint.
        hint_filter='' if scope=='all' else ' AND 0'
        for window,start,end,months in window_bounds(today,earliest):
            sql=f"""WITH eligible AS (
              SELECT t.date,t.amount,t.currency,COALESCE(t.merchant,'Other') AS merchant,t.merchant_source FROM transactions t
              JOIN items i ON i.id=t.item LEFT JOIN account_alias m ON m.item=t.item AND m.provider_ref=t.account LEFT JOIN account_alias u ON u.item=t.item AND u.provider_ref='virtual:'||t.item LEFT JOIN source_identity s ON s.provider_ref=t.item
              WHERE t.pending=0 AND lower(i.institution) NOT LIKE '%venmo%' AND t.flow_kind='activity' AND t.primary_category!='INCOME' AND NOT EXISTS (SELECT 1 FROM internal_matches x WHERE x.item=t.item AND x.transaction_ref=t.id) AND t.date>=? AND t.date<? {excluded} {scope_filter}),
              charges AS (SELECT *,julianday(date)-julianday(LAG(date) OVER(PARTITION BY merchant COLLATE NOCASE,currency ORDER BY date)) AS gap,
                ROW_NUMBER() OVER(PARTITION BY merchant COLLATE NOCASE,currency ORDER BY amount) AS amount_rank,
                COUNT(*) OVER(PARTITION BY merchant COLLATE NOCASE,currency) AS n FROM eligible WHERE amount>0),
              cadence AS (SELECT merchant,currency,
                CASE WHEN COUNT(*)>=2 AND MIN(amount)>0 AND MAX(amount)<=MIN(amount)*1.25 THEN CASE
                  WHEN MIN(gap)>=5 AND MAX(gap)<=9 THEN 'weekly'
                  WHEN MIN(gap)>=25 AND MAX(gap)<=35 THEN 'monthly'
                  WHEN MIN(gap)>=80 AND MAX(gap)<=100 THEN 'quarterly'
                  WHEN MIN(gap)>=350 AND MAX(gap)<=380 THEN 'yearly' END END AS frequency,
                AVG(CASE WHEN amount_rank IN ((n+1)/2,(n+2)/2) THEN amount END) AS typical_charge,
                MAX(date) AS last_day FROM charges GROUP BY merchant COLLATE NOCASE,currency),
              hints AS (SELECT r.merchant,r.currency,MIN(r.frequency) AS frequency,MIN(r.active) AS active
                FROM recurring_merchants r LEFT JOIN source_identity s ON s.provider_ref=r.item
                WHERE r.frequency IS NOT NULL {excluded} {hint_filter} GROUP BY r.merchant COLLATE NOCASE,r.currency HAVING COUNT(DISTINCT r.frequency)=1),
              grouped AS (SELECT MIN(e.merchant) AS merchant,e.currency,SUM(e.amount) AS total,SUM(e.amount>0) AS purchase_count,
                COALESCE(h.frequency,c.frequency) AS frequency,c.typical_charge,c.last_day,
                CASE WHEN h.frequency IS NOT NULL THEN 'provider_pattern' ELSE 'cadence_heuristic' END AS method,
                COALESCE(h.active,1) AS provider_active,CASE WHEN MIN(e.merchant_source)=MAX(e.merchant_source) THEN MIN(e.merchant_source) ELSE 'mixed' END AS merchant_source
                FROM eligible e LEFT JOIN cadence c ON c.merchant=e.merchant COLLATE NOCASE AND c.currency=e.currency
                LEFT JOIN hints h ON h.merchant=e.merchant COLLATE NOCASE AND h.currency=e.currency GROUP BY e.merchant COLLATE NOCASE,e.currency),
              folded AS (SELECT CASE WHEN purchase_count<? THEN 'Other' ELSE merchant END AS merchant,currency,total,purchase_count,
                CASE WHEN purchase_count<? THEN NULL ELSE frequency END AS frequency,typical_charge,last_day,method,provider_active,merchant_source FROM grouped)
              SELECT merchant,currency,CASE WHEN SUM(purchase_count)>=? THEN ROUND(SUM(total),2) END,SUM(purchase_count),
                CASE WHEN merchant!='Other' THEN MAX(frequency) END,
                CASE WHEN merchant!='Other' AND MAX(frequency) IS NOT NULL THEN ROUND(MAX(typical_charge),2) END,
                CASE WHEN merchant!='Other' AND MAX(frequency) IS NOT NULL THEN substr(MAX(last_day),1,7) END,
                CASE WHEN merchant!='Other' AND MAX(frequency) IS NOT NULL THEN MIN(provider_active) AND julianday(?)-julianday(MAX(last_day))<=1.5*CASE MAX(frequency) WHEN 'weekly' THEN 7 WHEN 'monthly' THEN 30.4375 WHEN 'quarterly' THEN 91.3125 WHEN 'yearly' THEN 365.25 END END,
                CASE WHEN merchant!='Other' AND MAX(frequency) IS NOT NULL THEN MAX(method) END,
                CASE WHEN merchant!='Other' THEN MAX(merchant_source) ELSE 'unknown' END
              FROM folded GROUP BY merchant,currency"""
            for merchant,currency,total,count,frequency,typical,month,active,method,source in db.execute(sql,[start.isoformat(),end.isoformat(),*disabled,*disabled,threshold,threshold,threshold,today.isoformat()]):
                out.execute('INSERT INTO merchants VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(window,merchant,currency,total,count,months,bool(frequency),frequency,typical,month,active,method,source,scope))
