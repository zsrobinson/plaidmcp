import time
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from access import AccessJWTMiddleware
from server import create_app

async def test_access_gate(snapshot,monkeypatch):
    _,path=snapshot
    monkeypatch.setenv('ACCESS_TEAM_DOMAIN','test.cloudflareaccess.com')
    monkeypatch.setenv('ACCESS_AUD','money-audience')
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    class JWK:
        def __init__(self,*args,**kwargs): pass
        def get_signing_key_from_jwt(self,token):
            class Signing: pass
            signing=Signing(); signing.key=key.public_key(); return signing
    monkeypatch.setattr(jwt,'PyJWKClient',JWK)
    app=create_app(path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8767') as c:
        for headers in ({},{'Cf-Access-Jwt-Assertion':'garbage'}):
            assert (await c.post('/mcp',headers=headers)).status_code==401
    middleware=AccessJWTMiddleware(app,'test.cloudflareaccess.com','money-audience')
    claims={'iss':'https://test.cloudflareaccess.com','aud':'money-audience','exp':time.time()+60}
    middleware.validate(jwt.encode(claims,key,algorithm='RS256'))
    for changed in ({'aud':'wrong'},{'iss':'https://wrong.cloudflareaccess.com'},{'exp':0}):
        with pytest.raises(jwt.PyJWTError): middleware.validate(jwt.encode({**claims,**changed},key,algorithm='RS256'))

def test_partial_access_fails(monkeypatch):
    monkeypatch.setenv('ACCESS_TEAM_DOMAIN','test.cloudflareaccess.com'); monkeypatch.delenv('ACCESS_AUD',raising=False)
    with pytest.raises(ValueError): create_app()


def test_public_deployment_requires_access(monkeypatch):
    monkeypatch.setenv('PLAIDMCP_REQUIRE_ACCESS','true')
    monkeypatch.delenv('ACCESS_TEAM_DOMAIN',raising=False);monkeypatch.delenv('ACCESS_AUD',raising=False)
    with pytest.raises(ValueError,match='Access configuration is required'): create_app()


def test_public_hostname_is_configured_and_validated(monkeypatch):
    from server import create_server
    monkeypatch.setenv('PLAIDMCP_PUBLIC_HOST','finance.example.org')
    assert create_server().settings.transport_security.allowed_hosts[-1]=='finance.example.org'
    monkeypatch.setenv('PLAIDMCP_PUBLIC_HOST','https://unsafe.example.org/path')
    with pytest.raises(ValueError): create_server()
