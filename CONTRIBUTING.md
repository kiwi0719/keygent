# Contributing

## Ground rules

- **The ledger is the only source of truth.** Task state is what `fold` derives from `ledger.jsonl`; anything kept elsewhere (in memory, in `meta.json`, in the app) is a cache that can be rebuilt from it. A change that needs state the ledger cannot reproduce needs a design note first.
- **The kernel does no IO.** `weaver/kernel.py` (`fold`, `decide`, `billable`) is pure. Model calls, tools, files and clocks belong to the runner and the daemon. The [kernel invariants](design/kernel.md) are settled unless a PR argues otherwise.
- **Core is standard library only.** The one optional dependency is the MCP SDK (`requirements-mcp.txt`), imported only inside `weaver/mcp/` and only after `sdk_available()`. Python 3.10 is the floor.
- **Don't break the prompt-cache prefix.** Within a session, nothing before the last cache breakpoint may change between turns ([design/cache.md](design/cache.md)). New background inputs go after it.
- **Safety layers stay layered.** Permissions, the OS sandbox, undo and redaction each assume the others can fail. A PR must not make one rely on another; changes to `permissions.py`, `sandbox.py`, `redact.py` or `guards.py` get the `area:safety` label and extra review.
- **Tests are offline.** No network, no API keys, no real models: use the fake models and SSE streams in `tests/`. Never commit a `.env`, a ledger or a `.weaver/` directory from a real session; they hold your code and possibly secrets.
- **The API has one spec.** `weaverd`'s HTTP + SSE interface is [design/api.md](design/api.md). A change to it updates that file, the daemon and `Keygent/Keygent/API/` in the same PR.

## Workflow

1. Open an issue, or pick one labelled `good first issue` / `help wanted`. For anything larger than a fix, say what you plan first.
2. Branch from `main`. One change per PR.
3. Lint and run the offline suite (needs Python 3.10+ and [ruff](https://docs.astral.sh/ruff/)):

   ```bash
   make check
   ```

   The MCP tests skip without the SDK. To run them too:

   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements-mcp.txt
   make check PYTHON=.venv/bin/python
   ```

4. If you touched the sandbox, `bash`, or ripgrep lookup, run the Linux suite. It runs the tests in `python:3.12-slim` on arm64 and amd64 with bubblewrap, so it needs Docker and nothing else:

   ```bash
   make test-linux
   ```

5. If you touched `Keygent/` or the daemon API, build the app (needs Xcode and `brew install xcodegen`) and try the flow you changed:

   ```bash
   make run
   ```

   A Debug build started with `KEYGENT_DEBUG_HOOKS=1` listens for the `com.keygent.debug` distributed notification to switch screens and dump state, which helps when testing with simulated key presses (`Keygent/Keygent/Debug/DebugHooks.swift`).

6. If behaviour changed, update the progress section at the end of the matching `design/*.md`.
7. Add a line to `CHANGELOG.md` under Unreleased.

## Talking to a real model

Some changes (a new provider preset, a dialect quirk, caching or compaction) can only be checked against a real endpoint. Put `WEAVER_BASE_URL`, `WEAVER_API_KEY` and `WEAVER_MODEL` in a gitignored `.env` at the repo root and run one prompt through the CLI:

```bash
python3 -m weaver --local "list the files here and summarise README.md"
```

The terminal prints token usage and cached tokens after each call and warns when the cache prefix broke; `python3 -m weaver -s <id> --log` prints the ledger. Paste the relevant lines (not the key) into the PR.

## Built-in skills

`weaver/builtin_skills/` is a copy of [obra/superpowers](https://github.com/obra/superpowers) with the edits listed in [NOTICE.md](weaver/builtin_skills/NOTICE.md). To sync upstream, copy the directories again and redo each edit; `tests/test_builtin_skills.py` fails if one is missing. Don't edit these files for any other reason; write a new skill instead.

## Labels

| Label | Meaning |
|---|---|
| `bug` | behaviour differs from the design docs |
| `model-compat` | a provider or model behaves differently from what Weaver expects |
| `area:safety` | permissions, sandbox, undo, redaction, loop guards; reviewed with extra care |
| `area:kernel` / `area:provider` / `area:tools` / `area:mcp` / `area:skills` / `area:daemon` / `area:app` | area |
| `design` | needs a design note or changes a recorded decision |
| `good first issue` / `help wanted` | open for contributors |
