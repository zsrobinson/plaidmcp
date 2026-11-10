"""Owner-only local helper; tunnel 127.0.0.1:8768 over SSH. Never route publicly."""
import argparse
import hmac
import http.server
import json
import re
import secrets
from plaid_client import Client, PlaidError
from sync import private_store

HTML = """<!doctype html><meta charset=utf-8><title>Link money account</title>
<p>Production Trial: only 10 Items total; removing one does not recover a slot.
Use update mode for a linked institution. Close this helper after linking.</p>
<button id=link>Open Plaid Link</button>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
const csrf=CSRF;
const expected=EXPECTED;
const expectedName=EXPECTED_NAME;
const linked=LINKED;
const blockedNames=BLOCKED_NAMES;
const normalize=s=>(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
let handler;
document.getElementById('link').onclick=async()=>{
 const r=await fetch('/token',{method:'POST',headers:{'X-CSRF':csrf}});
 const data=await r.json(); if(!r.ok){alert(data.error);return;}
 handler=Plaid.create({token:data.link_token,
 onEvent:(event,meta)=>{if(event==='SELECT_INSTITUTION' && ((expected && meta.institution_id!==expected) || (expectedName && !normalize(meta.institution_name).includes(normalize(expectedName))) || linked.includes(meta.institution_id) || blockedNames.some(n=>normalize(meta.institution_name).includes(n)))){handler.exit({force:true});alert('Wrong or already linked institution. Close this window; restart or use update mode instead of creating another Item.');}},
 onSuccess:async(public_token,metadata)=>{
 const r=await fetch('/exchange',{method:'POST',headers:{'X-CSRF':csrf,'Content-Type':'application/json'},body:JSON.stringify({public_token,institution_id:metadata.institution.institution_id,institution_name:metadata.institution.name})});
 alert(r.ok?'Linked. Close this helper.':'Link failed; inspect sync status.');
 }}); handler.open();
};</script>"""

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--institution',help='Optional verified institution ID; otherwise choose in the native Plaid Link search')
    parser.add_argument('--institution-name',help='Optional institution name hint to refuse wrong-bank selection before authentication')
    parser.add_argument('--product',choices=['transactions','investments'],required=True)
    parser.add_argument('--update',action='store_true',help='Re-link existing Item without consuming a slot')
    args=parser.parse_args()
    if args.update and not args.institution: parser.error('--update requires --institution')
    client=Client()
    # Exclusive lock held for helper lifetime prevents duplicate helpers/sync races.
    with private_store() as db:
        existing=db.execute('SELECT item,token FROM credentials WHERE institution_id=?',(args.institution,)).fetchone()
        csv_source=bool(db.execute("SELECT 1 FROM items WHERE id='venmo-csv'").fetchone())
        if args.institution_name and not args.institution:
            hint=re.sub(r'[^a-z0-9]','',args.institution_name.casefold())
            if 'venmo' in hint and csv_source: raise SystemExit('Venmo CSV source exists; choose one source before linking')
            for name, in db.execute('SELECT institution FROM items'):
                if hint and hint in re.sub(r'[^a-z0-9]','',name.casefold()):
                    raise SystemExit('Institution already linked. Use --update with its verified institution ID.')
        if args.institution and db.execute("SELECT 1 FROM items WHERE id='venmo-csv'").fetchone() and not existing:
            institution=client.call('/institutions/get_by_id',institution_id=args.institution,country_codes=['US'])['institution']['name']
            if 'venmo' in institution.casefold(): raise SystemExit('Venmo CSV source exists. Choose one source before linking; no Item created.')
        if existing and not args.update: raise SystemExit('Institution already linked. Use --update; no new Item created.')
        if args.update and not existing: raise SystemExit('No existing Item for update.')
        linked=[r[0] for r in db.execute('SELECT institution_id FROM credentials') if not existing or r[0]!=args.institution]
        csrf=secrets.token_urlsafe(32)
        class Handler(http.server.BaseHTTPRequestHandler):
            completed=False
            issued=False
            def log_message(self,*args): pass
            def valid(self):
                return self.headers.get('Host') in ('127.0.0.1:8768','localhost:8768') and self.headers.get('Origin') in (None,'http://127.0.0.1:8768','http://localhost:8768')
            def reply(self,status,data,kind='application/json'):
                body=data.encode() if isinstance(data,str) else json.dumps(data).encode()
                self.send_response(status); self.send_header('Content-Type',kind)
                self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                if not self.valid() or self.path!='/': return self.reply(403,{'error':'Forbidden'})
                self.reply(200,HTML.replace('CSRF',json.dumps(csrf)).replace('EXPECTED_NAME',json.dumps(args.institution_name)).replace('EXPECTED',json.dumps(args.institution)).replace('LINKED',json.dumps(linked)).replace('BLOCKED_NAMES',json.dumps(['venmo'] if csv_source else [])),'text/html')
            def do_POST(self):
                if not self.valid() or not hmac.compare_digest(self.headers.get('X-CSRF',''),csrf): return self.reply(403,{'error':'Forbidden'})
                if Handler.completed: return self.reply(409,{'error':'Already completed; close helper'})
                try:
                    if self.path=='/token':
                        if Handler.issued: return self.reply(409,{'error':'Token already issued; restart helper to retry'})
                        payload={'client_name':'Plaid MCP','user':{'client_user_id':'local-owner'},'country_codes':['US'],'language':'en'}
                        if existing: payload['access_token']=existing[1]
                        else: payload['products']=[args.product]
                        result=client.call('/link/token/create',**payload)
                        Handler.issued=True
                        return self.reply(200,{'link_token':result['link_token']})
                    if self.path!='/exchange' or not Handler.issued: return self.reply(404,{'error':'Unknown request'})
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0 < length < 8192: raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    institution=data.get('institution_id')
                    name=data.get('institution_name') or ''
                    if not isinstance(institution,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',institution): raise ValueError()
                    if args.institution and institution!=args.institution: raise ValueError()
                    if args.institution_name and re.sub(r'[^a-z0-9]','',args.institution_name.casefold()) not in re.sub(r'[^a-z0-9]','',name.casefold()): raise ValueError()
                    if not existing and db.execute('SELECT 1 FROM credentials WHERE institution_id=?',(institution,)).fetchone():
                        return self.reply(409,{'error':'Institution already linked; use update mode'})
                    if 'venmo' in name.casefold() and db.execute("SELECT 1 FROM items WHERE id='venmo-csv'").fetchone():
                        return self.reply(409,{'error':'Venmo CSV source already exists; choose one source'})
                    if not existing:
                        result=client.call('/item/public_token/exchange',public_token=data['public_token'])
                        # Persist immediately: a later metadata failure must not lose an already-created Item.
                        with db:
                            db.execute('INSERT INTO credentials VALUES(?,?,?,?,NULL)',(result['item_id'],institution,result['access_token'],args.product))
                            db.execute('INSERT INTO items VALUES(?,?,NULL,?)',(result['item_id'],name or institution,'INITIAL_SYNC_REQUIRED'))
                    Handler.completed=True
                    return self.reply(200,{'linked':True})
                except Exception: self.reply(400,{'error':'Link failed; no request details logged'})
        print('Open http://127.0.0.1:8768 through an SSH tunnel. Press Ctrl-C after linking.')
        http.server.HTTPServer(('127.0.0.1',8768),Handler).serve_forever()

if __name__=='__main__': main()
