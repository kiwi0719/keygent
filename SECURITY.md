# Security policy

## Supported versions

Only the latest minor release gets security fixes. Fixes land on `main` and
ship in the next patch release; there are no backports to older lines.

## Reporting a vulnerability

Report privately through GitHub:
[**Report a vulnerability**](https://github.com/kiwi0719/keygent/security/advisories/new).
Do not open a public issue, pull request or discussion for it.

Include the version or commit, macOS or Linux version, the model provider if it
matters, and the smallest prompt, project layout or request that reproduces it.
Never include real API keys, ledgers from real sessions or private code.

You should get a first reply within 7 days. Once a fix is ready it is released,
the advisory is published, and you are credited unless you ask not to be.

## What counts

Weaver runs a model's tool calls on your machine. A vulnerability is anything
that lets those calls, a project's files, or another local process get past the
safety layers:

- `bash` writing outside the working folder and temp, or reaching something the
  sandbox profile is meant to deny (sandbox escape on macOS `sandbox-exec` or
  Linux bubblewrap)
- a write, edit or command outside the working folder that runs without being
  asked about, or a hard-denied action that runs at all (permission bypass),
  including through a sub-agent, an MCP tool or "edit and approve"
- a project's `.weaver/` config, skills, agent types or MCP servers taking
  effect before the project is trusted
- secrets reaching the ledger, the model, the daemon log or the app unredacted
  when a redaction rule or a known environment value covers them
- the `weaverd` API answering without its token, listening beyond 127.0.0.1,
  or leaking the token (for example through logs or task output)
- path traversal or symlink tricks in uploads, blobs, `read_file` or undo that
  read or overwrite files outside where they should
- a file change that `--undo` cannot restore

What does not count: the model choosing a bad action that the permission rules
then ask you about, or a prompt injection that only changes what the model
says. Those are the system working as designed; open a public issue if a rule
is too loose. A prompt injection that gets an action past the approval it should
need is in scope; report that privately.
