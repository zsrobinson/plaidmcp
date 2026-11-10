"""Provider-independent questions over an aggregate-only snapshot."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import calendar
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
from typing import Literal

ActivityGroup = Literal['category','category_detailed','month','week','account','institution']
HoldingGroup = Literal['security','account','asset_class','security_type']
BalanceGroup = Literal['type','account','institution']


def today_utc():
    return datetime.now(timezone.utc).date()


def month_bounds(month):
    start=date.fromisoformat(month+'-01')
    return start.isoformat(),date(start.year+(start.month==12),start.month%12+1,1).isoformat()


def default_period(start,end):
    if bool(start)!=bool(end): raise ValueError('Supply both start and end, YYYY-MM-DD; end is exclusive')
    if start is None: start=today_utc().replace(day=1).isoformat(); end=(today_utc()+timedelta(days=1)).isoformat()
    a,b=date.fromisoformat(start),date.fromisoformat(end)
    if a>=b or (b-a).days>3660: raise ValueError('Use an increasing date range of at most ten years')
    return a.isoformat(),b.isoformat()


def units(start,end):
    a,b=date.fromisoformat(start),date.fromisoformat(end)
    weeks=(b-a).days/7
    months=0
    cursor=a
    while cursor<b:
        next_month=date(cursor.year+(cursor.month==12),cursor.month%12+1,1)
        stop=min(b,next_month)
        months+=(stop-cursor).days/calendar.monthrange(cursor.year,cursor.month)[1]
        cursor=stop
    return months,weeks


class Queries:
    def __init__(self,path=None):
        self.path=Path(path or os.environ.get('PLAIDMCP_PUBLIC','aggregate.sqlite')).resolve()
        self.include_last_four=os.environ.get('PLAIDMCP_INCLUDE_LAST_FOUR','false').lower()=='true'
        self.minimum=int(os.environ.get('PLAIDMCP_MIN_GROUP_COUNT','1'))
        self.stale_hours=float(os.environ.get('PLAIDMCP_STALE_HOURS','24'))
        if not 1<=self.minimum<=100000 or not math.isfinite(self.stale_hours) or self.stale_hours<=0:
            raise ValueError('Invalid aggregate privacy/freshness configuration')

    def _read(self,fn):
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
            db.row_factory=sqlite3.Row; db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
            sources=[]
            for raw in db.execute('SELECT * FROM sources ORDER BY institution,id'):
                s=dict(raw);s['data_kinds']=json.loads(s['data_kinds'])
                try:
                    age=(datetime.now(timezone.utc)-datetime.fromisoformat(s['as_of'])).total_seconds()
                    freshness='outdated' if age>self.stale_hours*3600 else 'current'
                except (TypeError,ValueError): freshness='not synced'
                s['freshness']=freshness;sources.append(s)
            data=fn(db,sources)
            return {'as_of':{s['id']:s['as_of'] for s in sources},'sources':sources,
                'unreliable_sources':[s for s in sources if s['status']!='healthy' or s['freshness']!='current'], 'data':data}

    @staticmethod
    def _filters(account_ids,institution,alias='a'):
        clauses=[];args=[]
        if account_ids is not None:
            if not account_ids or len(account_ids)>100: raise ValueError('Supply 1–100 opaque ids from list_accounts')
            if any(not isinstance(a,str) or not a.startswith('acct_') for a in account_ids): raise ValueError('Use account ids from list_accounts')
            clauses.append(alias+'.id IN ('+','.join('?' for _ in account_ids)+')');args.extend(account_ids)
        if institution is not None: clauses.append(alias+'.institution=?');args.append(institution)
        return (' AND '+' AND '.join(clauses)) if clauses else '',args

    def list_sources(self) -> dict:
        """Discover connected/imported sources, data kinds (balances/spending/holdings), sync times and plain-language health. No provider identifiers or credentials. All amounts elsewhere are decimal currency units, separated by currency. Check unreliable_sources before trusting totals."""
        return self._read(lambda db,sources:sources)

    def list_accounts(self,account_ids:list[str]|None=None,institution:str|None=None) -> dict:
        """Discover accounts and reusable opaque ids for filters. Generated display labels never contain owner/provider display text. Returns source kind, data kinds, availability (available/historical/unassigned), per-account as_of/freshness, decimal currency balances and positive amounts owed on credit/loan accounts. Missing values are null, not zero; masks are absent unless privately enabled. Institution is an exact discovered name."""
        where,args=self._filters(account_ids,institution)
        def read(db,sources):
            by_id={s['id']:s for s in sources};rows=[]
            for row in db.execute('SELECT * FROM accounts a WHERE 1=1'+where+' ORDER BY institution,display_name',args):
                a=dict(row);s=by_id[a.pop('source_ref')];a['data_kinds']=json.loads(a['data_kinds'])
                a.update(source_id=s['id'],source_kind=s['kind'],as_of=s['as_of'],freshness=s['freshness'],status=s['status'])
                last=a.pop('last_four')
                if self.include_last_four: a['last_four']=last or None
                rows.append(a)
            return rows
        return self._read(read)

    def list_categories(self,only_with_data:bool=True) -> dict:
        """Learn plaidmcp's source-independent category and detailed-category filter codes. Returns labels, posted-item counts, observed date coverage and has_data; set only_with_data=false for the whole supported taxonomy. Counts below configured min_group_count are folded into Other. Peer payments and bank transfers are distinct categories, excluded from cash_flow by default."""
        from taxonomy import CATEGORIES
        def read(db,sources):
            observed={(r['category'],r['category_detailed']):dict(r) for r in db.execute('SELECT category,category_detailed,SUM(count) AS count,MIN(day) AS first_date,MAX(day) AS last_date FROM daily GROUP BY category,category_detailed')}
            rows=[];folded=0
            for category,definition in CATEGORIES.items():
                for detail in definition[1:]:
                    raw=observed.get((category,detail),{})
                    count=raw.get('count',0)
                    if count<self.minimum: folded+=count;count=0;raw={}
                    if count or not only_with_data:
                        rows.append({'category':category,'label':definition[0],'category_detailed':detail,
                          'detailed_label':detail.replace('_',' ').title(),'count':count,'has_data':bool(count),
                          'first_date':raw.get('first_date'),'last_date':raw.get('last_date')})
            if folded: rows.append({'category':'other','label':'Other','category_detailed':'__other__','detailed_label':'Other (small groups)','count':folded,'has_data':True,'first_date':None,'last_date':None})
            return rows
        return self._read(read)

    def _activity(self,db,group_by,start,end,account_ids,institution,category,include_transfers,include_peer_transfers):
        expressions={'category':('d.category','d.category'),'category_detailed':('d.category_detailed','d.category_detailed'),
            'month':("substr(d.day,1,7)","substr(d.day,1,7)"),
            'week':("date(d.day,'-' || ((cast(strftime('%w',d.day) AS integer)+6)%7) || ' days')", "date(d.day,'-' || ((cast(strftime('%w',d.day) AS integer)+6)%7) || ' days')"),
            'account':('a.id','a.display_name'),'institution':('a.institution','a.institution')}
        if group_by not in expressions: raise ValueError('Invalid activity grouping')
        where,args=self._filters(account_ids,institution)
        where+=' AND d.day>=? AND d.day<?';args.extend((start,end))
        if category is not None: where+=' AND (d.category=? OR d.category_detailed=?)';args.extend((category,category))
        if not include_transfers: where+=" AND d.category!='bank_transfer'"
        if not include_peer_transfers: where+=" AND d.category!='peer_payment'"
        key,label=expressions[group_by]
        sql=f'''WITH grouped AS (SELECT {key} AS g,{label} AS label,d.currency,
          SUM(d.income+d.peer_in) AS income,SUM(d.spending+d.peer_out) AS spending,
          SUM(d.transfer_in) AS transfers_in,SUM(d.transfer_out) AS transfers_out,SUM(d.count) AS count
          FROM daily d JOIN accounts a ON a.id=d.account_ref WHERE 1=1 {where} GROUP BY g,label,d.currency),
          folded AS (SELECT CASE WHEN count<? THEN '__other__' ELSE g END AS g,
            CASE WHEN count<? THEN 'Other (small groups)' ELSE label END AS label,* FROM grouped)
          SELECT g AS 'group',label,currency,ROUND(SUM(income),2) AS income,ROUND(SUM(spending),2) AS spending,
            ROUND(SUM(transfers_in),2) AS transfers_in,ROUND(SUM(transfers_out),2) AS transfers_out,
            ROUND(SUM(income-spending+transfers_in-transfers_out),2) AS net,SUM(count) AS count
          FROM folded GROUP BY g,label,currency ORDER BY currency,g'''
        rows=[dict(r) for r in db.execute(sql,[*args,self.minimum,self.minimum])]
        for row in rows:
            a,b=start,end
            if group_by in ('month','week') and row['group']!='__other__':
                if group_by=='month': a,b=month_bounds(row['group'])
                else: a=row['group'];b=(date.fromisoformat(a)+timedelta(days=7)).isoformat()
                a=max(a,start);b=min(b,end)
            months,weeks=units(a,b)
            row['average_per_month']=round(row['spending']/months,2)
            row['average_per_week']=round(row['spending']/weeks,2)
        totals=[dict(r) for r in db.execute(f'''SELECT d.currency,ROUND(SUM(d.income+d.peer_in),2) AS income,
            ROUND(SUM(d.spending+d.peer_out),2) AS spending,ROUND(SUM(d.transfer_in),2) AS transfers_in,
            ROUND(SUM(d.transfer_out),2) AS transfers_out,ROUND(SUM(d.income+d.peer_in-d.spending-d.peer_out+d.transfer_in-d.transfer_out),2) AS net,
            SUM(d.count) AS count FROM daily d JOIN accounts a ON a.id=d.account_ref WHERE 1=1 {where} GROUP BY d.currency''',args)]
        months,weeks=units(start,end)
        for row in totals: row.update(average_per_month=round(row['spending']/months,2),average_per_week=round(row['spending']/weeks,2))
        return {'start':start,'end':end,'group_by':group_by,'groups':rows,'totals':totals}

    def cash_flow(self,group_by:ActivityGroup='month',start:str|None=None,end:str|None=None,
        account_ids:list[str]|None=None,institution:str|None=None,category:str|None=None,
        include_transfers:bool=False,include_peer_transfers:bool=False,
        compare_start:str|None=None,compare_end:str|None=None) -> dict:
        """Answer spending/income questions by category, detailed category, month, ISO Monday week, account or institution. Dates YYYY-MM-DD: start inclusive/end exclusive; default month-to-date. Use ids/names/codes from discovery. Decimal amounts per currency (USD stays USD): spending positive, refunds negative; income/transfer receipts positive; net=inflow-outflow. Bank/card transfers and peer payments default excluded independently. Peer opt-in treats receipts as income and sent payments as spending, not taxable-income classification. Returns counts, spending averages per calendar-month equivalent and seven-day week, totals and optional comparison period with absolute/percent deltas (null at zero baseline). No transaction details. min_group_count folds small groups into Other; it is not differential privacy."""
        start,end=default_period(start,end)
        if bool(compare_start)!=bool(compare_end): raise ValueError('Supply both comparison dates')
        if compare_start: compare_start,compare_end=default_period(compare_start,compare_end)
        def read(db,sources):
            result=self._activity(db,group_by,start,end,account_ids,institution,category,include_transfers,include_peer_transfers)
            result['comparison']=None
            if compare_start:
                other=self._activity(db,group_by,compare_start,compare_end,account_ids,institution,category,include_transfers,include_peer_transfers)
                def deltas(current,previous):
                    previous={r['currency']:r for r in previous};current={r['currency']:r for r in current};out=[]
                    for currency in sorted(previous.keys()|current.keys()):
                        a=current.get(currency,{});b=previous.get(currency,{})
                        row={'currency':currency}
                        for metric in ('income','spending','net'):
                            delta=round(a.get(metric,0)-b.get(metric,0),2);base=b.get(metric,0)
                            row[metric+'_delta']=delta;row[metric+'_percent_change']=round(100*delta/abs(base),2) if base else None
                        out.append(row)
                    return out
                result['comparison']={**other,'deltas':deltas(result['totals'],other['totals'])}
            return result
        return self._read(read)

    def _balances(self,db,group_by,account_ids,institution):
        if group_by not in ('type','account','institution'): raise ValueError('Invalid balance grouping')
        key={'type':'a.type','account':'a.id','institution':'a.institution'}[group_by]
        label='a.display_name' if group_by=='account' else key
        where,args=self._filters(account_ids,institution)
        where+=" AND a.availability!='historical'"
        rows=[dict(r) for r in db.execute(f'''SELECT {key} AS 'group',{label} AS label,currency,
          SUM(CASE WHEN type NOT IN ('credit','loan') THEN current ELSE 0 END) AS assets,
          SUM(CASE WHEN type IN ('credit','loan') THEN current ELSE 0 END) AS liabilities,
          SUM(CASE WHEN type IN ('credit','loan') THEN -current ELSE current END) AS net_worth,
          SUM(CASE WHEN type='depository' THEN current ELSE 0 END) AS cash,
          SUM(CASE WHEN type='investment' THEN current ELSE 0 END) AS invested,
          SUM(CASE WHEN type='credit' THEN current ELSE 0 END) AS credit_owed,
          COUNT(*)-COUNT(current) AS unknown_balances FROM accounts a WHERE 1=1 {where} GROUP BY {key},{label},currency''',args)]
        # Totals are computed independently in SQL, retaining unknown counts per currency.
        totals=[dict(r) for r in db.execute(f'''SELECT currency,SUM(CASE WHEN type NOT IN ('credit','loan') THEN current ELSE 0 END) AS assets,
          SUM(CASE WHEN type IN ('credit','loan') THEN current ELSE 0 END) AS liabilities,
          SUM(CASE WHEN type IN ('credit','loan') THEN -current ELSE current END) AS net_worth,
          SUM(CASE WHEN type='depository' THEN current ELSE 0 END) AS cash,
          SUM(CASE WHEN type='investment' THEN current ELSE 0 END) AS invested,
          SUM(CASE WHEN type='credit' THEN current ELSE 0 END) AS credit_owed,COUNT(*)-COUNT(current) AS unknown_balances
          FROM accounts a WHERE 1=1 {where} GROUP BY currency''',args)]
        return {'group_by':group_by,'groups':rows,'totals':totals}

    def balance_summary(self,group_by:BalanceGroup='type',account_ids:list[str]|None=None,institution:str|None=None) -> dict:
        """Answer net-worth and balance questions by type, account or institution. Decimal currency units; assets/cash/invested positive, liabilities and credit_owed positive amounts owed (negative for an actual credit balance), net_worth=assets-liabilities. Excludes historical accounts and counts unknown balances (including statement wallets) instead of inventing zero; totals with unknown_balances are partial. Investments use account balances, never balance plus holdings. No currency conversion."""
        return self._read(lambda db,sources:self._balances(db,group_by,account_ids,institution))

    def holdings(self,group_by:HoldingGroup='security',account_ids:list[str]|None=None,institution:str|None=None) -> dict:
        """Answer portfolio/allocation questions by security, account, asset_class or security_type. Decimal currency value/cost_basis/unrealized_gain=value-cost_basis; quantity is units and only meaningful per security. Missing basis/gain stays null. Allocation percentages are within the filtered portfolio per currency. Security rows include ticker/name; account ids match list_accounts. No trades, owners or account numbers. Small position groups fold into Other when configured."""
        if group_by not in ('security','account','asset_class','security_type'): raise ValueError('Invalid holdings grouping')
        where,args=self._filters(account_ids,institution)
        key={'security':"COALESCE(h.ticker,h.name,'unknown security')",'account':'a.id','asset_class':'h.asset_class','security_type':'h.security_type'}[group_by]
        label='a.display_name' if group_by=='account' else key
        def read(db,sources):
            sql=f'''WITH grouped AS (SELECT {key} AS g,{label} AS label,h.currency,
              CASE WHEN ?='security' THEN h.ticker END AS ticker,CASE WHEN ?='security' THEN h.name END AS name,
              CASE WHEN ?='security' THEN SUM(quantity) END AS quantity,SUM(value) AS value,
              CASE WHEN COUNT(cost_basis)=COUNT(*) THEN SUM(cost_basis) END AS cost_basis,SUM(h.count) AS count
              FROM holdings h JOIN accounts a ON a.id=h.account_ref WHERE 1=1 {where} GROUP BY g,label,h.currency,ticker,name),
              folded AS (SELECT CASE WHEN count<? THEN '__other__' ELSE g END AS g,CASE WHEN count<? THEN 'Other (small groups)' ELSE label END AS label,currency,
                CASE WHEN count>=? THEN ticker END AS ticker,CASE WHEN count>=? THEN name END AS name,
                CASE WHEN count>=? THEN quantity END AS quantity,value,cost_basis,count FROM grouped),
              summed AS (SELECT g AS 'group',label,currency,ticker,name,SUM(quantity) AS quantity,SUM(value) AS value,
                CASE WHEN COUNT(cost_basis)=COUNT(*) THEN SUM(cost_basis) END AS cost_basis,SUM(count) AS count
                FROM folded GROUP BY g,label,currency,ticker,name)
              SELECT *,value-cost_basis AS unrealized_gain,100.0*value/NULLIF(SUM(value) OVER(PARTITION BY currency),0) AS allocation_percent FROM summed ORDER BY currency,value DESC'''
            rows=[dict(r) for r in db.execute(sql,[group_by]*3+args+[self.minimum]*5)]
            totals=[dict(r) for r in db.execute(f'''SELECT h.currency,SUM(value) AS value,CASE WHEN COUNT(cost_basis)=COUNT(*) THEN SUM(cost_basis) END AS cost_basis,
                CASE WHEN COUNT(cost_basis)=COUNT(*) THEN SUM(value)-SUM(cost_basis) END AS unrealized_gain FROM holdings h JOIN accounts a ON a.id=h.account_ref WHERE 1=1 {where} GROUP BY h.currency''',args)]
            return {'group_by':group_by,'groups':rows,'totals':totals}
        return self._read(read)

    def overview(self) -> dict:
        """Start here for 'how are my finances?': net worth, cash/invested/credit owed, month-to-date spending, prior full month and same elapsed days, trailing-six-full-month median, and top current categories. Decimal amounts per currency; spending positive/refunds negative; liabilities positive owed. Transfers/peer payments excluded. Null median means less than six observed calendar months; no assumption that missing history is zero. Coverage/freshness is not a guarantee every transaction has arrived. Every result includes per-source as_of and unreliable_sources."""
        today=today_utc();start=today.replace(day=1);end=today+timedelta(days=1)
        n=start.year*12+start.month-2
        prev=date(n//12,n%12+1,1)
        prev_end=start
        previous_elapsed=min(start,prev+timedelta(days=today.day))
        n=start.year*12+start.month-7
        trailing=date(n//12,n%12+1,1)
        def read(db,sources):
            current=self._activity(db,'category',start.isoformat(),end.isoformat(),None,None,None,False,False)
            previous=self._activity(db,'month',prev.isoformat(),prev_end.isoformat(),None,None,None,False,False)
            elapsed=self._activity(db,'month',prev.isoformat(),previous_elapsed.isoformat(),None,None,None,False,False)
            typical=self._activity(db,'month',trailing.isoformat(),start.isoformat(),None,None,None,False,False)
            medians=[]
            currencies={r['currency'] for r in current['totals']}|{r['currency'] for r in typical['groups']}
            for currency in sorted(currencies):
                rows=[r for r in typical['groups'] if r['currency']==currency and r['group']!='__other__']
                medians.append({'currency':currency,'observed_months':len(rows),'median_monthly_spending':statistics.median([r['spending'] for r in rows]) if len(rows)==6 else None})
            comparisons=[]
            def by_currency(rows,c): return next((r['spending'] for r in rows if r['currency']==c),None)
            for c in sorted(currencies):
                amount=by_currency(current['totals'],c)
                row={'currency':c,'month_to_date':amount}
                for label,baseline in (
                    ('last_month',by_currency(previous['totals'],c)),
                    ('last_month_same_elapsed_days',by_currency(elapsed['totals'],c)),
                    ('typical_month',next(r['median_monthly_spending'] for r in medians if r['currency']==c))):
                    delta=round(amount-baseline,2) if amount is not None and baseline is not None else None
                    row[label]={'spending':baseline,'delta':delta,'percent_change':round(100*delta/abs(baseline),2) if baseline and delta is not None else None}
                comparisons.append(row)
            top=sorted(current['groups'],key=lambda r:r['spending'],reverse=True)[:5]
            return {'balances':self._balances(db,'type',None,None),'month_to_date':current['totals'],
              'last_month':previous['totals'],'last_month_same_elapsed_days':elapsed['totals'],
              'typical_month':medians,'spending_comparisons':comparisons,'top_categories':top,'start':start.isoformat(),'end':end.isoformat()}
        return self._read(read)
