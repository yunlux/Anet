# GitHub release checklist

## Before the first push

- [ ] Configure the final GitHub owner/repository remote.
- [ ] Confirm the public branch contains only sanitized current content.
- [ ] Scan every reachable Git blob for credentials, private key material,
      complete Node IDs, user paths, private addresses, and deployment names.
- [ ] Rewrite unpublished local history if it contains private deployment data,
      then scan the rewritten history again.
- [ ] Confirm `git status` is clean and no unexpected refs or tags are present.

## Repository settings

- [ ] Set the default branch to `main`.
- [ ] Enable branch protection or a ruleset requiring Core CI.
- [ ] Enable secret scanning, push protection, Dependabot alerts, and private
      vulnerability reporting.
- [ ] Restrict GitHub Actions to approved actions and require full commit SHAs
      where the owner policy supports it.
- [ ] Review OpenWiki secrets and keep environment approvals scoped.

## Release candidate

```bash
uv sync --locked --all-extras
uv run ruff check src tests scripts
uv run pytest -q
SOURCE_DATE_EPOCH=1754000000 uv run python -m build
uv run python -m twine check dist/*
```

- [ ] Inspect wheel and sdist member lists.
- [ ] Confirm neither archive contains caches, local config, evidence, identity
      state, or Ahub server code on a core-only installation path.
- [ ] Install the wheel in a clean environment and verify `anet --version`.
- [ ] Test the self-contained Linux Skill in a fresh HOME.
- [ ] Generate `SHA256SUMS` from artifacts built from the exact release tag.
- [ ] Create an immutable GitHub release and retain build provenance when the
      repository owner enables those features.

Do not publish raw node or network evidence. Add only sanitized summaries to
`VERIFICATION.md`.
