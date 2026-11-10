"""Private, mutual-unique transfer matching; no provider identifiers leave this layer."""
import os


def match_internal(db):
 db.execute('DELETE FROM internal_matches')
 disabled=list(filter(None,os.environ.get('PLAIDMCP_DISABLED_SOURCES','').split(',')))
 excluded=' AND COALESCE(s.ref,\'\') NOT IN ('+','.join('?' for _ in disabled)+')' if disabled else ''
 sql=f'''WITH eligible AS (
   SELECT t.id,t.item,t.date,t.currency,CAST(ROUND(t.amount*100) AS INTEGER) AS cents,m.ref AS account
   FROM transactions t JOIN account_alias m ON m.item=t.item AND m.provider_ref=t.account
   LEFT JOIN source_identity s ON s.provider_ref=t.item
   WHERE t.pending=0 AND t.flow_kind='bank_transfer' AND t.amount!=0 {excluded}),
   pairs AS (SELECT a.id AS outgoing,a.item AS out_item,b.id AS incoming,b.item AS in_item,
     COUNT(*) OVER(PARTITION BY a.item,a.id) AS out_options,
     COUNT(*) OVER(PARTITION BY b.item,b.id) AS in_options
     FROM eligible a JOIN eligible b ON a.cents=-b.cents AND a.currency=b.currency
     AND a.account!=b.account AND ABS(julianday(a.date)-julianday(b.date))<=3 WHERE a.cents>0)
   INSERT INTO internal_matches SELECT out_item,outgoing,in_item,incoming FROM pairs WHERE out_options=1 AND in_options=1
   UNION ALL SELECT in_item,incoming,out_item,outgoing FROM pairs WHERE out_options=1 AND in_options=1'''
 db.execute(sql,disabled)
