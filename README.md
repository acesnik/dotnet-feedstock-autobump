# dotnet-feedstock-autobump

Watches Microsoft's .NET release metadata and opens version-bump PRs against
[conda-forge/dotnet-feedstock](https://github.com/conda-forge/dotnet-feedstock).

Addresses [#55](https://github.com/conda-forge/dotnet-feedstock/issues/55)
(auto-bump doesn't recognise the download links) and
[#90](https://github.com/conda-forge/dotnet-feedstock/issues/90) (script for
getting SHAs).

## Why a separate repo

Two reasons, and the first is not negotiable.

**A feedstock can't host it.** A conda-forge feedstock's `.gitignore` is:

```
# User content belongs under recipe/.
# Everything else is managed by the conda-smithy rerender process.
# Please do not modify
*
!/conda-forge.yml
!/recipe/**
!/.ci_support/**
```

Every root file is ignored. A helper script or a custom workflow simply cannot
live at a feedstock root — that space belongs to `conda smithy rerender`.

**It doesn't need to.** The only artifact a PR requires is the `meta.yaml` diff.
So the automation checks out the feedstock, edits one file, and opens the PR from
a fork. Nothing about the tooling has to ship inside the package.

## Why the generic autotick bot can't do this

conda-forge's version bot handles most feedstocks. It can't handle this one, for
two structural reasons rather than anything to do with Microsoft's download page:

1. **Two independent versions.** `sdk_version` and `runtime_version` are not the
   same number and drift apart within a release line — `10.0.302` ships with
   runtime `10.0.10`. The bot has no concept of a second version.
2. **Five `sha256` values behind selectors.** The bot updates `sha256:` under
   `source:`; it has no path into
   `{% set sha256 = "..." %}  # [linux and aarch64]`.

Even a bot that detected the new version could not correctly update this recipe.
Microsoft *does* publish everything needed as JSON, including the sdk/runtime
pairing, so a purpose-built updater is straightforward where a generic one isn't.

## How it works

```
releases-index.json  ──probe──►  versions only, 2 HTTP GETs
                                      │
                        up to date ◄──┴──► new release
                             │                  │
                            exit           stream + hash 5 artifacts (~1 GB)
                                                │
                                         rewrite meta.yaml
                                                │
                                    push autobump/<sdk> to the fork
                                                │
                                       open PR upstream
                                                │
                                 comment "@conda-forge-admin, please rerender"
```

**The probe matters.** A weekly cron that checks with `--check` would download
~1 GB *every run* just to discover nothing changed, because computing a SHA-256
requires the bytes. `--probe` compares versions only — two HTTP requests, zero
downloads — and exits `10` when a bump exists, `0` when current. Hashing happens
only after the probe says there's something to hash.

**One branch per SDK version** (`autobump/10.0.302`). Re-runs are idempotent, and
it never force-pushes over a branch a human is working on. Manual `v<N>update`
branches are untouched.

**The fork is re-synced from upstream** before each bump. Basing on a stale fork
would produce a PR full of unrelated reverts.

**Rerender is requested as a comment, not in the PR body.** conda-forge's docs
describe the trigger as a comment on the PR (rerenders the head branch, pushes a
commit) or an issue title/comment (opens a separate PR). The PR description is not
a documented trigger. Note this means a rerender's diff lands without human
review, which can be large if the feedstock's rendering is stale — drop that step
if you'd rather inspect first.

## Setup

**1. Secret.** Add `FEEDSTOCK_TOKEN`, a PAT that can:

- push branches to `acesnik/dotnet-feedstock` (`contents: write`)
- open pull requests on `conda-forge/dotnet-feedstock`

`GITHUB_TOKEN` cannot do either — it's scoped to this repo. Opening a PR against
a repo you don't administer generally needs a classic PAT with `public_repo`
(acting as you, a listed `recipe-maintainer`), since fine-grained PATs can't
grant `pull_requests: write` on someone else's repo. Use the narrowest thing that
works and set an expiry.

**2. Check the channel matrix** in `.github/workflows/check-releases.yml`.
Currently `["10.0"]`. Adding `"11.0"` while it is preview would open PRs bumping
the package to a preview build — wait until its `support-phase` is `active`.

**3. Test with a dry run** before trusting the schedule:

```
gh workflow run check-releases.yml -f channel=10.0 -f dry_run=true
```

`dry_run` defaults to `true`, so a manual run never opens a PR unless you ask.
The scheduled run does (`inputs.dry_run` is empty on a schedule, and the guard is
`!= true`).

## Using the updater by hand

Works standalone, no CI needed. Run it from a feedstock checkout:

```bash
cd path/to/dotnet-feedstock

# what's the latest, and is the recipe stale? (no downloads)
../autobump/scripts/update-dotnet-version.py --channel 10.0 --probe

# resolve URLs without downloading
../autobump/scripts/update-dotnet-version.py --channel 10.0 --dry-run

# hash and print the meta.yaml block
../autobump/scripts/update-dotnet-version.py --channel 10.0

# ...and rewrite meta.yaml
../autobump/scripts/update-dotnet-version.py --channel 10.0 --write

# pin an exact SDK rather than the channel's latest
../autobump/scripts/update-dotnet-version.py --sdk-version 10.0.302 --write
```

Exit codes: `0` success / up to date, `10` update available (`--probe` only),
`1` error.

Standard library only, deliberately — it runs in a bare container with no
`pip install` step.

## Known gaps

- **`--write` does not rerender.** It can't; `conda-smithy` isn't a dependency
  here. The workflow delegates that to conda-forge's bot.
- **Platform list is hardcoded** in `PLATFORMS` in the updater. If the recipe
  gains platforms — `win_arm64` and `linux_armv7l` are both shipped by Microsoft
  and tracked in
  [#115](https://github.com/conda-forge/dotnet-feedstock/issues/115) — that list
  and the recipe's selectors have to change together, and the existing `# [win]`
  selector needs narrowing to `# [win and x86_64]` so it stops matching arm64.
- **No handling of SDK feature bands.** A release can ship several SDKs
  (`10.0.1xx`, `10.0.2xx`); this takes the release's primary `sdk` unless you pass
  `--sdk-version`.
- **Single fork hardcoded** (`acesnik/dotnet-feedstock`) in the workflow.
