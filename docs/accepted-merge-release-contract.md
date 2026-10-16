# Accepted-merge release contract

`.github/workflows/reusable-accepted-merge-release.yml` (MINECRAFT-371,
implementing MINECRAFT-44). A reusable (`workflow_call`) workflow that owns
the shared *mechanism* of turning an accepted merge into a release:
checkout of an immutable SHA, serialized/idempotent GitHub Release
reconcile, the Modrinth publication boundary via
[`rinth`](https://github.com/brooswit-minecraft/rinth), and a
machine-readable outcome contract. Consumer repos (e.g.
`brooswit-minecraft/sickos`) keep their own eligibility/version/hold
*policy* and call this workflow with the results.

This is a separate, additive contract from the existing
`reusable-release.yml`/`release.yml` (push- and `release`-event triggered,
no `authorized`/`hold`/idempotent-reconcile concepts). Nothing here changes
that workflow's behavior.

A calling repo's own checkout (at `sha`) is **not** assumed to contain
`scripts/release_state.py` — this workflow checks that helper out itself
from `brooswit-minecraft/schematic` (pinned to the REQUIRED `schematic-ref`
input) into `.schematic-mechanism/` in every job that needs it, so a
consumer repo needs nothing copied in to use this workflow. Each such
checkout is immediately followed by a fail-fast check that
`.schematic-mechanism/scripts/release_state.py` actually exists — a
`schematic-ref` that resolves but doesn't contain the helper (e.g. the `v1`
tag, cut before this helper existed) fails loudly with an actionable
`::error::` naming the problem, not a confusing Python traceback three
steps later.

`schematic-ref` is resolved to an exact commit SHA exactly **once** — in
the `normalize` job, immediately after its own mechanism checkout — and
that resolved SHA (not the raw `schematic-ref` input) is what the
`release` job's own mechanism checkout pins to (`needs.normalize.outputs.schematic-sha`).
If `schematic-ref` names a tag or branch rather than an exact SHA, this is
what guarantees both jobs execute the identical mechanism revision even if
that ref moved between the two checkouts, rather than each job resolving
`schematic-ref` independently and risking two different commits.

**Why `schematic-ref` has no default**: `v1` is this repo's existing stable
tag, but it predates `reusable-accepted-merge-release.yml` entirely and
does not contain `scripts/release_state.py` — defaulting to it would be a
default that is silently broken for every caller until `v1` is next cut
(which this PR, under the active MINECRAFT-12 release hold, is not
authorized to do). Requiring the input instead means a caller always pins
to something that was actually checked to contain the helper.

**A concrete, currently-valid example**: commit
`324ed36482cf43b82bf8de136373bcb65d68c572` on this repo's `MINECRAFT-371`
branch contains `scripts/release_state.py` (verify with `gh api
repos/brooswit-minecraft/schematic/contents/scripts/release_state.py?ref=324ed36482cf43b82bf8de136373bcb65d68c572`).
MINECRAFT-371 is the implementation owner; MINECRAFT-44 remains closed.
Once this PR merges directly to `main`, pin
`schematic-ref` to the resulting commit SHA on `main` instead (verify the
same way); once a `v1` tag is next cut on a commit at or after that merge,
pin to `v1` again.

The existing main-branch tag maintenance advances `v1` after integration.
Before that succeeds, use a reviewed commit containing this workflow for
both the `uses` ref and `schematic-ref`; do not assume the old `v1` has it.

## Calling it

```yaml
jobs:
  release:
    permissions:
      contents: write
    uses: brooswit-minecraft/schematic/.github/workflows/reusable-accepted-merge-release.yml@v1
    with:
      sha: ${{ needs.policy.outputs.accepted-sha }}
      version: ${{ needs.policy.outputs.version }}
      authorized: ${{ needs.policy.outputs.authorized == 'true' }}
      hold: ${{ needs.policy.outputs.hold == 'true' }}
      schematic-ref: '324ed36482cf43b82bf8de136373bcb65d68c572'  # see note above — re-pin once merged/tagged
    secrets: inherit
```

## Inputs

| Input | Required | Type | Description |
| --- | --- | --- | --- |
| `sha` | yes | string | Immutable source commit SHA — exactly 40 lowercase hex characters. Always checked out explicitly; never the caller's own triggering ref. |
| `version` | yes | string | Pack version to release, e.g. `1.2.3` or `1.2.3-beta.1`. A leading `v` is accepted and stripped. Validated as semver-like, and **canonicalized before anything else uses it** — including the concurrency lock — so `1.2.3` and `v1.2.3` are the same release, not two independently-locked ones. |
| `authorized` | yes | boolean | Whether the caller's own policy has accepted this merge for release. |
| `hold` | no (default `false`) | boolean | Explicit release hold, checked only when `authorized` is `true`. |
| `modrinth-required` | no (default `true`) | boolean | When `true` and Modrinth isn't configured, the run **fails** rather than silently publishing GitHub-only. |
| `notes-file` | no (default `''`) | string | Repo-relative path, read at the checked-out `sha`, used as both the GitHub release body and the Modrinth changelog. |
| `rinth-ref` | no (default `v0.9.1`) | string | Pinned `brooswit-minecraft/rinth` tag invoked via `bunx`. |
| `schematic-ref` | **yes, no default** | string | Exact `brooswit-minecraft/schematic` commit SHA (or tag, once one exists that contains the helper) this workflow checks its own mechanism helper (`scripts/release_state.py`) out from. See "a concrete, currently-valid example" above. A value that doesn't contain the helper fails fast with an actionable error (see above), not silently. |

An invalid `sha` or `version` fails the run before any consumer checkout/build step
with an actionable `::error::`.

## Outputs

| Output | Description |
| --- | --- |
| `state` | One of the six states below. |
| `sha` | The exact source SHA this run built from. |
| `version` | The validated, normalized version. |
| `artifact-name` | Name of the uploaded `.mrpack` workflow artifact. |
| `release-id` / `release-url` | GitHub Release identity, when one exists for this run (reused or freshly created). Empty otherwise. |
| `modrinth-version-id` | Modrinth version identity, when one exists for this run (reused or freshly created). Empty otherwise. |

## The six states

Decision logic lives in `scripts/release_state.py` (`decide()`), unit
tested in `tests/test_release_state.py` independent of any runner.

1. **`build-only`** — `authorized` is `false`. The pack is built and
   uploaded as a workflow artifact; nothing is released or published.
   A successful build returns this state; build failures return `failed`.
   External release/Modrinth reconciliation is not run.
2. **`held`** — `authorized` is `true` but `hold` is `true`. Same
   build-only behavior, but distinguishes "paused despite being eligible"
   from "not eligible" for anyone reading `state`. External reconciliation
   is not run; build failures still return `failed`.
3. **`skipped`** — `authorized` is `true`, `hold` is `false`, and
   reconciliation finds that **both** the target GitHub Release and (when
   Modrinth applies) the Modrinth version already exist and match this
   exact `sha`/`version` — a full replay of an already-completed publish.
   No mutation is performed; outputs report the existing identity. This is
   what makes a retried or overlapping event for an already-published
   version a no-op instead of a duplicate release (DoD requirement).
4. **`publishing`** — written right after the GitHub Release step (create
   or reuse) completes, before the Modrinth step is attempted. If the run
   then fails during the Modrinth step, `state` is left at `publishing` —
   not silently `failed` and not incorrectly `published` — meaning "the
   GitHub Release exists; Modrinth did not confirm; retry is safe." A
   retry's reconcile step finds the existing release and does not recreate
   it; it only needs to resolve Modrinth.
5. **`published`** — the fully successful terminal state: a GitHub Release
   exists (created or reused) **and** Modrinth publication is resolved
   (created, reused, or not required in the first place).
6. **`failed`** — any validation or reconciliation failure: invalid
   `sha`/`version`, an existing `v<version>` tag whose own peeled commit is a
   *different* SHA than requested (refused rather than repointed — checked
   regardless of whether a GitHub Release object exists for that tag, and
   regardless of what any such release's own cached metadata claims), or —
   when `modrinth-required` is `true` — missing `MODRINTH_TOKEN`/
   `MODRINTH_PROJECT_ID`. The run exits non-zero with an actionable
   `::error::`.

**A green run never implies publication was skipped silently**: `state`
always distinguishes `build-only`/`held`/`skipped` (no new publication this
run) from `published` (this run completed a publication, whether freshly or
by resuming a partially-done one).

## Idempotence and concurrency (DoD #2)

- A dedicated `normalize` job validates inputs and canonicalizes `version`
  (strips a leading `v`) *before* the `release` job's `concurrency:` key is
  evaluated (job-level `concurrency:` can reference `needs.*.outputs`). The
  lock is `accepted-merge-release-<repo>-<canonical-version>`,
  `cancel-in-progress: false` — so `1.2.3` and `v1.2.3` share one lock
  instead of racing each other under two different group names, and an
  overlapping/retried call for the same release reconciles against the
  prior run in order instead of racing it.
- The release-tag reconcile resolves `v<version>`'s own **peeled git
  commit** (`git rev-parse refs/tags/v<version>^{commit}`) — not a GitHub
  Release's cached `target_commitish` — and compares that to the requested
  `sha`. This is checked whether or not a Release object exists for the
  tag (a tag can exist without one), and a mismatch **fails without any
  mutation**, regardless of what a co-existing release's own metadata might
  claim. The GitHub Release object itself (`gh release view v<version>`) is
  looked up separately, purely to decide reuse-vs-create once the tag check
  has already passed.
- The Modrinth reconcile (`rinth versions latest <project> --version-number
  <version> --json`) reuses an existing matching version instead of
  attempting to recreate it; `rinth publish` itself also refuses a
  duplicate `version_number` (exit 5) as a second, independent guard.

## RINTH boundary

Modrinth publication goes exclusively through `rinth`'s supported CLI
surface (`bunx --bun github:brooswit-minecraft/rinth#<rinth-ref>`), the same
boundary `reusable-release.yml` already uses — no direct Modrinth API calls
from this workflow. See `brooswit-minecraft/rinth`'s README for the full
`versions latest` / `publish` contract.

## Release hold (MINECRAFT-12)

While the MINECRAFT-12 release hold is active, this workflow is validated
only by its unit tests (`tests/test_release_state.py`) and dry read-only
reconcile calls — it is not exercised against a real publish.
