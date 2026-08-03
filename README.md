# dotnet-feedstock-autobump

Watches Microsoft's .NET release metadata and acts on what it finds, for
[conda-forge/dotnet-feedstock](https://github.com/conda-forge/dotnet-feedstock).

Addresses [#55](https://github.com/conda-forge/dotnet-feedstock/issues/55)
(auto-bump doesn't recognise the download links) and
[#90](https://github.com/conda-forge/dotnet-feedstock/issues/90) (script for
getting SHAs).

## The organising principle

**Mechanical changes become PRs. Judgment calls become issues.**

A patch bump inside a tracked release line is mechanical — two version variables
and one hash per platform, all derivable from published metadata. That gets a PR,
unattended.

Everything else gets escalated to a human, because the bot has no basis for
deciding:

| Situation | Action |
|---|---|
| New SDK in a tracked line | **PR** per line, against that line's branch |
| A line newer than anything tracked goes active | **issue** |
| A still-supported line isn't tracked at all | **issue** (maintenance gap) |
| A tracked line reaches EOL below its final version | **issue** |
| Microsoft starts publishing a new architecture | **issue** |
| Microsoft *drops* an architecture we package | **issue** (impending breakage) |
| A preview line exists, or a line is skipped by policy | **notice** in the run summary |
| Nothing changed | a table in the run summary |

Notices are the third category, and they exist because silence was this
workflow's original sin. A hardcoded channel list would have let .NET 11 and 12
ship unremarked. A notice opens nothing and costs one line, but it makes
"we saw it and chose not to act" distinguishable from "we never looked".

## Multiple lines, not just the newest

This feedstock maintains several .NET lines at once, and the bot follows that.
Upstream branches are `main v2 v3 v5 v6 v7 v8`: when a new line takes over
`main`, the previous one gets a `vN` branch so it can keep receiving patches.
conda-forge accumulates every published version, so users can pin `dotnet=8`.

Each tracked line is planned and bumped **independently against its own branch**,
so a 8.0 bump lands on `v8` and never rewrites `main`'s 10.0 recipe. The bump job
is a matrix over whatever the plan found stale, with `fail-fast: false`, so a
problem on one line doesn't block another and their PRs cannot conflict.

Tracking is declared in `channels.json`:

```jsonc
"tracked": { "10.0": "main", "8.0": "v8" }
```

At the time of writing, both were stale — `main` at `10.0.100` against
`10.0.302`, `v8` at `8.0.407` against `8.0.423` — and **9.0 was orphaned**: it
shipped as `9.0.203` from `main`, then 10.0 took `main` over and no `v9` branch
was ever cut, so it sits nine patch releases behind with nowhere to patch from
while Microsoft still lists it as `maintenance`. That is the specific failure the
"still supported but not tracked" escalation exists to catch.

EOL lines (`v3`, `v5`, `v6`, `v7`) are deliberately not watched. Each already
sits at its line's final release, and Microsoft will publish no more, so there is
nothing to detect.

### Previews are detected but never packaged

11.0 is in preview, and the bot reports it as a notice while refusing to act. Two
independent reasons, both verified rather than assumed:

```
conda-build:  Bad character(s) (-) in package/version: 11.0.100-preview.6.26359.118
ordering:     10.0.302  <  11.0.100-preview.6.26359.118  <  11.0.100
```

conda package filenames are `name-version-build`, so a hyphen in a version is
structurally ambiguous and conda-build rejects it — and every Microsoft preview
SDK string contains one. Worse, even with the version mangled to make it build, a
preview **outranks stable**: publishing it would make `conda install dotnet`
resolve to a preview instead of the 10.0 LTS.

So adding a preview line to `tracked` doesn't silently start opening broken PRs —
it escalates with that explanation. There's also a general guard: any tracked
line whose upstream version isn't a valid conda version escalates rather than
producing a PR that cannot build, so an unexpected `-rc.1` is caught the same way.

Shipping previews at all would need a deliberate scheme — a mangled version *and*
a separate package name or channel label — which is a design decision, not a
version bump.

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
  "tracked": { "10.0": "main", "8.0": "v8" },   // line -> upstream branch
  "policy": "lts",                              // lts | latest | manual
  "issue_repo": "acesnik/dotnet-feedstock-autobump",
  "rid_map":        { "win-arm64": "win-arm64", ... },   // MS RID -> cf subdir
  "ignore_rids":    ["linux-musl-x64", ...],             // no cf equivalent
  "active_subdirs": ["linux-64", "win-arm64", ...],      // cf actually builds these
  "min_repodata_bytes": 102400
}
```

`policy` governs only lines **newer** than anything tracked, since adopting one
means moving `main` and cutting a `vN` branch for the outgoing line. Whether to
follow STS (18 months) or stay on LTS (3 years) is not a question metadata can
answer. Under `lts`, a new STS line is passed over silently; a new LTS line
raises an issue.

A still-supported line missing from `tracked` is escalated regardless of policy —
that's a maintenance gap, not a preference.

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
plan job  ── list channels (1 GET) ────┐
          ── git show <branch>:meta ───┤                ┌─ bump (8.0 → v8)
          ── list RIDs for newest line ┤─ plan.json ──┬─┤  matrix, ~1 GB each
          ── HEAD subdir repodata ─────┘              │ └─ bump (10.0 → main)
                                                      └─── notify (issues)
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

The checkout needs every tracked branch present as a ref (`fetch-depth: 0`);
recipes are read with `git show <ref>:recipe/meta.yaml`, so nothing is checked
out per line. It resolves refs across any remote name, not just `origin`.

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
- **`tracked` is maintained by hand.** The bot flags a supported line that isn't
  tracked, but cutting the `vN` branch and adding the mapping is manual — as it
  should be, since it decides what conda-forge keeps serving.
- **Cross-compiled platforms may not be tested.** conda-forge can't emulate
  Windows arm64 on x64 runners, so `win_arm64` packages are built and uploaded
  without the test suite running. First real validation is a user installing it.
