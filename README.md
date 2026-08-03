# dotnet-feedstock-autobump

Watches Microsoft's .NET release metadata and acts on what it finds, for
[conda-forge/dotnet-feedstock](https://github.com/conda-forge/dotnet-feedstock).

Addresses [#55](https://github.com/conda-forge/dotnet-feedstock/issues/55)
(auto-bump doesn't recognise the download links) and
[#90](https://github.com/conda-forge/dotnet-feedstock/issues/90) (script for
getting SHAs).

## The organising principle

**Mechanical changes become PRs. Judgment calls become issues.**

A patch bump inside the tracked release line is mechanical — two version
variables and five hashes, all derivable from published metadata. That gets a PR,
unattended.

Everything else gets escalated to a human, because the bot has no basis for
deciding:

| Situation | Action |
|---|---|
| New SDK in the tracked channel | **PR** |
| A new release line goes active (.NET 11, 12, …) | **issue** |
| The tracked line reaches end of life | **issue** |
| Microsoft starts publishing a new architecture | **issue** |
| Microsoft *drops* an architecture we package | **issue** (impending breakage) |
| Nothing changed | one line in the run summary |

The thing it will never do is go quiet. An earlier version of this workflow
hardcoded `matrix: channel: ["10.0"]`, which meant .NET 11 and 12 could ship
while it kept reporting success — blindness that looks like health. Now the
channel list is discovered every run and compared against declared policy.

## Why the generic autotick bot can't do this

conda-forge's version bot handles most feedstocks. It can't handle this one, for
two structural reasons rather than anything to do with Microsoft's download page:

1. **Two independent versions.** `sdk_version` and `runtime_version` are not the
   same number and drift apart within a release line — `10.0.302` ships with
   runtime `10.0.10`. The bot has no concept of a second version.
2. **Six `sha256` values behind selectors.** The bot updates `sha256:` under
   `source:`; it has no path into
   `{% set sha256 = "..." %}  # [linux and aarch64]`.

## Why a separate repo

A conda-forge feedstock's `.gitignore` is:

```
# User content belongs under recipe/.
# Everything else is managed by the conda-smithy rerender process.
# Please do not modify
*
!/conda-forge.yml
!/recipe/**
!/.ci_support/**
```

Every root file is ignored — a helper script or custom workflow cannot live at a
feedstock root. It doesn't need to: the only artifact a PR requires is the
`meta.yaml` diff.

## Declared policy: `channels.json`

Human-owned; the automation reads it and never edits it.

```jsonc
{
  "track": "10.0",              // the line the feedstock currently ships
  "policy": "lts",              // lts | latest | manual
  "issue_repo": "acesnik/dotnet-feedstock-autobump",
  "rid_map":        { "win-arm64": "win-arm64", ... },   // MS RID -> cf subdir
  "ignore_rids":    ["linux-musl-x64", ...],             // no cf equivalent
  "active_subdirs": ["linux-64", "win-arm64", ...],      // cf actually builds these
  "min_repodata_bytes": 102400
}
```

`policy` exists because a conda-forge feedstock publishes **one** `dotnet`
package, so adopting a new line means abandoning the current one. Whether a
scientific packaging channel should follow STS (18 months) or stay on LTS (3
years) is not a question metadata can answer. Under `lts`, a new STS line is
noted but raises nothing; a new LTS line raises an issue.

The set of RIDs the recipe *packages* is deliberately **not** listed here — it's
read from `PLATFORMS` in `update-dotnet-version.py`, so the audit cannot drift
out of sync with what the recipe actually does.

### Two gates on architecture viability, and why both are needed

A conda-forge subdir existing is not the same as it being usable.

`min_repodata_bytes` catches empty subdirs. `linux-armv7l` is real but its
`repodata.json` is 1,306 bytes — three packages, no `icu`/`openssl`/`zlib` — so a
`dotnet` built there would install and be unusable. This is why
[#115](https://github.com/conda-forge/dotnet-feedstock/issues/115) got `win_arm64`
and not `linux_armv7l`.

`active_subdirs` catches the opposite failure, which size alone misses. `win-32`
has a **10.4 MB** repodata and 24,464 packages — it sails past any size threshold
— but conda-forge stopped building it in 2019 (newest package there is dated
`2019-02-20`). Size distinguishes "has an ecosystem" from "empty", not "thriving"
from "frozen". Detecting that from package timestamps would mean downloading
repodata (432 MB for `linux-64`), which CI can't do, so the active list is
declared instead.

Keep `active_subdirs` in sync with what conda-forge actually builds.

## How a run goes

```
plan job  ── list channels (1 GET) ─┐
          ── read recipe versions ──┤── plan.json ──┬── bump job    (PR, ~1 GB)
          ── list RIDs for channel ─┤               └── notify job  (issues)
          ── HEAD subdir repodata ──┘
```

The **plan** job is cheap: a couple of JSON fetches and some HEAD requests. It
needs no token, because the feedstock is public and planning only reads.

The **bump** job runs only when the plan found a version change, and that gate is
the point — computing a SHA-256 requires the bytes, so an unconditional check
would download ~1 GB every single run to discover nothing had changed.

The **notify** job files issues, deduplicating on a hidden `<!-- autobump-key:
… -->` marker rather than the title, so retitling an issue by hand doesn't cause a
duplicate next week.

Other behaviours worth knowing: **one branch per SDK version**
(`autobump/10.0.302`), so re-runs are idempotent and a branch a human is working
on is never force-pushed over; the fork is **re-synced from upstream** before each
bump, since basing on a stale fork would produce a PR full of unrelated reverts;
and the rerender is requested as a **PR comment**, because conda-forge documents
the trigger as a comment (or an issue title/comment) and *not* as the PR
description.

## Setup

### 1. `FEEDSTOCK_TOKEN`

The token has to do four things:

| Action | Repo | Needs |
|---|---|---|
| push the bump branch | `acesnik/dotnet-feedstock` | contents: write |
| open the PR | `conda-forge/dotnet-feedstock` | pull requests: write |
| comment `@conda-forge-admin, please rerender` | `conda-forge/dotnet-feedstock` | issues: write |
| file escalation issues | whatever `issue_repo` names | issues: write |

`GITHUB_TOKEN` can't do any of it — it's scoped to this repo only.

**Recommended: a classic PAT with the single `public_repo` scope.** That covers
all four, because it acts as you and you're a listed `recipe-maintainer` on the
feedstock. Do *not* grant full `repo` — `public_repo` is the narrower scope and
this needs nothing private. Set an expiry.

Be aware of what that costs: `public_repo` grants write to **every public repo
you can write to**, including every other feedstock you maintain. That's broader
than this job needs, and it's inherent to classic scopes.

**Tighter, at the cost of one click per release:** a fine-grained PAT restricted
to `acesnik/dotnet-feedstock` (Contents: read/write) and your `issue_repo`
(Issues: read/write). Fine-grained tokens generally *cannot* open a PR on a repo
owned by another org unless that org has enabled fine-grained PAT access, so with
this option drop the `gh pr create` step: the bot pushes the branch and files an
issue containing the compare link, and you click it. Strictly less standing
authority pointed at a shared conda-forge repo.

### 2. Check `channels.json`

Confirm `track` and `policy` match your intent before the first scheduled run.

### 3. Dry run first

```
gh workflow run check-releases.yml -f dry_run=true
```

`dry_run` defaults to `true`, so a manual run can't open a PR or file issues by
accident. Scheduled runs act (`inputs.dry_run` is empty on a schedule and the
guard is `!= true`).

## Using the tools by hand

No CI needed. Run the updater from a feedstock checkout:

```bash
cd path/to/dotnet-feedstock

update-dotnet-version.py --list-channels            # every channel + support phase
update-dotnet-version.py --channel 10.0 --list-rids # RIDs Microsoft publishes
update-dotnet-version.py --channel 10.0 --probe     # stale? no downloads
update-dotnet-version.py --channel 10.0 --dry-run   # resolve URLs, no downloads
update-dotnet-version.py --channel 10.0             # hash, print the block
update-dotnet-version.py --channel 10.0 --write     # rewrite meta.yaml
update-dotnet-version.py --sdk-version 10.0.302 --write   # pin exactly
```

Exit codes: `0` success / up to date, `10` update available (`--probe` only), `1`
error.

And the planner, from this repo:

```bash
scripts/plan.py channels.json path/to/dotnet-feedstock | jq
```

Standard library only, deliberately — everything runs in a bare container with no
`pip install` step.

## Known gaps

- **`--write` does not rerender.** It can't; `conda-smithy` isn't a dependency
  here. The workflow delegates that to conda-forge's bot via a PR comment, which
  means the rerender's diff lands without human review — and that diff can be
  large when a feedstock's rendering is stale.
- **`active_subdirs` is a manual list.** Nothing detects conda-forge adding or
  retiring a subdir, so a genuinely new platform stays invisible until someone
  updates it. The `win-32` case above is why it can't be inferred cheaply.
- **A new RID needs recipe surgery, not just a hash.** The escalation issue spells
  this out, but the trap is real: adding an arch usually requires *narrowing* an
  existing broader selector, and forgetting to means silently shipping the wrong
  hash for the new platform.
- **No SDK feature-band handling.** A release can ship several SDKs
  (`10.0.1xx`, `10.0.2xx`); the tools take the release's primary `sdk` unless you
  pass `--sdk-version`.
- **Fork and upstream are hardcoded** in the workflow's `env` block.
- **Cross-compiled platforms may not be tested.** conda-forge can't emulate
  Windows arm64 on x64 runners, so `win_arm64` packages are built and uploaded
  without the test suite running. First real validation is a user installing it.
