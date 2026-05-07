# PyPI Publishing Setup for oracle3

This document walks through the one-time setup needed before
`oracle3` can be auto-published to PyPI when you cut a GitHub release.
Trusted publishing (OIDC) is used, so **no PyPI API token is stored
in this repo or in GitHub secrets** — the workflow authenticates via
short-lived tokens issued by GitHub OIDC.

Reference: <https://docs.pypi.org/trusted-publishers/>

---

## Prerequisites

- The PyPI project name `oracle3` is currently **available**
  (verified on 2026-05-06 via `https://pypi.org/simple/oracle3/` → 404).
- Repo: <https://github.com/YichengYang-Ethan/oracle3>
- Workflow: `.github/workflows/pypi-publish.yml`

---

## Step 1 — Create your PyPI account (skip if you already have one)

1. Go to <https://pypi.org/account/register/>.
2. Register with `ethanyang85@outlook.com`.
3. Verify the email.

## Step 2 — Enable two-factor authentication (REQUIRED by PyPI)

1. Visit <https://pypi.org/manage/account/two-factor/>.
2. Enable **TOTP** (1Password, Authy, Google Authenticator, etc.).
3. Save the recovery codes to your password manager.

> PyPI requires 2FA for all account actions on new projects since 2024.
> You will not be able to register the project name without it.

## Step 3 — Configure Trusted Publishing for `oracle3`

Because the project does not yet exist on PyPI, you need to use the
"pending publisher" flow (the publisher is created first, then PyPI
will create the project on the first successful publish).

1. Open <https://pypi.org/manage/account/publishing/>.
2. Scroll to **"Add a new pending publisher"**.
3. Fill in **exactly** these values:

   | Field             | Value                              |
   | ----------------- | ---------------------------------- |
   | PyPI project name | `oracle3`                          |
   | Owner             | `YichengYang-Ethan`                |
   | Repository name   | `oracle3`                          |
   | Workflow filename | `pypi-publish.yml`                 |
   | Environment name  | `pypi`                             |

4. Click **"Add"**.

## Step 4 — Create the matching `pypi` GitHub environment

1. Open <https://github.com/YichengYang-Ethan/oracle3/settings/environments>.
2. Click **"New environment"** and name it **`pypi`** (case-sensitive,
   must match the workflow file).
3. (Optional but recommended)
   - Enable **"Required reviewers"** and add yourself —
     this gates publishes behind a manual approval click.
   - Restrict deployment branches to `main` and tags `v*`.
4. No secrets are needed in the environment — OIDC handles auth.

## Step 5 — Cut a release

After steps 1–4 are complete:

```bash
git checkout main
git pull
# bump the version (commitizen keeps pyproject.toml + oracle3/__init__.py in sync)
poetry run cz bump --increment patch  # or minor/major
git push --follow-tags
gh release create v1.1.2 --generate-notes
```

The `Publish to PyPI` workflow will run automatically when the release
is published. After ~2 minutes, the package will appear at
<https://pypi.org/project/oracle3/>.

## Step 6 — Verify

```bash
pip install --upgrade oracle3
python -c "import oracle3; print(oracle3.__version__)"
oracle3 --help
```

---

## Troubleshooting

- **"trusted publisher not found"** — the publisher in Step 3 must
  match the workflow file name and environment name **exactly**. If
  you renamed either, update the publisher on pypi.org.
- **"version already exists"** — PyPI rejects re-uploads of the same
  version. Bump the version and cut a new release.
- **"environment protection rules blocked deployment"** — if you
  enabled required reviewers, you must approve the workflow run on
  GitHub before the publish step proceeds.
- **Manual one-off publish** — the workflow also supports
  `workflow_dispatch`, so you can trigger it from the Actions tab
  without cutting a release (useful for testing).

---

## What's already done in this repo

- [x] `pyproject.toml` has all required PyPI metadata
  (name, version, description, authors, license, readme, repository,
  homepage, classifiers, keywords).
- [x] `oracle3/__init__.py` exposes `__version__ = "1.1.1"` (kept in
  sync via `commitizen` `version_files`).
- [x] LICENSE (Apache-2.0) is bundled in the wheel.
- [x] README.md is rendered as the long description on PyPI.
- [x] `.github/workflows/pypi-publish.yml` is set up to publish on
  release via OIDC trusted publishing.
- [x] Local build verified: `poetry build` produces a valid
  `oracle3-1.1.1-py3-none-any.whl` and `oracle3-1.1.1.tar.gz`.
- [x] Wheel scanned with gitleaks — no secrets bundled.

Only the PyPI-side configuration in steps 1–4 above is still required.
