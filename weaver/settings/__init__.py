"""设置页的后端：模型（.env）、用户级 MCP 服务器（mcp.json）、用户级 skills。见 design/settings.md。

这里只碰文件，不碰 HTTP；weaverd 的 /v1/settings/* 路由经 service.Settings 调用它们。
"""
