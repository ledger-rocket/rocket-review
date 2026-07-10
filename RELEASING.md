# Releasing

rocket-review publishes to PyPI through GitHub's [trusted publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) — no API tokens are stored anywhere.

## One-time setup (before the first release)

1. On PyPI, logged in as the project owner, add a **pending publisher** at
   <https://pypi.org/manage/account/publishing/>:
   - PyPI project name: `rocket-review`
   - Owner: `ledger-rocket` — Repository: `rocket-review`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. In this repo, create the `pypi` environment (Settings → Environments → New).
   Optionally add yourself as a required reviewer there — publishes then wait
   for a click, which is a nice guard against accidental releases.

## Every release

1. Bump `version` in `pyproject.toml` via a normal PR.
2. Create a GitHub release with tag `v<version>` (e.g. `v0.1.0`) targeting `main`.
3. `publish.yml` builds the sdist + wheel and uploads to PyPI. It fails fast if
   the tag and `pyproject.toml` version disagree.
