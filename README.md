# schematic

A [packwiz](https://packwiz.infra.link) modpack **template** for Minecraft **1.20.1 /
Forge** (both are defaults you can change) — clone it, edit a couple of fields, and you
have a modpack project that validates and builds itself on every push, with release and
server-update automation available too. CI, release, and server-update are provided as
**reusable GitHub Actions workflows**, called — from the thin stubs already sitting in
this repo — at their upstream location in this template, pinned `@v1`, so you get all
three without writing any workflow logic yourself.

[brooswit-factory/schematic-example](https://github.com/brooswit-factory/schematic-example)
is the living example built from this template: a real pack (starting from the [Create](https://modrinth.com/mod/create)
mod) that began life as a clone of this repo. Look there for what a filled-in version of
this template looks like.

## Getting started

```sh
git clone https://github.com/brooswit-minecraft/schematic.git <yours>
cd <yours>
git remote rename origin template
git remote add origin <your repo url>
git push -u origin main
```

`template` stays as the upstream you can pull future improvements from (see
[Updating from the template](#updating-from-the-template) below); `origin` becomes your
own repo.

## Rename checklist

Almost nothing to rename. Edit the pack identity in `pack.toml`:

```toml
name = "my-modpack"      # -> your pack's name
author = "your-name"     # -> you
```

and, if you want a Minecraft version or mod loader other than the defaults, the
`[versions]` block (`minecraft`, `forge`) too — the reusable workflows derive the
Modrinth game-versions/loaders from these.

Then regenerate the index and commit:

```sh
make refresh
git add pack.toml index.toml
git commit -m "Rename pack"
```

That's it. The build artifact name (`<name>-<version>.mrpack`) is derived from
`pack.toml` automatically, and so are the Modrinth game version and loader it publishes
under — nothing to rename in the Makefile or the workflows. The Modrinth project you
publish *to* is a separate thing: it's set by the `MODRINTH_PROJECT_ID` repo variable,
not by anything in `pack.toml` (see [Secrets & variables](#secrets--variables) below).

Nothing under `.github/workflows` needs editing or deleting — see
[Template-only files](#template-only-files) below for why.

Verify with `grep -ri schematic .`: the only remaining hits should be this README, the
`uses: brooswit-minecraft/schematic/.github/workflows/reusable-<name>.yml@v1` line in
each of `ci.yml`, `release.yml`, and `server-update.yml`, and the
`if: github.repository == 'brooswit-minecraft/schematic'` guard in `tag-v1.yml` and
`tests.yml`. **Leave all of those alone** — the `uses:` lines point at this project's
upstream reusable workflows, not at your own pack, and the `tag-v1.yml`/`tests.yml`
guards are what keep those template-only files from running in your repo (see
[Template-only files](#template-only-files) below). Renaming any of them will break the
thing they exist to do.

Optionally, you can also replace the copyright holder in `LICENSE` with your own name —
that's not required for anything to work; it's your call whether the template's MIT
license and holder should carry over to your fork.

## Updating from the template

### Pulling template changes

This repo keeps evolving — Makefile fixes, README clarifications, improvements to the
reusable workflows. Pull those into your own pack with:

```sh
git fetch template
git merge template/main                                  # expect conflicts
git checkout ORIG_HEAD -- pack.toml index.toml mods      # your pack content wins, conflicted or not
git checkout --theirs -- <other conflicted files you have not customised>
make refresh
git add -A
git commit
```

`ORIG_HEAD` is your branch tip as it was immediately before the merge — checking out
`pack.toml`, `index.toml`, and `mods/` from it restores **your own content** there no
matter what happened during the merge.

That last point matters: git only reports a conflict on a file **both sides changed**.
If the template deletes or changes a file you never touched — most importantly a mod
file in `mods/`, or `index.toml` — there's no conflict, and the merge silently applies
the template's version, which for a deleted mod means it's just gone with nothing to
resolve. That's the intended behaviour for tooling files (`Makefile`, workflow stubs)
that should track the template automatically, but it's exactly why the
`git checkout ORIG_HEAD -- pack.toml index.toml mods` step above is unconditional
rather than only for conflicted paths — it protects your pack content whether or not git
flagged a conflict on it.

One caveat: `git checkout ORIG_HEAD -- ...` only restores paths that existed on your
branch; it never deletes, so a file the template adds under `mods/` (such as its
placeholder `mods/.gitkeep`) is kept — harmless, because `.packwizignore` keeps it out
of the exported pack.

For the `--theirs` line, "other conflicted files you have not customised" typically
means `Makefile` — take the template's version of it unless you've made local edits
worth preserving. `README.md` is the
one file you have almost certainly customised — reconcile it by hand: keep your
pack-specific prose and fold in template improvements where they still apply. If you
previously deleted `.github/workflows/tag-v1.yml` locally and the template has since
changed it, you'll see it as a delete/modify conflict instead of a clean merge; per
[Template-only files](#template-only-files) above, you don't need to delete it in
the first place, so the simplest fix is to keep the template's version
(`git checkout --theirs -- .github/workflows/tag-v1.yml`) rather than re-deleting it.

### The pinned workflow stubs

`ci.yml`, `release.yml`, and `server-update.yml` each pin their `uses:` line to `@v1` —
a moving tag kept pointed at this repo's `main`. Fixes and improvements to the reusable
workflows they call reach your repo automatically, with **zero merge effort** on your
part.

`v1` promises backwards-compatible inputs, secrets, and variable names; anything that
would break your workflow ships as a `v2` instead. If you'd rather not receive moving
updates, pin a specific tag or commit SHA in place of `@v1` in your stub's `uses:` line.

## Template-only files

Five files under `.github/workflows` are template-only, and safe to leave in place:

`tag-v1.yml` keeps the `v1` tag on **this** repo pointed at its own `main`. It is
guarded by a `github.repository` check, so it is inert in any repo cloned from this
template — the job is skipped entirely, so it creates no tag in your repo.

`reusable-ci.yml`, `reusable-release.yml`, and `reusable-server-update.yml` are
`workflow_call`-only definitions — nothing invokes them by local path. Your stubs
(`ci.yml`, `release.yml`, `server-update.yml`) call them **upstream**, at
`brooswit-minecraft/schematic/.github/workflows/reusable-<name>.yml@v1`, so your local
copies never run.

`tests.yml` runs this repo's own `tests/` suite (the unit tests for
`scripts/server_deploy.py`) on every push to `main` and every pull request. A consumer
built from this template has no `tests/` directory, so like `tag-v1.yml` it is guarded
by the same `github.repository` check — the job is skipped, not failed, in your repo.

There's no need to delete any of the five — doing so gains nothing, since they don't
run locally either way, and deleting one only creates work for you later: a
`git merge template/main` does not restore a file you deleted (your deletion simply
persists, merge or no merge) until the template itself changes that file, at which
point the merge stops with a delete/modify conflict you have to resolve by hand.
Leaving the five alone avoids that conflict entirely, on every future merge.

`ci.yml`, `release.yml`, and `server-update.yml` are the three stubs you, as a consumer
of this template, need to care about — the five template-only files above need no
attention at all.

## Secrets & variables

The default catalog integration is optional. With none of it set, `ci.yml` still
builds and uploads the `.mrpack` as a workflow artifact, `release.yml` still builds
and attaches it to the GitHub Release, and the catalog server-update job skips
cleanly. Selecting `SERVER_DEPLOY_METHOD=sftp` is an explicit opt-in: all RCON,
SFTP, and Hosting settings documented below then become required and missing
configuration fails the deployment.

| Name | Kind | Used by | Purpose |
|---|---|---|---|
| `MODRINTH_TOKEN` | secret | `release.yml`, `server-update.yml` | Auth token for publishing to Modrinth and updating a Modrinth-hosted server |
| `MODRINTH_PROJECT_ID` | variable | `release.yml`, `server-update.yml` | Identifies which Modrinth project to publish to / follow |
| `MODRINTH_SERVER_ID` | variable | `server-update.yml` | The Modrinth-hosted server to keep in sync with releases |
| `SERVER_DEPLOY_METHOD` | variable | `server-update.yml` | Selects `modrinth-api` (default) or `sftp`; see the complete SFTP configuration below |

## Releasing

`.github/workflows/release.yml` cuts a release. To publish a new version:

1. Create a GitHub Release with a tag of the form `vX.Y.Z` (e.g. `v0.2.0`).
2. On publish, the workflow:
   - sets the pack version from the tag (in-workflow only — nothing is committed back),
   - builds `build/<name>-<version>.mrpack` with the same `make` targets used locally
     and in CI,
   - attaches the `.mrpack` to the GitHub Release as a download,
   - publishes the same file to Modrinth, if Modrinth is configured (see
     [Secrets & variables](#secrets--variables) above).

You can also dry-run the whole build-and-package path without creating a Release, via
`workflow_dispatch`:

```sh
gh workflow run release.yml --ref <branch> -f version=0.0.1-test
```

This builds and uploads the `.mrpack` as a workflow artifact but skips the
Release-asset step (there is no Release object to attach to). **A plain `workflow_dispatch`
run (no `publish: true`, see below) never publishes to Modrinth, even when
`MODRINTH_TOKEN` and `MODRINTH_PROJECT_ID` are both configured** — the Modrinth publish
only runs on `push` or `release`, or when `publish: true` is set. If Modrinth is
configured, a dry run still validates the payload it *would* send via
`rinth publish --dry-run` — project, file, version, channel, game version and loaders —
without ever calling the Modrinth API, so a dry run catches a bad target (e.g. a
mistyped loader) before a real release does.

### Releasing from an automated workflow with `GITHUB_TOKEN`

`push` and `release` cover a human cutting a release. Neither works for an *automated*
caller — say, a `repository_dispatch`-triggered workflow that just committed a version
bump and wants to release that exact commit: `repository_dispatch` isn't `push` or
`release`, so nothing would publish, and even if it were, `github.sha` on that run is the
commit *before* the bump. Pushing the bump to let `release.yml` fire on its own `push`
trigger doesn't work either — GitHub does not start workflow runs from a push made with
the default `GITHUB_TOKEN`.

Three `workflow_call` inputs on `reusable-release.yml` exist for exactly this case, and
all default to today's behaviour when left unset:

| Input | Type | Default | Effect |
|---|---|---|---|
| `ref` | string | `''` | Checks out this ref/SHA instead of the caller's own triggering ref, and the GitHub release's `target_commitish` becomes the **resolved SHA of that checkout** rather than `github.sha`. Leave empty for unchanged behaviour. |
| `publish` | boolean | `false` | When `true`, the run publishes exactly like a `push`-triggered run: the refuse-to-overwrite guard runs before the build, the GitHub release is created with generated release notes, and the Modrinth publish runs (if configured). Leave `false` for unchanged behaviour. |
| `notes-file` | string | `''` | Repo-relative path to a file (read at the checked-out `ref`) whose contents become the GitHub release body and the Modrinth changelog on the `push`/`publish: true` path. See [Authoring release notes for an automated release](#authoring-release-notes-for-an-automated-release) below. Leave empty for unchanged behaviour. |

A caller that commits its own bump and wants to release it passes both, from a job with
`contents: write` (permissions live in the **caller's** job, not in the reusable
workflow):

```yaml
name: Auto-bump release

on:
  repository_dispatch:
    types: [bump]

permissions:
  contents: write

jobs:
  bump-and-release:
    runs-on: ubuntu-latest
    outputs:
      sha: ${{ steps.bump.outputs.sha }}
    steps:
      - uses: actions/checkout@v4
      # ...bump pack.toml, commit, and push here...
      - id: bump
        run: echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"

  release:
    needs: bump-and-release
    permissions:
      contents: write
    uses: brooswit-minecraft/schematic/.github/workflows/reusable-release.yml@v1
    with:
      publish: true
      ref: ${{ needs.bump-and-release.outputs.sha }}
    secrets: inherit
```

**Pass `publish: true` from a caller like this one specifically — never as a blanket
setting in your normal `release.yml` stub.** `release.yml`'s own `on:` also includes
`release`, and the overwrite guard runs whenever `push` **or** `publish: true` is true.
If `publish: true` were set unconditionally there, **every** `release`-triggered run
would hit the guard and fail, because the release that triggered the run already exists
— the guard would refuse to overwrite it. Every consumer shares this same `@v1`, so keep
`publish: true` scoped to the automated caller that actually needs it.

### Authoring release notes for an automated release

By default, a `push` or `publish: true` run has no authored notes to draw on — only the
GitHub-generated ones (`generate_release_notes: true`, unchanged). `notes-file` lets a
caller supply real, authored notes (a `## Migration` section for a breaking change,
say) on that same path, without needing a human to open a GitHub Release:

```yaml
  release:
    needs: bump-and-release
    permissions:
      contents: write
    uses: brooswit-minecraft/schematic/.github/workflows/reusable-release.yml@v1
    with:
      publish: true
      ref: ${{ needs.bump-and-release.outputs.sha }}
      notes-file: CHANGELOG.md
    secrets: inherit
```

When set on the `push`/`publish: true` path, the file's contents become **both**:

- the GitHub release body, with the generated notes still appended after (authored
  notes first, generated notes after — the file's text is unmodified, GitHub's own
  `generate_release_notes` behaviour just appends after whatever `body`/`body_path` it's
  given);
- the Modrinth changelog for that release. A plain `workflow_dispatch` run *without*
  `publish: true` is a dry run in the `push`/`publish: true` sense too: `notes-file` is
  neither read nor validated there (its changelog stays empty, exactly as before this
  input existed), since the same `push || publish: true` gate governs `notes-file` as
  governs the overwrite guard and generated notes above. A `publish: true` run — whether
  triggered by `push` or by `workflow_dispatch` — does read the file, for both the
  GitHub release body and the Modrinth changelog. Nothing here previews the changelog
  without actually publishing.

**A caller that only conditionally has notes to pass** (e.g. a step that produces a
notes file solely for a breaking change) can wire `notes-file` straight from that step's
output — an empty output is silent and behaves exactly like leaving `notes-file` unset,
not an error:

```yaml
    with:
      publish: true
      ref: ${{ needs.bump-and-release.outputs.sha }}
      notes-file: ${{ steps.maybe-write-notes.outputs.path }}
```

**On a `release` event, `notes-file` is ignored entirely** — not read, not validated,
never fails the run. The human-authored GitHub Release body remains the only source for
both the release and the Modrinth changelog there, exactly as before this input existed.

**A non-empty `notes-file` that isn't usable fails the run loudly**, before
`actions/setup-go` and before anything is built or published: the path is missing, is a
directory, is an empty file, or resolves outside the checkout (via `../`, an absolute
path, or a symlink) — the "Validate notes-file" step's `::error::` message says which.
This check runs whether or not Modrinth is configured for the caller.

## Deploying to a Modrinth Server

`.github/workflows/server-update.yml` runs once the `Release` workflow above has
finished — chained via `workflow_run`, so it only starts after a release has actually
published to Modrinth — or on demand via `workflow_dispatch` (with an optional
`version` input; it otherwise falls back to the `version` field in `pack.toml`). See
[Migrating an existing stub](#migrating-an-existing-stub) below if your own
`server-update.yml` still uses the older `release: published` trigger — it keeps
working unchanged, and switching is optional.

The default route installs the just-published version through Modrinth Hosting's
catalog API. A project awaiting moderation is absent from that catalog and must use
the explicit `sftp` route described below. That route verifies the running server
over RCON, atomically uploads the exact `.mrpack` contents from the corresponding
GitHub Release, and restarts Minecraft through a repository-managed startup
supervisor. The final
gate requires RCON to go offline and return authenticated, so a green deployment
represents the whole lifecycle.

One-time setup:

1. Buy a Modrinth Server.
2. Install the pack once from your Modrinth project, or configure the `sftp` route
   while a new project is awaiting moderation.
3. Find the server's id: it's the UUID in the dashboard URL
   `modrinth.com/hosting/manage/<server_id>`. It's also returned as `server_id` by
   `rinth servers list` (or `GET https://archon.modrinth.com/modrinth/v0/servers`).
4. Set the `MODRINTH_SERVER_ID` and `MODRINTH_PROJECT_ID` repo variables (and
   `MODRINTH_TOKEN`, if you haven't already set it for publishing).

This **skips cleanly** (the workflow still finishes green) when `MODRINTH_SERVER_ID` /
`MODRINTH_TOKEN` aren't configured, so this works out of the box on a fresh clone — you
opt in by adding the variable/secret above whenever you're ready. Once configured,
`MODRINTH_PROJECT_ID` is required too — the workflow fails loudly rather than skipping
if it's missing. The workflow looks up the published version via the
[`rinth`](https://github.com/brooswit-minecraft/rinth) CLI, invoked at a pinned version
through `bunx` so nothing needs installing in your repo, then performs the install
described above.

That same pinned rinth CLI performs the version lookup, **authenticated with
`MODRINTH_TOKEN`**: a Modrinth project stays a draft — invisible to unauthenticated
reads — until Modrinth moderation approves it, so without authentication a
first-release consumer could never be followed. The lookup is bounded to about 5
minutes before failing, now expressed as rinth's own `--wait` budget rather than a
hand-rolled retry loop — on the chained `workflow_run` trigger and on any other
automated direct call (see below) that's just insurance against a lag between
Modrinth's publish and that version becoming visible over its API; on the older
`release: published` trigger it's still doing its original job, since that trigger
races the release workflow with no ordering guarantee between them. The one path
that does **not** wait is `workflow_dispatch` (a manual re-point): there the version
is expected to already exist, so a missing version fails immediately rather than
spending up to 5 minutes confirming its absence. The ~5 minute bound was checked
against a real chained release and kept as-is; any consumer still on
`release: published` (like schematic-example) depends on that same bound today.
The `sftp` route (below) mirrors this with its own 21-attempt/15s-interval loop,
gated the same way.

### Updating the server from an automated workflow

Like `reusable-release.yml` (see [above](#releasing-from-an-automated-workflow-with-github_token)),
`reusable-server-update.yml` accepts an optional `ref` input for a caller that can't
rely on `github.sha` being the commit it just released:

| Input | Type | Default | Effect |
|---|---|---|---|
| `ref` | string | `''` | Checks out this ref/SHA instead of the caller's own triggering ref, in both the `modrinth-api` and `sftp` jobs (not the `sftp` job's separate checkout of this repo's own `v1` deploy scripts, which is unrelated to the caller's release). Also determines what commit the `pack.toml` fallback reads when `version` is not given. Leave empty for unchanged behaviour, matching `reusable-release.yml`'s `ref` input. |
| `backup` | boolean | `false` | `sftp` route only: archive the world before uploading, with retention. See [Pre-deploy world archive](#pre-deploy-world-archive-opt-in) below. Rejected loudly (before any change) if the resolved route is `modrinth-api`, which has no SFTP access to archive over. |
| `backup-retention` | number | `5` | How many recent world archives to keep once `backup` is `true`. Must be a positive integer; `0` is rejected, not treated as "unlimited". Ignored when `backup` is `false`. |

An automated caller that calls this workflow directly right after publishing a release
— rather than relying on the `workflow_run` chain — has an `event_name` of its own
(e.g. `repository_dispatch`) that is none of `release`, `workflow_run`, or
`workflow_dispatch`. That path gets the same `--wait`/retry tolerance as `release` and
`workflow_run` (see above) automatically: every event except `workflow_dispatch`
waits. Pass `ref` so the version-resolution fallback reads the commit that was
actually released, not the commit before it:

```yaml
  server-update:
    needs: bump-and-release
    uses: brooswit-minecraft/schematic/.github/workflows/reusable-server-update.yml@v1
    with:
      ref: ${{ needs.bump-and-release.outputs.sha }}
    secrets: inherit
```

### The stub pattern

This repo's own `server-update.yml` is the reference implementation of the chained
stub:

```yaml
name: Server update

on:
  workflow_run:
    workflows: ["Release"]
    types: [completed]
  workflow_dispatch:
    inputs:
      version:
        description: 'Version to announce (defaults to the pack.toml version)'
        required: false
        type: string

permissions:
  contents: read

jobs:
  server-update:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event.workflow_run.conclusion == 'success' && github.event.workflow_run.event == 'release')
    uses: brooswit-minecraft/schematic/.github/workflows/reusable-server-update.yml@v1
    with:
      version: ${{ inputs.version }}
    secrets: inherit
```

Two things worth calling out:

- `workflows: ["Release"]` matches the **display name** (`name: Release` in
  release.yml), not the filename `release.yml`. If you ever rename that `name:`
  field, update this list too, or the chain silently stops firing.
- The `if:` guard admits two different runs: this workflow's own manual
  `workflow_dispatch` (for a re-point attempt with no new release involved), and a
  `workflow_run` whose upstream Release run both succeeded and was itself triggered by
  `release` — not by release.yml's own `workflow_dispatch` dry-run path, which
  deliberately creates no GitHub Release and must not trigger a server update.

### Migrating an existing stub

**This is optional and non-breaking.** `reusable-server-update.yml@v1` keeps
supporting the old `release: published` trigger forever — that's the whole point of
`@v1` being backwards-compatible — so an existing stub can stay exactly as it is. Move
to the `workflow_run` pattern above only when it's convenient.

If you do migrate, know this going in:

- **A `workflow_run` trigger only arms once the stub is on your repo's default
  branch.** GitHub will not fire it from a pull request branch, so you cannot test the
  chaining itself before merging — you can still validate the YAML statically, but
  proving the chain fires end-to-end has to happen after the stub lands on `main` (or
  your default branch) and a real release runs.
- `workflows: ["Release"]` is matched by the upstream workflow's `name:` field, not
  its filename — see above.
- A dry-run `workflow_dispatch` of `release.yml` (see [Releasing](#releasing) above)
  deliberately does not create a GitHub Release, so it will not trigger a chained
  server update either — that's intended, not a gap.

## Working on the pack

```sh
packwiz modrinth add <slug>     # e.g. packwiz modrinth add jei
packwiz remove <name>           # e.g. packwiz remove jei
```

Both commands update `index.toml` for you. Commit the resulting `mods/<name>.pw.toml`
along with the changed `index.toml` and `pack.toml` (`packwiz refresh` writes the new
index hash into `pack.toml`'s `[index]` block) — **CI fails if the index does not
match what is on disk.**

packwiz also has `packwiz curseforge add` and `packwiz url add` if a mod is not on
Modrinth. Note that `packwiz modrinth export` restricts downloads to domains Modrinth
allows, so URL-sourced mods may not be exportable.

```sh
make check     # fails if the committed index.toml is stale
make refresh   # rewrite index.toml after changing files by hand
make build     # -> build/<name>-<version>.mrpack
make clean     # remove build/ and bin/
```

### Prerequisites

- [packwiz](https://packwiz.infra.link) — the pack manager. It publishes no tagged
  releases, so this repo pins a commit SHA (`PACKWIZ_REF` in the `Makefile`).
- [Go](https://go.dev) 1.23+, only if you want `make tools` to install that pinned
  packwiz for you. If you already have a packwiz on your `PATH`, it is used instead.

```sh
make tools     # installs the pinned packwiz into ./bin (needs Go)
```

### What is in the repo

```
pack.toml                           pack metadata — name, version, Minecraft and Forge versions
index.toml                          generated file list with hashes; do not edit by hand
mods/                                one *.pw.toml file per mod, pinning a version and its hash — empty by default
.packwizignore                      repo files (docs, CI, Makefile) kept out of the pack
.github/workflows/ci.yml            validates the index and builds the .mrpack on every push
.github/workflows/release.yml       cuts a release (see Releasing above)
.github/workflows/server-update.yml installs each release on a Modrinth-hosted server (see Deploying to a Modrinth Server above)
.github/workflows/tag-v1.yml        template-only (see Template-only files above)
.github/workflows/tests.yml         template-only (see Template-only files above)
.github/workflows/reusable-*.yml    template-only (see Template-only files above)
Makefile                            the build entry point, shared by humans and CI
```

No jars are committed — `.pw.toml` files reference downloads by URL and hash, and
packwiz fetches them at export time.

### CI

`.github/workflows/ci.yml` runs on every push to `main` and on every pull request. It
installs the pinned packwiz, fails if `packwiz refresh` produces a diff, builds the
pack, and uploads the resulting `.mrpack` as a workflow artifact.

## Removing a path you don't need

- No Modrinth publishing or releases? Delete `.github/workflows/release.yml`, the
  [Releasing](#releasing) section above, and the `MODRINTH_TOKEN` /
  `MODRINTH_PROJECT_ID` rows in the [secrets & variables](#secrets--variables) table.
- No server to keep in sync? Delete `.github/workflows/server-update.yml`, the
  [Deploying to a Modrinth Server](#deploying-to-a-modrinth-server) section, and the
  `MODRINTH_SERVER_ID` row in the [secrets & variables](#secrets--variables) table
  above.

## Modrinth project metadata

Put structured Modrinth listing fields in `.modrinth/project.json` and the
formatted long description in `.modrinth/description.md`. A push to `main`
that changes either file runs `modrinth-sync.yml`, PATCHes only the declared
fields, and verifies them by reading the project back. The project id comes
from `MODRINTH_PROJECT_ID`; authentication uses `MODRINTH_TOKEN`.

```json
{
  "title": "My Pack",
  "slug": "my-pack",
  "description": "A short Modrinth summary.",
  "categories": ["technology", "multiplayer"],
  "client_side": "required",
  "server_side": "required",
  "license_id": "MIT",
  "source_url": "https://github.com/example/my-pack",
  "issues_url": "https://github.com/example/my-pack/issues"
}
```

The schema deliberately excludes moderation status, permissions, members,
monetization, gallery media, and deletion. Manage those exceptional controls
in Modrinth rather than granting routine CI authority over them.
# Server Deployment Method

Repository variable `SERVER_DEPLOY_METHOD` selects `modrinth-api` (the default
when unset) or `sftp`. Selection is explicit: API errors do not trigger SFTP.
The existing Rinth/API behavior and credentials below are unchanged. Consumers
such as Sickos calling `reusable-server-update.yml@v1` need no stub changes once
this implementation is released on upstream `v1`; local edits alone do not
update that shared tag. The SFTP job fetches its helper scripts from upstream
Schematic `v1`, not the consumer's checkout.

## SFTP Live-Deployment Configuration

Set these in the **consumer** repository. Missing RCON, SFTP, or Hosting supervisor
configuration is an error, not a successful skip. The workflow validates the live
game and credentials before modifying files. No manual stopped-server acknowledgement
is used.

| Kind | Name | Contract |
| --- | --- | --- |
| Variable | `SERVER_DEPLOY_METHOD` | `sftp` to enable uploads; unset or `modrinth-api` keeps the existing path |
| Variable | `SERVER_RCON_HOST` | Public hostname or address for the Minecraft server's RCON allocation |
| Variable | `SERVER_RCON_PORT` | RCON port, default `25575` |
| Variable | `SERVER_RCON_SHUTDOWN_TIMEOUT` | Seconds to wait for each RCON restart phase, default `120`, maximum `600` |
| Secret | `SERVER_RCON_PASSWORD` | Password matching `rcon.password` in `server.properties` |
| Variable | `SERVER_SFTP_HOST` | Hosting provider's SFTP hostname |
| Variable | `SERVER_SFTP_PORT` | SFTP port, default `22` |
| Variable | `SERVER_SFTP_PATH` | Existing absolute server root in the SFTP account's filesystem/chroot |
| Secret | `SERVER_SFTP_USERNAME` | SFTP account username |
| Secret | `SERVER_SFTP_PASSWORD` | Password; set exactly one of password/private key |
| Secret | `SERVER_SFTP_PRIVATE_KEY` | Unencrypted SSH private key, alternative to password |
| Secret | `SERVER_SFTP_KNOWN_HOSTS` | OpenSSH known_hosts entry verified out of band with the host/provider; nonstandard ports use `[host]:port` |
| Variable | `MODRINTH_SERVER_ID` | Existing Modrinth Hosting server whose startup command is managed |
| Secret | `MODRINTH_TOKEN` | Credential used by Rinth to synchronize the Hosting startup command |

Enable RCON in `server.properties`, expose its allocated port, and configure the
same password as `SERVER_RCON_PASSWORD`. The deploy helper first requires an
authenticated RCON response, promotes release-owned files atomically, and installs
`.schematic-supervisor.sh`. Rinth synchronizes the world's Hosting startup command
to that wrapper. The workflow then flushes the world and sends `stop`; the wrapper
relaunches Minecraft after five seconds. The final gate must observe RCON become
unavailable and then return an authenticated response before the job succeeds; an
authentication failure is never interpreted as an outage. Rinth is pinned to the
reviewed release containing that command. The first migration from a plain `run.sh`
startup needs one Hosting-panel restart after CI installs and selects the wrapper;
later deployments are unattended. Do not run another deployment
controller or modify server files during this sequence.

The source is the single `.mrpack` attached to the exact GitHub Release, not the
default branch or a fresh packwiz resolution. Release/chained triggers wait up to
300 seconds for its asset; manual runs require an existing release (version
`1.2.3` selects tag `v1.2.3`). The caller's read-only `GITHUB_TOKEN` downloads the
asset, including from private repositories. Multiple pack assets are rejected.
The helper checks `versionId`, SHA-1, SHA-512 and file sizes; includes server
required/optional files; excludes client-only files; applies `overrides` then
`server-overrides`. Downloads require HTTPS, including redirects.

Files are uploaded into a unique staging directory and read back for hash
verification. The SFTP server must support the OpenSSH POSIX rename extension
for atomic per-file overwrite. **The whole deployment is not atomic.** Only
files recorded in `.schematic-deploy.json` are eligible for stale-file removal;
there is no mirror-delete of the server root. Symlinks, protected server data,
untracked mod files, and changed/untracked file collisions are refused. For a
first migration, back up and remove old unmanaged mods and conflicting configs
yourself; worlds and other unmanaged data are not deployment inputs. Pack paths
containing hidden components are deliberately unsupported.

A failed upload leaves `.schematic-deploy-pending` as a lock and may leave a
`.schematic-stage-*` directory. Stop the server before inspecting or restoring the
backup and manifest, then remove the marker/staging directory manually before
retrying. Never clear the marker automatically after a partial promotion.
Unknown host keys are rejected; do not replace verification with `ssh-keyscan`
trust-on-first-use. Downloaded pack contents must come from trusted releases.

### Pre-deploy world archive (opt-in)

`sftp`-route only, off by default, over the SFTP access the route already has — no
new credentials. Enable it on a consumer's stub with:

```yaml
    with:
      backup: true
      backup-retention: 5   # optional; 5 is also the default when omitted
```

**When it runs.** After the world is quiesced (RCON `save-off` then `save-all
flush`) and before any pack file is uploaded — see the "Archive world before
upload" step in `reusable-server-update.yml`, which runs strictly before the
step that uploads pack files. `save-on` is issued afterwards via
`try`/`finally`, even when the archive fails, so an ordinary failure (or a
Ctrl-C/cancellation) can never leave autosave disabled on the live world.
**Caveat:** nothing running in Python can trap a SIGKILL or the runner
disappearing mid-archive — that residual case can still leave autosave off;
if a run ends that way, send `save-on` by hand over RCON before trusting the
world's autosave state again.

**What is archived.** The world directories only — never logs, jars, the pack,
or anything else in `SERVER_SFTP_PATH`. The level name is read from the remote
`server.properties`' `level-name` (default `world`) and validated (no `..`, no
path separators, no hidden/absolute names, no symlinks anywhere inside); the
three directories `<level>`, `<level>_nether` and `<level>_the_end` are
included whenever each exists (a brand-new world may not have generated the
nether/end yet — that is not an error). A file that grows between being
listed and being read is captured only up to the size seen at listing time
(the archive format requires declaring each entry's size up front) — with
`save-off` in effect this should be rare, but the archive is not guaranteed
byte-exact for a file actively being written at the moment of archiving.

**Where it goes, and the naming scheme.** A single `tar.gz` per run, written to
`<SERVER_SFTP_PATH>/backups/` (already excluded from pack management — see
`PROTECTED` in `scripts/server_deploy.py`) as:

```
<level>-<UTC timestamp>-<tag>.tar.gz          e.g. world-20260920T063000Z-1.2.3.tar.gz
```

The timestamp format sorts lexicographically in chronological order. It is
uploaded to a temporary staged name first (`.schematic-backup-stage-<uuid>`),
and only promoted to its real name (atomic rename) after the staged copy's
size and hash are read back and verified — a corrupt or interrupted upload is
never promoted, and never counts against retention. If the upload, the
verification, or the rename itself fails partway (connection drop, the
staging step's channel timeout, the host running out of disk), the partial
staged file is removed as part of handling that failure; if a run is killed
before it can even do that (SIGKILL, runner loss), the **next** run sweeps
any pre-existing staged file it finds before starting its own archive —
safe because the workflow's own `concurrency` group serializes every
`sftp`-route run against a given consumer, so there is never a second run
concurrently writing one.

**Retention.** `backup-retention` (default `5`) is the number of most recent
archives to keep; older ones matching this feature's own naming pattern for
the same level are deleted — nothing else under `backups/` is ever touched,
and pruning only happens after the new archive is written and verified, so a
failed run can never shrink the number of good backups. `backup-retention`
must be a positive integer; `0` is rejected outright, not treated as
"unlimited".

**Failure behaviour.** An archive failure with `backup: true` **aborts the
deploy before any upload** — the archive step has no `continue-on-error` or
`if: always()`, so the job simply fails there. This is deliberate: a silent
skip would defeat the purpose of an explicitly-requested backup, and nothing
has been uploaded yet at that point, so aborting costs nothing beyond the
backup itself not existing this run — a re-run is always safe.

**The `modrinth-api` route** has no SFTP access at all, so it cannot archive
anything. Setting `backup: true` without `SERVER_DEPLOY_METHOD: sftp` is
rejected loudly by the "Validate deployment selector" job step, before either
route does anything — never a silent no-op that would give false confidence a
backup was taken.

**Restoring an archive by hand.** There is no automated restore — do this:

1. Stop the Minecraft server first (e.g. RCON `stop`, or via the Hosting
   panel). Restoring into a running world will corrupt it.
2. The archive lives at `<SERVER_SFTP_PATH>/backups/<level>-<timestamp>-<tag>.tar.gz`
   on the SFTP host; download it (e.g. `sftp` or `scp`) to wherever you'll
   extract it.
3. Move the existing (possibly broken) world directories aside rather than
   deleting them outright, in case something is still needed from them:
   `mv <level> <level>.broken` (repeat for `<level>_nether`/`<level>_the_end`
   if present).
4. Extract the archive at the server root: `tar -xzf <archive> -C
   <SERVER_SFTP_PATH>/`. It expands directly into `<level>/`,
   `<level>_nether/` and `<level>_the_end/` (whichever were present at backup
   time) — no extra path prefix to strip.
5. **Ownership/permissions caveat:** the archive was built by the deploy
   runner reading over SFTP, so extracted files may not match the server
   process's expected owner/group or the original file permissions exactly
   (world files were `0o644` inside the tar; directories default to your
   extracting tool's umask). Fix ownership/permissions to match what the
   Minecraft process expects on that host before starting it (e.g. `chown -R`
   to the service user) — an SFTP-chrooted account often doesn't need this,
   but verify for your host.
6. Restart the server and verify: confirm it starts without a "failed to load
   level" error, `list`/log in-game to confirm the expected spawn point and a
   sampling of known builds are present. Only delete the `.broken` directories
   once you've confirmed the restore is good.

**Disk usage.** Each archive is a compressed copy of the world at that moment,
so archive size scales with world size (compression ratio varies with how much
of the world is already-compressed region data vs. NBT/metadata — do not
assume a fixed ratio). With the default retention of `5`, worst-case steady-state
usage is roughly **5× the compressed world size**, and it accumulates on the
**same host as the world itself** — this protects against a bad plugin/mod
corrupting the world, but a `backups/` directory living on the same disk as
the live world is not protection against loss of that host. Size
`backup-retention` (and monitor host disk) accordingly for large worlds.

Local verification (no network or server credentials — `tests/test_server_backup_loopback.py`
exercises the backup path over a real SFTP/RCON protocol pair, but entirely on
127.0.0.1 loopback sockets with made-up test credentials, never a real host):

```sh
python3 -m unittest discover -s tests -v
```

`test_server_backup_loopback.py` needs `paramiko` installed (`pip install
paramiko==4.0.0`, matching the version the workflow itself installs) — it is
skipped, not failed, when paramiko isn't available, so the rest of the suite
is unaffected either way.
