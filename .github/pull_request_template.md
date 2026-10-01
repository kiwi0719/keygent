<!-- One change per PR. See CONTRIBUTING.md. -->

## What and why



Closes #

## Checklist

- [ ] `make check` is green (ruff + offline tests)
- [ ] `make test-linux` is green, if the sandbox, `bash` or ripgrep lookup changed
- [ ] The app builds and the changed flow was tried (`make run`), if `Keygent/`
      or the daemon API changed; `design/api.md` and `Keygent/Keygent/API/`
      updated in this PR
- [ ] The kernel still does no IO, and no state lives outside the ledger
- [ ] The prompt-cache prefix is unchanged within a session, if prompts, tools
      or background inputs changed
- [ ] No API keys, real ledgers or private code in code, fixtures or logs
- [ ] The progress section of the matching `design/*.md` is updated, if
      behaviour changed
- [ ] `CHANGELOG.md` has a line under Unreleased

## Verification

<!-- what you ran and what you saw; for model-facing changes, the provider/model and the relevant ledger lines -->
