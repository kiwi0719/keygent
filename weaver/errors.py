"""对外接口用的错误（weaverd 的 HTTP 层把它们变成 400 / 404 / 409）。message 是给人看的中文。"""
from __future__ import annotations


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BadRequest(Exception):
    pass
