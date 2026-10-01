"""脱敏：工具结果进账本之前，把疑似密钥换成占位符。见 design/redaction.md。

两类识别：
- 规则：移植自 Claude Code（secretScanner.ts）挑过的 gitleaks 高置信度规则（gitleaks 是 MIT 许可）——
  只要带明显前缀的密钥，误报率接近零；通用的关键字规则（password=…）一律不要。
- 已知的值：本进程环境变量里名字像密钥、值够长的，按值原文替换（覆盖 OpenRouter 这类规则里没有的 key）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

PLACEHOLDER = "[已脱敏 "
SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)", re.I)
MIN_KNOWN = 12          # 环境变量里的值至少这么长才当密钥（太短的容易误伤）
MIN_PARTIAL = 16        # 至少这么长才保留首 8 末 4，否则整个换掉
_B = r"(?:[`'\"\s;]|\\[nr]|$)"          # gitleaks 常用的结尾边界（不算进密钥本身）

# (规则 id, 正则)。正则里有分组时，替换第 1 组；没有就替换整个匹配。
RULES: list[tuple[str, str]] = [
    # 云服务
    ("aws-access-token", r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b"),
    ("gcp-api-key", r"\b(AIza[\w-]{35})" + _B),
    ("azure-ad-client-secret",
     r"(?:^|[\\'\"`\s>=:(,)])([a-zA-Z0-9_~.]{3}\dQ~[a-zA-Z0-9_~.-]{31,34})(?:$|[\\'\"`\s<),])"),
    ("digitalocean-pat", r"\b(dop_v1_[a-f0-9]{64})" + _B),
    ("digitalocean-access-token", r"\b(doo_v1_[a-f0-9]{64})" + _B),
    # AI 服务
    ("anthropic-api-key", r"\b(sk-ant-api03-[a-zA-Z0-9_\-]{93}AA)" + _B),
    ("anthropic-admin-api-key", r"\b(sk-ant-admin01-[a-zA-Z0-9_\-]{93}AA)" + _B),
    ("openai-api-key",
     r"\b(sk-(?:proj|svcacct|admin)-(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})T3BlbkFJ(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})\b"
     r"|sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})" + _B),
    ("huggingface-access-token", r"\b(hf_[a-zA-Z]{34})" + _B),
    # 代码托管
    ("github-pat", r"ghp_[0-9a-zA-Z]{36}"),
    ("github-fine-grained-pat", r"github_pat_\w{82}"),
    ("github-app-token", r"(?:ghu|ghs)_[0-9a-zA-Z]{36}"),
    ("github-oauth", r"gho_[0-9a-zA-Z]{36}"),
    ("github-refresh-token", r"ghr_[0-9a-zA-Z]{36}"),
    ("gitlab-pat", r"glpat-[\w-]{20}"),
    ("gitlab-deploy-token", r"gldt-[0-9a-zA-Z_\-]{20}"),
    # 通讯
    ("slack-bot-token", r"xoxb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*"),
    ("slack-user-token", r"xox[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34}"),
    ("slack-app-token", r"(?i)xapp-\d-[A-Z0-9]+-\d+-[a-z0-9]+"),
    ("twilio-api-key", r"SK[0-9a-fA-F]{32}"),
    ("sendgrid-api-token", r"\b(SG\.[a-zA-Z0-9=_\-.]{66})" + _B),
    # 开发工具
    ("npm-access-token", r"\b(npm_[a-zA-Z0-9]{36})" + _B),
    ("pypi-upload-token", r"pypi-AgEIcHlwaS5vcmc[\w-]{50,1000}"),
    ("databricks-api-token", r"\b(dapi[a-f0-9]{32}(?:-\d)?)" + _B),
    ("hashicorp-tf-api-token", r"[a-zA-Z0-9]{14}\.atlasv1\.[a-zA-Z0-9\-_=]{60,70}"),
    ("pulumi-api-token", r"\b(pul-[a-f0-9]{40})" + _B),
    ("postman-api-token", r"\b(PMAK-[a-fA-F0-9]{24}-[a-fA-F0-9]{34})" + _B),
    # 监控
    ("grafana-api-key", r"\b(eyJrIjoi[A-Za-z0-9+/]{70,400}={0,3})" + _B),
    ("grafana-cloud-api-token", r"\b(glc_[A-Za-z0-9+/]{32,400}={0,3})" + _B),
    ("grafana-service-account-token", r"\b(glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8})" + _B),
    ("sentry-user-token", r"\b(sntryu_[a-f0-9]{64})" + _B),
    ("sentry-org-token",
     r"\bsntrys_eyJpYXQiO[a-zA-Z0-9+/]{10,200}(?:LCJyZWdpb25fdXJs|InJlZ2lvbl91cmwi|cmVnaW9uX3VybCI6)"
     r"[a-zA-Z0-9+/]{10,200}={0,2}_[a-zA-Z0-9+/]{43}"),
    # 支付
    ("stripe-access-token", r"\b((?:sk|rk)_(?:test|live|prod)_[a-zA-Z0-9]{10,99})" + _B),
    ("shopify-access-token", r"shpat_[a-fA-F0-9]{32}"),
    ("shopify-shared-secret", r"shpss_[a-fA-F0-9]{32}"),
    # 私钥
    ("private-key", r"(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\s\S-]{64,}?"
                    r"-----END[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"),
]
_COMPILED = [(rid, re.compile(src)) for rid, src in RULES]
_WHOLE = {"private-key"}             # 这些不留任何字符

SPECIAL = {"aws": "AWS", "gcp": "GCP", "api": "API", "pat": "PAT", "ad": "AD", "tf": "TF", "oauth": "OAuth",
           "npm": "NPM", "pypi": "PyPI", "github": "GitHub", "gitlab": "GitLab", "openai": "OpenAI",
           "digitalocean": "DigitalOcean", "huggingface": "HuggingFace", "hashicorp": "HashiCorp",
           "sendgrid": "SendGrid"}


def label(rule_id: str) -> str:
    return " ".join(SPECIAL.get(p, p.capitalize()) for p in rule_id.split("-"))


@dataclass
class Hit:
    rule: str
    label: str


def _mask(lbl: str, value: str, whole: bool = False) -> str:
    if whole or len(value) < MIN_PARTIAL:
        return f"{PLACEHOLDER}{lbl}]"
    return f"{PLACEHOLDER}{lbl}: {value[:8]}…{value[-4:]}]"


class Redactor:
    def __init__(self, env: dict | None = None):
        env = os.environ if env is None else env
        # 已知的值：按长度从长到短，免得短的先替换掉长的一部分
        self.known = sorted(((v, k) for k, v in env.items() if SECRET_ENV.search(k) and len(v or "") >= MIN_KNOWN),
                            key=lambda kv: -len(kv[0]))

    def redact(self, text: str) -> tuple[str, list[Hit]]:
        hits: list[Hit] = []
        if not isinstance(text, str) or not text:
            return text, hits
        for value, name in self.known:
            if value in text:
                text = text.replace(value, f"{PLACEHOLDER}环境变量 {name}]")
                hits.append(Hit(f"env:{name}", f"环境变量 {name}"))
        for rid, rx in _COMPILED:
            def sub(m: re.Match, rid=rid) -> str:
                g = 1 if m.re.groups and m.group(1) is not None else 0
                secret = m.group(g)
                hits.append(Hit(rid, label(rid)))
                start, end = m.start(g) - m.start(), m.end(g) - m.start()
                whole = m.group(0)
                return whole[:start] + _mask(label(rid), secret, rid in _WHOLE) + whole[end:]
            text = rx.sub(sub, text)
        return text, hits

    def scan(self, text: str) -> list[Hit]:
        """只看有没有，不改内容（记忆写入前用）。按规则去重。"""
        seen, out = set(), []
        for h in self.redact(text)[1]:
            if h.rule not in seen:
                seen.add(h.rule)
                out.append(h)
        return out


def note(hits: list[Hit]) -> str:
    """附在工具结果末尾的说明。"""
    labels = list(dict.fromkeys(h.label for h in hits))
    return (f"\n[输出里有 {len(hits)} 处疑似密钥已脱敏：{'、'.join(labels)}。"
            "需要用到时请让用户自己处理，不要尝试还原或猜测原值。]")


_default: Redactor | None = None


def default() -> Redactor:
    """按当前进程环境变量建的脱敏器（第一次用时建，之后复用）。"""
    global _default
    if _default is None:
        _default = Redactor()
    return _default
