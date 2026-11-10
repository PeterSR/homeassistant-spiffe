# Contributing

Thanks for considering a contribution. This integration aims to stay small and to work with any
SPIFFE implementation. Contributions that keep it that way are the most welcome kind: nothing in
the integration should depend on one particular agent or issuer.

## Repository layout

| Path | What |
|---|---|
| `custom_components/spiffe/source.py` | the only code that talks to py-spiffe, behind a small `SpiffeSource` interface |
| `custom_components/spiffe/coordinator.py` | the scheduled health check, the cached bundles and the repair issues |
| `custom_components/spiffe/__init__.py` | setup, unload and the helpers other integrations call |
| `custom_components/spiffe/config_flow.py` | asking for the Workload API address and checking it |
| `tests/` | the tests; `test_e2e_spiffex.py` runs against a real agent |

## Development

You need [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync --group test
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

To run the end-to-end tests too, point `SPIFFEX` at a
[spiffex](https://github.com/PeterSR/spiffex) binary. Tests against other implementations are
welcome.

## Pull requests

- Keep each pull request to one change, with tests.
- Add a line to the *Unreleased* section of [CHANGELOG.md](CHANGELOG.md) for anything a user would
  notice.
- User-facing text lives in `strings.json`; copy it to `translations/en.json`.
- Never log, store or expose a token, not even in a test's failure message.

## Releases

Bump `version` in `custom_components/spiffe/manifest.json` and `pyproject.toml`, move the
*Unreleased* changelog entries under the new version, tag `vX.Y.Z`, and publish a GitHub release
for the tag. HACS offers GitHub releases.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE),
and you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).
