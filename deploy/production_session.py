"""Temporary, owner-started privilege session restricted to Plaid MCP onboarding.

No TCP control listener, generic command execution, returned keys, or bank writes.
The root session expires after two hours and stops its own Link child on exit.
"""
import argparse,hashlib,json,os,pwd,re,shlex,signal,socket,sqlite3,stat,struct,subprocess,sys,tempfile,time
from pathlib import Path

BASE=Path('/opt/fleet-plaidmcp/current')
PRIVATE=Path('/var/lib/plaidmcp-private')
ENV=PRIVATE/'.config/plaidmcp/sync.env'
PUBLIC=Path('/var/lib/plaidmcp/aggregate.sqlite')
CONTROL=Path('/run/plaidmcp-production/control.sock')
BANKS={'capital_one':('transactions','Capital One'),'schwab':('investments','Schwab'),'venmo':('transactions','Venmo - Personal')}


def read_env(path):
    values={}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);values[key]=' '.join(shlex.split(value))
    return values


def install_keys(path,client_id,secret,uid,gid):
    for value in (client_id,secret):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,200}',value):raise ValueError('Invalid credential format')
    settings=read_env(path)
    settings.update(PLAID_CLIENT_ID=client_id,PLAID_SECRET=secret,PLAID_ENV='production',PLAIDMCP_SANDBOX_ONLY='false')
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.production-')
    try:
        os.fchmod(fd,0o600);os.fchown(fd,uid,gid)
        with os.fdopen(fd,'w') as f:
            f.write(''.join(k+'='+shlex.quote(v)+'\n' for k,v in settings.items()));f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):subprocess.run(['shred','--remove=unlink','--',name],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


class Session:
    def __init__(self,stage):
        self.stage=stage;self.child=None;self.bank=None;self.audit=None
        self.onboarding=Path('/opt/fleet-plaidmcp/onboarding')
        self.refresh_helpers()
    def refresh_helpers(self):
        if self.child and self.child.poll() is None:raise ValueError('Stop Link before refreshing helper code')
        self.onboarding.mkdir(mode=0o755,exist_ok=True)
        release=json.loads((self.stage/'services/plaidmcp/onboarding.json').read_text())
        for src,dst,expected in ((self.stage/'source/link.py',self.onboarding/'link.py',release['link_sha256']),
            (self.stage/'scripts/plaidmcp-production-audit.py',self.onboarding/'audit.py',release['audit_sha256'])):
            data=src.read_bytes()
            if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('Onboarding source verification failed')
            target=dst.with_suffix('.new');target.write_bytes(data);target.chmod(0o644);os.chown(target,0,0);os.replace(target,dst)
    def settings(self):
        values=read_env(ENV)
        if values.get('PLAID_ENV')!='production' or values.get('PLAIDMCP_SANDBOX_ONLY')!='false' or not values.get('PLAID_CLIENT_ID') or not values.get('PLAID_SECRET'):
            raise ValueError('Production credentials not installed')
        return {'PATH':'/usr/sbin:/usr/bin:/bin','HOME':str(PRIVATE),'PYTHONPATH':str(BASE),'PYTHONDONTWRITEBYTECODE':'1',**values}
    def command(self,*args):
        return ['/usr/sbin/runuser','-u','plaidmcp-sync','--',str(BASE/'.venv/bin/python'),*map(str,args)]
    def stop_link(self):
        if self.child and self.child.poll() is None:
            os.killpg(self.child.pid,signal.SIGTERM)
            try:self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(self.child.pid,signal.SIGKILL);self.child.wait(timeout=5)
        self.child=None;self.bank=None
    def status(self):
        with sqlite3.connect((PRIVATE/'private.sqlite').as_uri()+'?mode=ro',uri=True) as db:
            linked=db.execute('SELECT COUNT(*) FROM credentials').fetchone()[0]
        info={}
        for path in (PRIVATE,PUBLIC.parent,PRIVATE/'private.sqlite',PUBLIC,ENV):
            s=path.stat();info[str(path)]={'mode':oct(s.st_mode&0o7777),'owner':pwd.getpwuid(s.st_uid).pw_name}
        current=read_env(ENV)
        return {'linked_items':linked,'helper_running':bool(self.child and self.child.poll() is None),'helper_bank':self.bank,
            'production_configured':current.get('PLAID_ENV')=='production' and bool(current.get('PLAID_SECRET')),
            'timer_enabled':subprocess.run(['systemctl','is-enabled','--quiet','fleet-plaidmcp-sync.timer']).returncode==0,
            'state_permissions':info}
    def dispatch(self,request):
        operation=request.get('op')
        if operation=='status':return self.status()
        if operation=='refresh_helpers':self.refresh_helpers();return {'helpers_refreshed':True}
        if operation=='maintenance':
            if request.get('action') not in ('deploy','verify'):raise ValueError('Unapproved maintenance action')
            self.stop_link()
            pin=json.loads((self.stage/'services/plaidmcp/maintenance.json').read_text())
            data=(self.stage/'scripts/plaidmcp-maintenance.py').read_bytes()
            if hashlib.sha256(data).hexdigest()!=pin['sha256']:raise ValueError('Maintenance source verification failed')
            script=self.onboarding/'maintenance.py';pending=script.with_suffix('.new')
            pending.write_bytes(data);pending.chmod(0o644);os.chown(pending,0,0);os.replace(pending,script)
            result=subprocess.run([sys.executable,str(script),request['action'],str(self.stage)],capture_output=True,text=True,timeout=900)
            try:report=json.loads(result.stdout)
            except ValueError:raise ValueError('Maintenance failed without safe diagnostics') from None
            if result.returncode or not report.get('verified'):return {'error':'Maintenance verification did not pass; scheduler remains paused'}
            self.audit=report;self.digest=hashlib.sha256(PUBLIC.read_bytes()).hexdigest()
            return report
        if operation=='install_keys':
            self.stop_link();identity=pwd.getpwnam('plaidmcp-sync')
            previous=read_env(ENV)
            with sqlite3.connect((PRIVATE/'private.sqlite').as_uri()+'?mode=ro',uri=True) as db:
                linked=db.execute('SELECT COUNT(*) FROM credentials').fetchone()[0]
            if linked and (previous.get('PLAID_CLIENT_ID')!=request.get('client_id') or previous.get('PLAID_SECRET')!=request.get('secret')):
                raise ValueError('Linked Items exist; refusing to replace their Production credentials')
            install_keys(ENV,request.get('client_id'),request.get('secret'),identity.pw_uid,identity.pw_gid)
            self.audit=None;return {'installed':True,'environment':'production','endpoint_product_and_access_guards':'unchanged'}
        if operation=='start_link':
            if request.get('bank') not in BANKS:raise ValueError('Unapproved bank selection')
            # Pause only this application's scheduler while adding a source. A successful
            # sync/privacy audit must resume it explicitly with enable_timer.
            if subprocess.run(['systemctl','is-active','--quiet','fleet-plaidmcp-sync.timer']).returncode==0:
                subprocess.run(['systemctl','stop','fleet-plaidmcp-sync.timer'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if subprocess.run(['systemctl','is-active','--quiet','fleet-plaidmcp-sync.service']).returncode==0:
                raise ValueError('A scheduled sync is still finishing; retry Link after it completes')
            self.stop_link()
            with socket.socket() as probe:probe.bind(('127.0.0.1',8768))
            product,hint=BANKS[request['bank']]
            self.child=subprocess.Popen(self.command(self.onboarding/'link.py','--product',product,'--institution-name',hint),env=self.settings(),cwd=BASE,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            time.sleep(.5)
            if self.child.poll() is not None:raise ValueError('Link helper stopped; institution may already be linked or unavailable')
            self.bank=request['bank'];self.audit=None;return {'link_started':True,'bank':self.bank,'url':'http://127.0.0.1:8768'}
        if operation=='stop_link':self.stop_link();return {'link_stopped':True}
        if operation=='sync_verify':
            self.stop_link()
            result=subprocess.run(self.command(self.onboarding/'audit.py'),env=self.settings(),cwd=BASE,capture_output=True,text=True,timeout=240)
            if result.returncode:
                self.audit=None
                try:diagnostic=json.loads(result.stdout)
                except ValueError:return {'error':'Audit failed without safe diagnostics; timer remains disabled'}
                safe={k:v for k,v in diagnostic.items() if k in ('phase','error_type','table','column','match_length') and (isinstance(v,int) or isinstance(v,str) and __import__('re').fullmatch(r'[A-Za-z_]{1,80}',v))}
                return {'error':'Production sync/privacy audit did not pass; timer remains disabled',**safe}
            self.audit=json.loads(result.stdout)
            self.digest=hashlib.sha256(PUBLIC.read_bytes()).hexdigest()
            return self.audit
        if operation=='enable_timer':
            if not self.audit or not self.audit.get('ready_for_timer') or hashlib.sha256(PUBLIC.read_bytes()).hexdigest()!=self.digest or (self.child and self.child.poll() is None):
                raise ValueError('A successful Item, stopped Link, and current successful privacy audit are required')
            subprocess.run(['systemctl','enable','--now','fleet-plaidmcp-sync.timer'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return {'timer_enabled':True}
        if operation=='close':self.stop_link();return {'closed':True}
        raise ValueError('Unknown onboarding operation')


def serve():
    if os.geteuid()!=0 or not os.environ.get('SUDO_UID') or int(os.environ['SUDO_UID'])==0:
        raise SystemExit('Start with sudo from the approved operator account.')
    owner=pwd.getpwuid(int(os.environ['SUDO_UID']))
    if owner.pw_name!=os.environ.get('SUDO_USER'):raise SystemExit('Unexpected operator; no session started.')
    if CONTROL.exists():
        if not stat.S_ISSOCK(CONTROL.lstat().st_mode):raise SystemExit('Unexpected control path; no session started.')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
            try:probe.connect(str(CONTROL))
            except ConnectionRefusedError:CONTROL.unlink()
            else:raise SystemExit('An onboarding session is already listening; do not replace it.')
    CONTROL.parent.mkdir(mode=0o710,exist_ok=True);os.chown(CONTROL.parent,0,owner.pw_gid);CONTROL.parent.chmod(0o710)
    session=Session(Path(__file__).resolve().parent.parent)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(str(CONTROL));os.chown(CONTROL,owner.pw_uid,owner.pw_gid);CONTROL.chmod(0o600)
    listener.listen(1);listener.settimeout(2);deadline=time.monotonic()+7200
    print('Production onboarding session ready. Keep this terminal open. It expires in two hours.',flush=True)
    try:
        while time.monotonic()<deadline:
            try:conn,_=listener.accept()
            except socket.timeout:continue
            with conn:
                pid,uid,gid=struct.unpack('3i',conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid!=owner.pw_uid:continue
                conn.settimeout(10);data=b''
                try:
                    while not data.endswith(b'\n'):
                        part=conn.recv(4096)
                        if not part:break
                        data+=part
                        if len(data)>16384:raise ValueError('Request too large')
                    request=json.loads(data);response=session.dispatch(request)
                except Exception as error:
                    response={'error':str(error) if isinstance(error,ValueError) else 'Onboarding operation failed; no sensitive details logged'}
                try:conn.sendall(json.dumps(response).encode()+b'\n')
                except (BrokenPipeError,ConnectionResetError):continue
                if response.get('closed'):break
    finally:
        session.stop_link();listener.close();CONTROL.unlink(missing_ok=True)
        try:CONTROL.parent.rmdir()
        except OSError:pass


def client():
    data=sys.stdin.buffer.read(16385)
    if len(data)>16384:raise SystemExit('Request too large')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.connect(str(CONTROL));conn.settimeout(300);conn.sendall(data.rstrip()+b'\n');out=b''
        while not out.endswith(b'\n'):
            part=conn.recv(4096)
            if not part:break
            out+=part
        print(out.decode().strip())

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');args=parser.parse_args()
    try:serve() if args.serve else client()
    except KeyboardInterrupt:pass
    except Exception as error:raise SystemExit('Production session stopped ('+type(error).__name__+'); no sensitive values logged.') from None
