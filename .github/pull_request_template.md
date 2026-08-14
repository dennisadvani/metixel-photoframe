## Description

<!-- Describe the change and why you're making it. Link any related issues. -->

Closes #<!-- issue number -->

## Type of change

<!-- Tick the relevant box(es). -->

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / code quality
- [ ] Documentation
- [ ] CI / tooling
- [ ] Other

## Target branch

<!-- All PRs target `dev`. `main` is release-only — it is updated solely by the
     release process (`scripts/release.ps1`), never directly. -->

- [ ] Base branch is `dev` (not `main`)

## Code quality (also enforced by CI)

<!-- Optional pre-flight — CI runs these on every PR and is the source of truth.
     Run locally only if you want faster feedback. See `CONTRIBUTING.md`. -->

- [ ] `ruff check src/metixel/` passes
- [ ] `ruff format --check src/metixel/` passes
- [ ] `mypy src/metixel/` passes (no errors)
- [ ] `pytest tests/ -q --no-cov` passes

## Required (not covered by CI)

<!-- CI cannot verify these — the author must confirm them. -->

- [ ] Web UI change? `cd web-tests && npx playwright test` passes against a live frame
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] Docs updated if user-facing behaviour, URLs, or the API changed

## Notes for reviewers

<!-- Anything the reviewer should know: design decisions, trade-offs, testing performed. -->
