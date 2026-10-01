# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-02

First public release.

### Added

- **Keygent** (macOS 14+, Apple silicon): menu-bar launcher (<kbd>⌥ Space</kbd>)
  with working folder and attachments, status capsule, task window with live
  steps, a queue of everything waiting for you (approve / deny / edit and
  approve / answer), step timeline, and keyboard-only settings for model, MCP
  and skills. The app bundle embeds Python 3.14 and the MCP SDK and registers
  `weaverd` as a login item; it restarts the daemon when the bundled code
  changes.
- **weaverd**: background service with one ledger and one worker per task,
  localhost HTTP + SSE API with a token ([design/api.md](design/api.md)),
  human-readable step translation, system notifications when the app is
  closed, shared MCP connections across tasks, and `weaver daemon
  install / start / stop / status / logs` for running it under launchd
  without the app.
- **Weaver kernel**: append-only event ledger with a pure `fold` / `decide`
  kernel; crash recovery by replaying the ledger.
- **Models**: OpenAI-compatible chat and Anthropic Messages protocols with
  streaming, prompt caching with cache-break warnings, and presets for 15
  providers including local Ollama, LM Studio and vLLM.
- **Context**: two-stage compaction (trim, then summarise; the ledger keeps the
  originals), instruction files and two-level memory with automatic extraction,
  environment info, `recall` over a task's ledger and search across tasks.
- **Tools**: `read_file`, `grep`, `find_files` (bundled ripgrep 15.2.0),
  `write_file`, `edit_file`, `bash` (with background jobs), `todo`, `ask_user`,
  parallel tool batches, and sub-agents (`explore`, `general`, custom types,
  background and forked).
- **Safety**: permission rules (allow inside the working folder, ask outside,
  hard-deny a short list), OS sandbox for `bash` (macOS `sandbox-exec`, Linux
  bubblewrap), undo for every file change, secret redaction before the ledger,
  loop and budget guards.
- **Extensions**: MCP servers (stdio and HTTP, OAuth login, deferred tool lists,
  project-level trust) and skills, including ten built-in skills adapted from
  obra/superpowers.

### Fixed

- MCP connection errors on Python 3.10 raised `NameError`
  (`BaseExceptionGroup` is built in only from 3.11).

[Unreleased]: https://github.com/kiwi0719/keygent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kiwi0719/keygent/releases/tag/v0.1.0
