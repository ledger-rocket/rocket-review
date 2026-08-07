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
4. Within a day, [`ledger-rocket/homebrew-tap`](https://github.com/ledger-rocket/homebrew-tap)
   notices the new version on PyPI, rewrites the formula's `url` and `sha256`, and
   pushes a `bump/rocket-review-<version>` branch. **It stops there.** Opening the
   PR from a workflow would need *Allow GitHub Actions to create and approve pull
   requests*, a single toggle that also grants approval — too much standing
   authority for a tap, where a merged formula runs code on users' machines.

   The run's summary carries a compare link. Open the PR from it, let CI run —
   a human-opened PR starts its own checks, which one opened by `GITHUB_TOKEN`
   would not — and merge. Until it merges, `brew install` serves the previous
   version.

The bump lives in the tap rather than here on purpose: this repo's CI holds no
secrets, and pushing to another repository would mean storing a cross-repo token.
The sdist's sha256 does not exist until step 3 finishes either.
