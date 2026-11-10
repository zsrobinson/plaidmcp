"""Synthetic checks of the reusable temporary-session template; no sudo/network."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import pytest


def module():
    spec=importlib.util.spec_from_file_location('session_template',Path(__file__).parents[1]/'deploy/production_session.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


def test_keys_atomic_private_and_existing_audit_gate(tmp_path):
    m=module();path=tmp_path/'sync.env'
    path.write_text('PLAID_ENV=sandbox\nPLAIDMCP_SANDBOX_ONLY=true\nPLAIDMCP_INCLUDE_LAST_FOUR=false\n')
    m.install_keys(path,'synthetic-client-id','synthetic-production-secret',os.getuid(),os.getgid())
    assert path.stat().st_mode&0o777==0o600
    assert m.read_env(path)['PLAIDMCP_INCLUDE_LAST_FOUR']=='false'
    assert len(list(tmp_path.iterdir()))==1
    before=path.read_bytes()
    with pytest.raises(ValueError):m.install_keys(path,'newline\nKEY=value','synthetic-secret',os.getuid(),os.getgid())
    assert path.read_bytes()==before
    s=m.Session.__new__(m.Session);s.audit=None;s.child=None
    with pytest.raises(ValueError):s.dispatch({'op':'enable_timer'})
    with pytest.raises(ValueError):s.dispatch({'op':'arbitrary_root_command'})


def test_helper_refresh_verifies_pin_and_rejects_running_link(tmp_path,monkeypatch):
    import hashlib,json
    m=module();stage=tmp_path/'stage';dest=tmp_path/'dest'
    (stage/'source').mkdir(parents=True);(stage/'scripts').mkdir();(stage/'services/plaidmcp').mkdir(parents=True)
    link=b'fixture-link';audit=b'fixture-audit'
    (stage/'source/link.py').write_bytes(link);(stage/'scripts/plaidmcp-production-audit.py').write_bytes(audit)
    pin={'link_sha256':hashlib.sha256(link).hexdigest(),'audit_sha256':hashlib.sha256(audit).hexdigest()}
    (stage/'services/plaidmcp/onboarding.json').write_text(json.dumps(pin))
    monkeypatch.setattr(m.os,'chown',lambda *args:None)
    s=m.Session.__new__(m.Session);s.child=None;s.stage=stage;s.onboarding=dest
    s.refresh_helpers();assert (dest/'link.py').read_bytes()==link
    (stage/'source/link.py').write_bytes(b'changed')
    with pytest.raises(ValueError):s.refresh_helpers()
    s.child=SimpleNamespace(poll=lambda:None)
    with pytest.raises(ValueError):s.refresh_helpers()
