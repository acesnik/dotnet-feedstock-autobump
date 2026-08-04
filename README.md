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
| A line leaves preview and should take over `main` | **transition**: cut `vN`, then PR |
| A preview line exists, or a line is skipped by policy | **notice** in the run summary |
| Nothing changed | a table in the run summary |

A channel id that fails the format check is dropped rather than passed to a
shell — and that drop is surfaced as a notice, because it is the one event that
makes the bot go blind to a whole release line. An earlier version logged it to
stderr only.

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
"tracked": { "10.0": "main", "9.0": "v9", "8.0": "v8" }
```

All three were stale at the time of writing — `main` at `10.0.100` against
`10.0.302`, `v9` against `9.0.316`, `v8` at `8.0.407` against `8.0.423`.

**9.0 was orphaned until recently**, and it is the clearest illustration of why
this matters: it shipped as `9.0.203` from `main`, then 10.0 took `main` over and
no `v9` branch was ever cut, so it sat nine patch releases behind with nowhere to
patch from while Microsoft still listed it as `maintenance`. The
"still supported but not tracked" escalation exists to catch exactly that; `v9`
has since been cut and the line is tracked again.

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

## The line transition, automated

When a line leaves preview, the convention is: cut a `vN` branch from `main` so
the outgoing line stays patchable, then move `main` to the new line. Skipping the
first half is exactly how 9.0 got orphaned, so the bot does it.

Three actions, two of them ordinary reviewable PRs:

1. **Cut `vN` from `main`'s current tip.** The one action not mediated by a PR,
   because a ref cannot be created by one. Additive only — a new ref, never a
   force-update — and it aborts if the branch already exists. Set
   `transition.cut_branch_upstream: false` to have it pushed to the fork instead,
   leaving you one `git push upstream vN`.
2. **PR `main` → the new line**, then request a rerender by comment.
3. **PR this repo's `channels.json`** so `tracked` reflects the new layout
   (`10.0` moves to `v10`, `11.0` takes `main`). A PR rather than a direct edit,
   because that file is human-owned. The edit is textual, not a JSON round-trip,
   so the `_comment` keys documenting every decision survive.

This is safe to automate because the move is mechanical — verified by rendering
the recipe at `11.0.100`, which needs no edits beyond the two version variables:
`framework` derives `net11.0` for the test paths and the metapackage pins follow
`runtime_version`.

One interaction handled explicitly: if the outgoing line is *also* stale, its
bump is **deferred** rather than opened, because both PRs would target `main` and
conflict. Its patches belong on the new `vN` branch, which the next run picks up
once `tracked` maps it there — self-healing rather than order-dependent.

The planner also warns when a `vN` branch's recipe carries a different line's
version, which catches a branch cut from the wrong commit. That is a notice, not
an issue: it is often benign, since the recipe is version-agnostic and a branch
cut from a newer `main` inherits newer rerender infrastructure.

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
  "tracked": { "10.0": "main", "9.0": "v9", "8.0": "v8" },  // line -> branch
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
answer. Under `lts`, a new STS line raises no issue but does produce a notice; a
new LTS line raises an issue.

A still-supported line missing from `tracked` is escalated regardless of policy —
that's a maintenance gap, not a preference.

The set of RIDs the recipe *packages* is deliberately **not** listed here — it's
read from the newest tracked branch's recipe, so the audit cannot drift out of
sync with what the recipe actually does.

### The recipe shape is discovered, not assumed

Each branch carries whatever shape it was cut with. `main` has six platforms with
`# [win and x86_64]`; a branch from before win-arm64 has five with a bare
`# [win]`. So the updater reads the mapping out of the recipe's own lines:

```jinja
{% set platform = "linux-arm64" %}  # [linux and aarch64]
{% set platform = "win-x64" %}      # [win]
```

This is not hypothetical tidiness. A hardcoded `PLATFORMS` list matched `main` and
hard-failed on both `v8` and `v9` with `could not find the sha256 line for
selector # [win and x86_64]` — the tool was 100% broken for the branches it was
about to be pointed at, while every test passed, because every fixture used the
newest shape. There is now an old-shape fixture.

`PLATFORMS` survives only as a fallback for when no recipe is available.

Self-consistency is checked **both ways**, because discovery trusts the `platform`
lines. A platform with no `sha256` line fails loudly; so does a `sha256` line with
no `platform` line — that orphan would otherwise be silently left **stale**, and
the recipe would pair one platform's URL with another's hash and fail at download,
far from the cause. Dropping *both* lines for an arch is not an error: that is
simply an older shape.

### Shell-safety and packageability are separate checks

Two different questions, and conflating them broke `--dry-run --channel 11.0`
entirely:

- **Shell-safe?** Enforced always, on every upstream-derived value, because these
  become shell arguments and branch names in CI. Rejects `;`, `$`, backticks,
  quotes, whitespace and friends.
- **Packageable by conda?** Enforced only for `--write` / `--check`, and *before*
  any downloading. A preview's hyphen makes it unpackageable but perfectly safe to
  *inspect*, so rejecting it at resolve time destroyed the ability to look at a
  preview at all — and an earlier placement burned ~1 GB of downloads before
  rejecting the result.

`conda_version_problem` has exactly one definition, in the updater, used by both
the planner (which escalates) and the writer (which refuses). A test asserts
`plan.py` has not grown a second copy.

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

### Rerenders are requested one at a time, after the matrix

Not from inside the bump matrix. Two rerenders of the *same feedstock* at once
collide: #118 and #119 had theirs requested 1 second apart, and one died with

```
OSError 39, 'Directory not empty'
```

inside conda-forge's rerender container while the other succeeded. Worth noting
what was *not* the cause — the delay after PR creation was near-identical (19s vs
17s), so waiting longer after opening the PR would not have helped. Concurrency
was the variable.

So a separate `rerender` job runs after the whole matrix and requests them
sequentially with a gap. Since a rerender takes minutes, that reduces overlap
rather than eliminating it — which is why the job is **stateless**: it finds open
`autobump/` PRs carrying no `MNT: Re-rendered` commit, rather than being told what
this run opened. Any PR whose rerender failed earlier gets picked up next run,
instead of waiting for a human to notice. It only ever touches `autobump/`
branches, so a human's PR is never poked.

One implementation trap worth recording: asking `gh pr list` for `commits` across
50 PRs exceeds GitHub's GraphQL node limit outright (505,050 > 500,000), so the
query is two-stage — cheap fields to find candidates, then per-PR commit lookups.

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

## Tests

```bash
pytest            # 54 offline tests, ~2.5s
pytest -m live    # 9 tests against real upstream endpoints
```

The offline suite monkeypatches `fetch_json`, `repodata_size` and git, and an
autouse fixture makes any real request in a default-run test **fail** rather than
silently succeed — so "offline" is enforced, not aspirational.

Most of these assertions started life as throwaway one-liners while building
this, and each one corresponds to a bug that actually existed or a decision that
was easy to get wrong:

- `rewrite_meta` puts each hash on **its own selector**. Six sha256 lines are
  structurally identical apart from a trailing `# [linux and aarch64]` comment, so
  an anchoring bug would ship the wrong platform's hash — a package that installs
  and then fails on exactly one architecture.
- The **negative** cases: EOL lines are never bumped, previews are refused,
  a frozen subdir is skipped, an outgoing line's bump is deferred during a
  transition. All of these are "do nothing" behaviours, where a regression looks
  identical to success.
- The audit reads `PLATFORMS` from the updater rather than config, which the suite
  asserts — that drift is the exact thing the audit exists to detect.

The **live** suite exists because the offline one cannot catch the likeliest real
breakage: Microsoft or anaconda.org changing a payload shape. Every offline test
asserts against fixtures, so all of them would keep passing while production
broke. The live tests check only the fields the scripts read — including that SDK
hashes are still 128-char SHA-512 (if that ever becomes SHA-256, the re-hashing
step can go), that every RID in `PLATFORMS` is still published, and that the
recipe's `dotnetcli.azureedge.net` host still serves artifacts even though the
metadata points at a different hostname. They run weekly on a schedule rather than
per-commit, since a failure there is upstream's change, not yours.

## Known gaps

- **`--write` does not rerender.** It can't; `conda-smithy` isn't a dependency
  here. The workflow delegates that to conda-forge's bot via a PR comment, which
  means the rerender's diff lands without human review — and that diff can be
  large when a feedstock's rendering is stale.
- **`active_subdirs` is a manual list.** Nothing detects conda-forge adding or
  retiring a subdir, so a genuinely new platform stays invisible until someone
  updates it. The `win-32` case above is why it can't be inferred cheaply.
- **The architecture audit runs per line.** Each line has its own recipe *and*
  its own set of published RIDs, so a "dropped RID" verdict drawn from 10.0 can be
  false for 8.0. Breakage is therefore checked per line, and an older line simply
  packaging fewer RIDs is not flagged — that is normal, not a drop.
- **`git_show` searches remotes before the bare ref.** It used to try the bare
  ref first, which let a stale *local* branch shadow the remote one and silently
  report upstream `v8` as `8.0.407` when it was at `8.0.408` — wrong data, no
  error, in a tool whose whole job is reading other people's branches.
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
