"""Exercise rendered browser headers, not only the Python HTTP handler."""
import re
import subprocess
import shutil
import link


def test_rendered_csrf_handoff_preserves_http_header_name():
    # The real template renderer must substitute data without touching X-CSRF.
    page=link.render_page('synthetic-csrf',None,'Example Bank',[],False)
    assert page.count("'X-CSRF'")==2
    script=re.search(r'<script>\s*([\s\S]*?)</script>',page)[1]
    harness='''
const vm=require('node:vm');
let calls=0;
const button={};
const context={document:{getElementById:()=>button},alert:()=>{},
  fetch:async(path,options)=>{new Headers(options.headers);if(options.headers['X-CSRF']!=='synthetic-csrf')throw Error('CSRF handoff failed');calls++;return {ok:true,json:async()=>({link_token:'synthetic-link'})}},
  Plaid:{create:options=>({open:()=>{if(options.token!=='synthetic-link')throw Error('Token handoff failed')}})}};
vm.createContext(context);vm.runInContext(SCRIPT,context);
button.onclick().then(()=>{if(calls!==1)process.exit(2)}).catch(()=>process.exit(3));
'''.replace('SCRIPT',__import__('json').dumps(script))
    if shutil.which('node'):
        subprocess.run(['node','-e',harness],check=True,capture_output=True)
