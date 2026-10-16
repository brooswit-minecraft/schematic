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
    secrets: inherit
```

## Inputs

| Input | Required | Type | Description |
| --- | --- | --- | --- |
| `sha` | yes | string | Immutable source commit SHA — exactly 40 lowercase hex characters. Always checked out explicitly; never the caller's own triggering ref. |
| `version` | yes | string | Pack version to release, e.g. `1.2.3` or `1.2.3-beta.1`. A leading `v` is accepted and stripped. Validated as semver-like. |
| `authorized` | yes | boolean | Whether the caller's own policy has accepted this merge for release. |
| `hold` | no (default `false`) | boolean | Explicit release hold, checked only when `authorized` is `true`. |
| `modrinth-required` | no (default `true`) | boolean | When `true` and Modrinth isn't configured, the run **fails** rather than silently publishing GitHub-only. |
| `notes-file` | no (default `''`) | string | Repo-relative path, read at the checked-out `sha`, used as both the GitHub release body and the Modrinth changelog. |
| `rinth-ref` | no (default `v0.9.1`) | string | Pinned `brooswit-minecraft/rinth` tag invoked via `bunx`. |

An invalid `sha` or `version` fails the run before any checkout/build step
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
   Always a successful (green) run.
2. **`held`** — `authorized` is `true` but `hold` is `true`. Same
   build-only behavior, but distinguishes "paused despite being eligible"
   from "not eligible" for anyone reading `state`.
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
   `sha`/`version`, a release tag that already points at a *different* SHA
   than requested (refused rather than repointed), or — when
   `modrinth-required` is `true` — missing `MODRINTH_TOKEN`/
   `MODRINTH_PROJECT_ID`. The run exits non-zero with an actionable
   `::error::`.

**A green run never implies publication was skipped silently**: `state`
always distinguishes `build-only`/`held`/`skipped` (no new publication this
run) from `published` (this run completed a publication, whether freshly or
by resuming a partially-done one).

## Idempotence and concurrency (DoD #2)

- `concurrency: group: accepted-merge-release-<repo>-<version>`,
  `cancel-in-progress: false` serializes overlapping/retried calls for the
  same repo+version so they reconcile against each other in order instead
  of racing.
- The GitHub Release reconcile (`gh release view v<version>`) reuses an
  existing release that targets the requested `sha`, and **refuses** (fails
  loudly, `state=failed`) a release whose tag already targets a *different*
  SHA — it never repoints an existing release.
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
