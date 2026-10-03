"""测试用的 MCP 服务器（stdio）。工具数量可以用环境变量 FAKE_MCP_EXTRA 调多，测延迟模式。"""
import base64
import os
from typing import Annotated, Literal

from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver import Context, MCPServer, Resolve
from mcp.server.mcpserver.resolve import Elicit, ListRoots, Sample
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import CreateMessageResult, ListRootsResult, SamplingMessage, TextContent, ToolAnnotations
from pydantic import BaseModel

# 1×1 的透明 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

server = MCPServer("fake", instructions="这是测试服务器：echo 原样返回，add 做加法，boom 总是报错。")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """原样返回输入的文字"""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """两个整数相加（会被当成有副作用，因为没标只读）"""
    return a + b


@server.tool()
def boom() -> str:
    """总是报错"""
    raise ValueError("故意出错")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def leak() -> str:
    """返回一段带假密钥的文字"""
    return "token=ghp_" + "a" * 32 + "WXYZ"


class Deploy(BaseModel):
    env: Literal["staging", "prod"]
    count: int = 1
    force: bool = False


if os.environ.get("FAKE_MCP_V2"):             # MCP 第二期的能力（design/mcp2.md）：提问、借模型、图片、roots、清单变化、prompt
    # 用 Resolve 标记要输入：新协议（2026-07-28）里变成 input_required 结果、客户端补齐后重试，
    # 旧协议里是调用中途单独发来的请求。两种客户端都走同一组回调。
    def ask_env() -> Elicit[Deploy]:
        return Elicit("部署到哪个环境？", Deploy)

    @server.tool()
    def deploy(choice: Annotated[ElicitationResult[Deploy], Resolve(ask_env)]) -> str:
        """部署：中途问用户要部署到哪个环境（elicitation）"""
        if choice.action == "accept":
            d = choice.data
            return f"deployed to {d.env} x{d.count} force={d.force}"
        return f"not deployed: {choice.action}"

    def ask_model(text: str) -> Sample:
        return Sample([SamplingMessage(role="user", content=TextContent(type="text", text=f"总结：{text}"))],
                      max_tokens=50)

    @server.tool()
    def summarize(text: str, r: Annotated[CreateMessageResult, Resolve(ask_model)]) -> str:
        """借用客户端的模型总结一段话（sampling）"""
        return "summary: " + r.content.text

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def picture() -> list:
        """返回一段文字和一张图片"""
        return ["这是一张图", Image(data=PNG, format="png")]

    def ask_roots() -> ListRoots:
        return ListRoots()

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def where(r: Annotated[ListRootsResult, Resolve(ask_roots)]) -> str:
        """问客户端能碰哪些目录（roots）"""
        return "roots: " + ",".join(str(x.uri) for x in r.roots)

    @server.tool()
    async def grow(ctx: Context) -> str:
        """加一个新工具 grown，并通知客户端工具清单变了"""
        server.add_tool(lambda: "grown!", name="grown", description="新长出来的工具")
        await ctx.notify_tools_changed()
        return "grew"

    @server.prompt()
    def review(pr: str, focus: str = "") -> str:
        """审查一个 PR"""
        return f"请审查 PR {pr}" + (f"，重点看{focus}" if focus else "")


@server.resource("memo://readme", description="说明文档")
def readme() -> str:
    return "资源正文：hello resource"


for i in range(int(os.environ.get("FAKE_MCP_EXTRA", "0"))):
    def make(i=i):
        def extra(x: str) -> str:
            return f"extra{i}: {x}"
        extra.__name__ = f"extra_{i}"
        extra.__doc__ = f"第 {i} 个额外工具，关键字 kw{i}"
        return extra
    server.tool()(make())

if __name__ == "__main__":
    server.run("stdio")
