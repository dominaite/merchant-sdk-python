# Contributing

Thank you for helping with this SDK. This page lists the rules that a change must follow.

## Run the tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

CI runs the same commands. A pull request must pass all of them.

## Runtime floor

1. The SDK supports Python 3.9 and later. Do not use syntax or standard library features
   that are newer than 3.9.
2. The SDK has no runtime dependencies. Do not add one. Tools for tests and lint go in the
   `dev` extra in `pyproject.toml`.

## Test fixtures

1. `tests/merchant-api-contract.json` is the same file in every Dominaite SDK. Do not edit it
   in a pull request here. A change to it starts in the API.
2. `tests/merchant-api-wire-contract.json` is generated from the production API contract with
   the jq projection in `.github/workflows/contract-drift.yml`. Do not edit it by hand.
3. The signing vector and the webhook vector in `tests/` are shared by every Dominaite SDK. When
   a vector test fails, fix the code. Do not change the expected value.
4. Every change gets a test. Test what a caller gets back, not how the SDK works inside.

## Versioning

1. The SDK is on 0.x. A minor release (0.6 to 0.7) can contain a breaking change. A patch
   release (0.6.0 to 0.6.1) does not.
2. A change that a caller can see gets a line in `CHANGELOG.md`. A breaking change says so
   in its line.
3. Do not change the version in `pyproject.toml` in a pull request.

## Releases

The maintainer tags the releases. A tag starts the publish workflow. Do not create tags or
GitHub releases in a pull request.

## Security

Do not report a security problem in a public issue. See [SECURITY.md](SECURITY.md).
