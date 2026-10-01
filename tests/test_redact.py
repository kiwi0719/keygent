"""脱敏测试：36 条规则、不误报、已知的值、执行者接入、bash 存盘、写回保护、记忆。全部离线。
样例都是按格式拼出来的假值，不是真密钥。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from weaver.memory import scan as memory_scan
from weaver.models import FakeModel, reply
from weaver.policy import Policy
from weaver.redact import PLACEHOLDER, RULES, Redactor
from weaver.runner import Runner
from weaver.stores import MemoryBlobStore, MemoryEventStore
from weaver.tools import Tool, ToolBox, _spec, run_bash

a, b, h = "a" * 200, "B" * 200, "abcdef0123456789" * 8
SAMPLES = {
    "aws-access-token": "AKIAIOSFODNN7EXAMPLE",
    "gcp-api-key": "AIza" + "A" * 35,
    "azure-ad-client-secret": "abc8Q~" + a[:32],
    "digitalocean-pat": "dop_v1_" + h[:64],
    "digitalocean-access-token": "doo_v1_" + h[:64],
    "anthropic-api-key": "sk-ant-api03-" + a[:93] + "AA",
    "anthropic-admin-api-key": "sk-ant-admin01-" + a[:93] + "AA",
    "openai-api-key": "sk-" + a[:20] + "T3BlbkFJ" + a[:20],
    "huggingface-access-token": "hf_" + a[:34],
    "github-pat": "ghp_" + a[:32] + "WXYZ",
    "github-fine-grained-pat": "github_pat_" + a[:82],
    "github-app-token": "ghs_" + a[:36],
    "github-oauth": "gho_" + a[:36],
    "github-refresh-token": "ghr_" + a[:36],
    "gitlab-pat": "glpat-" + a[:20],
    "gitlab-deploy-token": "gldt-" + a[:20],
    "slack-bot-token": "xoxb-1234567890-1234567890-abcdefgh",
    "slack-user-token": "xoxp-1234567890-1234567890-1234567890-" + a[:30],
    "slack-app-token": "xapp-1-A1B2C3-1234-abcdef",
    "twilio-api-key": "SK" + h[:32],
    "sendgrid-api-token": "SG." + a[:66],
    "npm-access-token": "npm_" + a[:36],
    "pypi-upload-token": "pypi-AgEIcHlwaS5vcmc" + a[:60],
    "databricks-api-token": "dapi" + h[:32],
    "hashicorp-tf-api-token": a[:14] + ".atlasv1." + a[:65],
    "pulumi-api-token": "pul-" + h[:40],
    "postman-api-token": "PMAK-" + h[:24] + "-" + h[:34],
    "grafana-api-key": "eyJrIjoi" + b[:80],
    "grafana-cloud-api-token": "glc_" + b[:40],
    "grafana-service-account-token": "glsa_" + b[:32] + "_abcdef12",
    "sentry-user-token": "sntryu_" + h[:64],
    "sentry-org-token": "sntrys_eyJpYXQiO" + a[:20] + "LCJyZWdpb25fdXJs" + a[:20] + "_" + a[:43],
    "stripe-access-token": "sk_live_" + a[:24],
    "shopify-access-token": "shpat_" + h[:32],
    "shopify-shared-secret": "shpss_" + h[:32],
    "private-key": "-----BEGIN RSA PRIVATE KEY-----\n" + b[:80] + "\n-----END RSA PRIVATE KEY-----",
}


class Rules(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(env={})

    def test_every_rule_has_a_sample_and_fires(self):
        self.assertEqual(len(RULES), 36)
        self.assertEqual(set(SAMPLES), {rid for rid, _ in RULES})
        for rid, secret in SAMPLES.items():
            with self.subTest(rid):
                out, hits = self.r.redact(f"config = '{secret}' # end")
                self.assertNotIn(secret, out)
                self.assertIn(PLACEHOLDER, out)
                self.assertIn(rid, [x.rule for x in hits])
                self.assertTrue(out.startswith("config = '") and out.endswith("' # end"))   # 周围原样

    def test_mask_format(self):
        out, _ = self.r.redact("token: " + SAMPLES["github-pat"])
        self.assertEqual(out, "token: [已脱敏 GitHub PAT: ghp_aaaa…WXYZ]")
        out, _ = self.r.redact(SAMPLES["private-key"])
        self.assertEqual(out, "[已脱敏 Private Key]")                    # 私钥不留任何字符

    def test_no_false_positives(self):
        normal = [
            "def main():\n    return compute(token_budget=200_000)",
            "token 预算设成 200k，password 字段在表单里",
            "uuid: 550e8400-e29b-41d4-a716-446655440000",
            "commit e83c5163316f89bfbde7d9ab23ca2e25604af290",
            "sha256 " + "0123456789abcdef" * 4,
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
            "SKU-12345 和 sk-learn 的用法",
            "-----BEGIN PUBLIC KEY-----\n" + b[:80] + "\n-----END PUBLIC KEY-----",
        ]
        for text in normal:
            with self.subTest(text[:30]):
                self.assertEqual(self.r.redact(text), (text, []))


class Known(unittest.TestCase):
    def test_env_values(self):
        key = "sk-or-v1-" + "f" * 48                                     # OpenRouter 的 key 不在规则里
        r = Redactor(env={"OPENROUTER_API_KEY": key, "SHORT_TOKEN": "abc", "HOME": "/Users/someone/long/path"})
        out, hits = r.redact(f"Authorization: Bearer {key}\nhome=/Users/someone/long/path")
        self.assertEqual(out, "Authorization: Bearer [已脱敏 环境变量 OPENROUTER_API_KEY]\nhome=/Users/someone/long/path")
        self.assertEqual([x.rule for x in hits], ["env:OPENROUTER_API_KEY"])


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_runner_redacts_tool_results_not_user_input(self):
        secret = SAMPLES["github-pat"]
        box = ToolBox(tools={"leak": Tool(_spec("leak", "", {}, []), lambda: f"GITHUB_TOKEN={secret}")})
        store, model = MemoryEventStore(), FakeModel([reply(calls=[("c1", "leak", {})]), reply("好")])
        r = Runner("s", store, model, box, Policy(), "SYS", redactor=Redactor(env={}))
        r.submit(f"用这个 token：{secret}")                                 # 用户自己给的，不脱敏
        r.run()
        result = [e for e in store.load("s") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
        self.assertNotIn(secret, result["output"])
        self.assertIn("1 处疑似密钥已脱敏：GitHub PAT", result["output"])
        sent = str(model.calls[1]["messages"])
        self.assertEqual(sent.count(secret), 1)                              # 只剩用户原话里那一处
        self.assertIn("[已脱敏 GitHub PAT", sent)

    def test_model_error_text_redacted(self):
        secret = SAMPLES["anthropic-api-key"]

        def boom(_):
            raise RuntimeError(f"HTTP 401: bad key {secret}")
        store = MemoryEventStore()
        r = Runner("s", store, FakeModel([boom]), ToolBox(tools={}), Policy(), "SYS", redactor=Redactor(env={}))
        r.submit("hi")
        r.run()
        self.assertNotIn(secret, str(store.load("s")))

    def test_bash_saved_output_redacted(self):
        secret = SAMPLES["github-pat"]
        out = run_bash(f"for i in $(seq 1 2000); do echo 'line {secret} padding padding'; done", self.root,
                       outputs_dir=self.root / "out")
        saved = next((self.root / "out").glob("*.log")).read_text()
        self.assertNotIn(secret, saved)
        self.assertIn("[已脱敏 GitHub PAT", saved)

    def test_write_back_guard(self):
        (self.root / "cfg.txt").write_text("TOKEN=real\n")
        box = ToolBox(self.root).add_write_tools("s", MemoryBlobStore(), None)
        box.execute("read_file", {"path": "cfg.txt"})
        out, err = box.execute("write_file", {"path": "cfg.txt", "content": "TOKEN=[已脱敏 GitHub PAT: ghp_aaaa…WXYZ]\n"})
        self.assertTrue(err)
        self.assertIn("脱敏占位符", out)
        out, err = box.execute("edit_file", {"path": "cfg.txt", "old_string": "real",
                                             "new_string": "[已脱敏 环境变量 X]"})
        self.assertTrue(err)
        self.assertEqual((self.root / "cfg.txt").read_text(), "TOKEN=real\n")


class Memory(unittest.TestCase):
    def test_unified_rules_and_strict_extras(self):
        self.assertIn("GitHub PAT", memory_scan("我的 token 是 " + SAMPLES["github-pat"]))
        self.assertIsNotNone(memory_scan("key sk-abcdefghijklmnopqrstuvwx"))        # 记忆里保留更严的兜底
        self.assertIsNone(memory_scan("用户喜欢简短回答"))


if __name__ == "__main__":
    unittest.main()
