"""测试用的 MCP 服务器（stdio）。工具数量可以用环境变量 FAKE_MCP_EXTRA 调多，测延迟模式。"""
import os

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

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
