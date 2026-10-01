"""测试用的要登录的 MCP 服务器（Streamable HTTP + OAuth），用 SDK 自带的授权服务器实现。

python oauth_mcp_server.py <端口>
- 支持动态注册客户端；授权时不问人，直接重定向回 redirect_uri（测试代替浏览器去访问授权地址即可）
- 访问令牌 FAKE_TOKEN_TTL 秒后过期（默认 3600），可以用刷新令牌续
- 环境变量 FAKE_REVOKE_FILE 指的文件存在时：所有令牌都无效、刷新也失败（模拟授权服务器撤销）
"""
import os
import secrets
import sys
import time
from urllib.parse import urlencode

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthToken

PORT = int(sys.argv[1])
BASE = f"http://127.0.0.1:{PORT}"
TTL = int(os.environ.get("FAKE_TOKEN_TTL", "3600"))
REVOKE = os.environ.get("FAKE_REVOKE_FILE", "")


def revoked() -> bool:
    return bool(REVOKE) and os.path.exists(REVOKE)


class Provider:
    def __init__(self):
        self.clients, self.codes, self.tokens, self.refresh = {}, {}, {}, {}

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params):
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return f"{params.redirect_uri}?{urlencode({'code': code, 'state': params.state or ''})}"

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    def _issue(self, client_id, scopes, resource):
        access, refresh = "at_" + secrets.token_urlsafe(16), "rt_" + secrets.token_urlsafe(16)
        self.tokens[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=int(time.time()) + TTL, resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=access, expires_in=TTL, refresh_token=refresh, scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return None if revoked() else self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if revoked():
            raise TokenError("invalid_grant", "revoked")
        self.refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        t = self.tokens.get(token)
        if revoked() or t is None or (t.expires_at and t.expires_at < time.time()):
            return None
        return t

    async def revoke_token(self, token):
        self.tokens.pop(getattr(token, "token", ""), None)


server = MCPServer(
    "oauth-fake", instructions="要登录的测试服务器：whoami 返回调用者。",
    auth_server_provider=Provider(),
    auth=AuthSettings(issuer_url=BASE, resource_server_url=f"{BASE}/mcp",
                      client_registration_options=ClientRegistrationOptions(enabled=True)))


@server.tool()
def whoami() -> str:
    """返回“已登录”"""
    return "已登录的用户"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=PORT, log_level="warning")
