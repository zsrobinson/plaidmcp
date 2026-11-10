import anyio
import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

class AccessJWTMiddleware:
    """Validate Access assertions before the MCP transport can dispatch any tool."""

    def __init__(self, app: ASGIApp, team_domain: str, audience: str) -> None:
        self.app = app
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        # Cache the set for five minutes, not individual keys indefinitely.
        self.jwks = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs",
                                   cache_jwk_set=True, cache_keys=False,
                                   lifespan=300, timeout=5)

    def validate(self, token: str) -> None:
        if len(token) > 16384 or jwt.get_unverified_header(token).get("alg") != "RS256":
            raise jwt.InvalidTokenError("Expected RS256 assertion")
        key = self.jwks.get_signing_key_from_jwt(token)
        jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audience,
                   issuer=self.issuer, options={"require": ["aud", "iss", "exp"]})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            tokens = Headers(scope=scope).getlist("cf-access-jwt-assertion")
            try:
                if len(tokens) != 1 or not tokens[0]:
                    raise jwt.InvalidTokenError("Missing assertion")
                # JWKS retrieval is blocking: keep it off the event loop.
                await anyio.to_thread.run_sync(self.validate, tokens[0])
            except jwt.PyJWTError:
                # Do not log assertions or return validation/network details.
                response = JSONResponse({"error": "Invalid or missing Access assertion"},
                                        status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
