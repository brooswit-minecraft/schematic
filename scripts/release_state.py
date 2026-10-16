#!/usr/bin/env python3
"""Accepted-merge release contract: input validation and the publication
state machine (MINECRAFT-371 implementing MINECRAFT-44).

Extracted from .github/workflows/reusable-accepted-merge-release.yml so the
decision logic is unit-testable without a GitHub Actions runner. The
workflow resolves the release tag's own PEELED commit sha with `git
rev-parse refs/tags/vVERSION^{commit}` (not a GitHub Release's cached
`target_commitish`, which can exist independently of — or in principle
disagree with — the tag itself), looks up any GitHub Release object with
`gh release view`, and checks Modrinth with `rinth versions latest`, to
gather facts (existing tag / existing release / existing Modrinth version,
or None for "doesn't exist"), then calls `decide()` with those facts to get
back one of six states: build-only, held, skipped, publishing, published,
failed.

`publishing` returned by `decide()` authorizes the workflow's publication
branch. The externally visible publishing checkpoint is written only once
the GitHub Release identity is known, before Modrinth publication. A later
failure retains that checkpoint and identity; earlier failures expose
failed. A retry reconciles against the existing release rather than
recreating it.

CLI usage (both subcommands print one JSON object to stdout and exit 0 on
success; a validation/decision failure prints `{"error": "..."}` and exits
1 so a workflow step can `::error::` it and stop):

    release_state.py validate --sha <40-hex> --version <raw-version>
    release_state.py decide --facts <path-to-json-or-->
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-.+][A-Za-z0-9.-]+)?$")

STATES = ("build-only", "held", "skipped", "publishing", "published", "failed")


class ContractError(ValueError):
    """An actionable input/reconcile failure (never a bug in this script)."""


def validate_sha(sha: str) -> str:
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ContractError(
            f"sha must be an exact 40-character lowercase hex commit SHA, got {sha!r}"
        )
    return sha


def validate_version(raw_version: str) -> str:
    if not isinstance(raw_version, str) or not raw_version:
        raise ContractError("version is required")
    version = raw_version[1:] if raw_version.startswith("v") else raw_version
    if not VERSION_RE.fullmatch(version):
        raise ContractError(
            f"version must be semver-like (e.g. 1.2.3 or 1.2.3-beta.1), got {raw_version!r}"
        )
    return version


@dataclass
class Decision:
    state: str
    release_action: str = "none"  # none | create | reuse
    modrinth_action: str = "none"  # none | create | reuse
    release_id: Optional[str] = None
    release_url: Optional[str] = None
    modrinth_version_id: Optional[str] = None
    error: Optional[str] = None

    def to_json(self) -> dict:
        d = {
            "state": self.state,
            "release_action": self.release_action,
            "modrinth_action": self.modrinth_action,
            "release_id": self.release_id or "",
            "release_url": self.release_url or "",
            "modrinth_version_id": self.modrinth_version_id or "",
        }
        if self.error:
            d["error"] = self.error
        return d


def decide(
    *,
    sha: str,
    authorized: bool,
    hold: bool,
    modrinth_required: bool,
    modrinth_configured: bool,
    existing_tag: Optional[dict],
    existing_release: Optional[dict],
    existing_modrinth_version: Optional[dict],
) -> Decision:
    """`existing_tag`: {"sha": ...} or None — the vVERSION git tag's own
    PEELED commit sha (annotated tags resolved to the commit they point at),
    resolved independently of any GitHub Release object. This, not a
    release's cached `target_commitish` metadata, is the source of truth
    for "does this tag already point somewhere", since a tag can exist
    without a release (or, in principle, disagree with stale release
    metadata).
    `existing_release`: {"id": ..., "url": ...} or None — whether a GitHub
    Release object exists for this tag. Only consulted for reuse-vs-create;
    never for the sha-mismatch check.
    `existing_modrinth_version`: {"id": ...} or None.
    Never raises — a reconcile failure is reported as state=failed.
    """
    if not authorized:
        return Decision(state="build-only")

    if hold:
        return Decision(state="held")

    if existing_tag is not None and existing_tag.get("sha") != sha:
        return Decision(
            state="failed",
            error=(
                f"the release tag already exists at {existing_tag.get('sha')!r}, "
                f"not the requested sha {sha!r} — refusing to repoint an "
                "existing tag/release"
            ),
        )

    release_action = "none"
    release_id = release_url = None
    if existing_release is not None:
        release_action = "reuse"
        release_id = existing_release.get("id")
        release_url = existing_release.get("url")
    else:
        release_action = "create"

    if modrinth_required and not modrinth_configured:
        return Decision(
            state="failed",
            error=(
                "Modrinth publication is required for this call but "
                "MODRINTH_TOKEN/MODRINTH_PROJECT_ID are not configured"
            ),
        )

    modrinth_action = "none"
    modrinth_version_id = None
    if modrinth_configured:
        if existing_modrinth_version is not None:
            modrinth_action = "reuse"
            modrinth_version_id = existing_modrinth_version.get("id")
        else:
            modrinth_action = "create"

    if release_action == "reuse" and modrinth_action in ("none", "reuse"):
        # Nothing new to do: a full replay of a previously-completed
        # publish (DoD #2 — retries/overlapping events must not duplicate).
        return Decision(
            state="skipped",
            release_action=release_action,
            modrinth_action=modrinth_action,
            release_id=release_id,
            release_url=release_url,
            modrinth_version_id=modrinth_version_id,
        )

    return Decision(
        state="publishing",
        release_action=release_action,
        modrinth_action=modrinth_action,
        release_id=release_id,
        release_url=release_url,
        modrinth_version_id=modrinth_version_id,
    )


def _cmd_validate(args: argparse.Namespace) -> dict:
    sha = validate_sha(args.sha)
    version = validate_version(args.version)
    return {"sha": sha, "version": version}


def _cmd_decide(args: argparse.Namespace) -> dict:
    raw = sys.stdin.read() if args.facts == "-" else open(args.facts).read()
    facts = json.loads(raw)
    decision = decide(
        sha=facts["sha"],
        authorized=bool(facts["authorized"]),
        hold=bool(facts["hold"]),
        modrinth_required=bool(facts["modrinth_required"]),
        modrinth_configured=bool(facts["modrinth_configured"]),
        existing_tag=facts.get("existing_tag"),
        existing_release=facts.get("existing_release"),
        existing_modrinth_version=facts.get("existing_modrinth_version"),
    )
    return decision.to_json()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--sha", required=True)
    p_validate.add_argument("--version", required=True)
    p_validate.set_defaults(func=_cmd_validate)

    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--facts", default="-", help="path to a JSON facts file, or - for stdin")
    p_decide.set_defaults(func=_cmd_decide)

    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except ContractError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(result))
    if args.cmd == "decide" and result.get("state") == "failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
