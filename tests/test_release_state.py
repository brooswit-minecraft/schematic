"""MINECRAFT-371: offline tests for scripts/release_state.py.

Covers input validation and every one of the six contract states, plus the
retry/overlap scenarios DoD #2 requires: a repeat call against an
already-published version must reuse (state=skipped), and a tag pointing at
a different SHA must fail loudly rather than silently repoint the release.
"""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/release_state.py"
SPEC = importlib.util.spec_from_file_location("release_state", SCRIPT)
rs = importlib.util.module_from_spec(SPEC)
sys.modules["release_state"] = rs  # dataclass() needs the module registered
SPEC.loader.exec_module(rs)

SHA_A = "a" * 40
SHA_B = "b" * 40


class ValidateSha(unittest.TestCase):
    def test_accepts_full_lowercase_sha(self):
        self.assertEqual(rs.validate_sha(SHA_A), SHA_A)

    def test_rejects_trailing_newline_sha(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_sha(SHA_A + "\n")

    def test_rejects_short_sha(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_sha(SHA_A[:7])

    def test_rejects_uppercase(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_sha(SHA_A.upper())

    def test_rejects_branch_name(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_sha("main")

    def test_rejects_non_string(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_sha(None)


class ValidateVersion(unittest.TestCase):
    def test_accepts_plain_semver(self):
        self.assertEqual(rs.validate_version("1.2.3"), "1.2.3")

    def test_rejects_trailing_newline_version(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_version("1.2.3\n")

    def test_strips_leading_v(self):
        self.assertEqual(rs.validate_version("v1.2.3"), "1.2.3")

    def test_accepts_prerelease(self):
        self.assertEqual(rs.validate_version("1.2.3-beta.1"), "1.2.3-beta.1")

    def test_rejects_empty(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_version("")

    def test_rejects_non_semver(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_version("latest")

    def test_rejects_missing_patch(self):
        with self.assertRaises(rs.ContractError):
            rs.validate_version("1.2")


def decide(**overrides):
    base = dict(
        sha=SHA_A,
        authorized=True,
        hold=False,
        modrinth_required=True,
        modrinth_configured=True,
        existing_tag=None,
        existing_release=None,
        existing_modrinth_version=None,
    )
    base.update(overrides)
    return rs.decide(**base)


class DecideStates(unittest.TestCase):
    def test_unauthorized_is_build_only(self):
        d = decide(authorized=False)
        self.assertEqual(d.state, "build-only")
        self.assertEqual(d.release_action, "none")
        self.assertEqual(d.modrinth_action, "none")

    def test_unauthorized_wins_over_hold(self):
        # authorized=false is checked first regardless of hold's value.
        d = decide(authorized=False, hold=True)
        self.assertEqual(d.state, "build-only")

    def test_authorized_and_held_is_held(self):
        d = decide(authorized=True, hold=True)
        self.assertEqual(d.state, "held")
        self.assertEqual(d.release_action, "none")

    def test_missing_required_modrinth_config_fails_actionably(self):
        d = decide(modrinth_required=True, modrinth_configured=False)
        self.assertEqual(d.state, "failed")
        self.assertIn("MODRINTH_TOKEN", d.error)

    def test_missing_optional_modrinth_config_still_publishes(self):
        d = decide(modrinth_required=False, modrinth_configured=False)
        self.assertEqual(d.state, "publishing")
        self.assertEqual(d.release_action, "create")
        self.assertEqual(d.modrinth_action, "none")

    def test_fresh_release_and_modrinth_is_publishing(self):
        d = decide()
        self.assertEqual(d.state, "publishing")
        self.assertEqual(d.release_action, "create")
        self.assertEqual(d.modrinth_action, "create")

    def test_sha_mismatch_on_existing_tag_fails_loudly(self):
        # Metadata/actual-tag disagreement: the tag itself is the source of
        # truth, regardless of whether a Release object exists or what it
        # claims.
        d = decide(existing_tag={"sha": SHA_B}, existing_release=None)
        self.assertEqual(d.state, "failed")
        self.assertIn(SHA_B, d.error)
        self.assertIn(SHA_A, d.error)
        self.assertEqual(d.release_action, "none")

    def test_sha_mismatch_fails_even_with_a_matching_release_object(self):
        # A Release object's own cached metadata must never substitute for
        # the tag's actual peeled commit when they disagree.
        d = decide(
            existing_tag={"sha": SHA_B},
            existing_release={"id": "1", "url": "u"},
        )
        self.assertEqual(d.state, "failed")

    def test_existing_tag_without_release_object_still_creates_release(self):
        # A tag was pushed (e.g. manually) but no GitHub Release exists yet
        # for it. The tag matches the requested sha, so this is safe to
        # proceed: create the Release object (GitHub will attach it to the
        # existing tag, ignoring target_commitish, since the tag already
        # exists at the right commit).
        d = decide(existing_tag={"sha": SHA_A}, existing_release=None)
        self.assertEqual(d.state, "publishing")
        self.assertEqual(d.release_action, "create")

    def test_full_replay_of_completed_publish_is_skipped(self):
        # Retry/overlap (DoD #2): tag, release AND modrinth version already
        # exist for this exact sha/version — no mutation, existing identity
        # returned.
        d = decide(
            existing_tag={"sha": SHA_A},
            existing_release={"id": "123", "url": "https://x/123"},
            existing_modrinth_version={"id": "mv1"},
        )
        self.assertEqual(d.state, "skipped")
        self.assertEqual(d.release_action, "reuse")
        self.assertEqual(d.modrinth_action, "reuse")
        self.assertEqual(d.release_id, "123")
        self.assertEqual(d.modrinth_version_id, "mv1")

    def test_release_exists_but_modrinth_missing_resumes_publishing(self):
        # Simulates a retry after a prior run created the GH Release but
        # failed before Modrinth completed: reconcile reuses the release
        # and still attempts the Modrinth publish, rather than recreating
        # the release or silently skipping Modrinth.
        d = decide(
            existing_tag={"sha": SHA_A},
            existing_release={"id": "123", "url": "https://x/123"},
            existing_modrinth_version=None,
        )
        self.assertEqual(d.state, "publishing")
        self.assertEqual(d.release_action, "reuse")
        self.assertEqual(d.modrinth_action, "create")

    def test_overlap_two_calls_same_sha_second_is_skipped_not_duplicated(self):
        first = decide(existing_tag=None, existing_release=None, existing_modrinth_version=None)
        self.assertEqual(first.release_action, "create")
        # First call's effects observed by the second (overlapping/retried) call:
        second = decide(
            existing_tag={"sha": SHA_A},
            existing_release={"id": "1", "url": "u"},
            existing_modrinth_version={"id": "mv"},
        )
        self.assertEqual(second.state, "skipped")

    def test_overlap_two_calls_different_sha_second_fails_without_mutation(self):
        # Overlapping calls for the SAME version but DIFFERENT shas: the
        # first to land wins the tag; the second must fail rather than
        # repoint it.
        first = decide(sha=SHA_A, existing_tag=None)
        self.assertEqual(first.release_action, "create")
        second = decide(sha=SHA_B, existing_tag={"sha": SHA_A})
        self.assertEqual(second.state, "failed")
        self.assertEqual(second.release_action, "none")


class DecisionJson(unittest.TestCase):
    def test_to_json_omits_error_when_absent(self):
        d = decide(authorized=False)
        self.assertNotIn("error", d.to_json())

    def test_to_json_blanks_missing_identities(self):
        d = decide(authorized=False)
        j = d.to_json()
        self.assertEqual(j["release_id"], "")
        self.assertEqual(j["modrinth_version_id"], "")


class CliValidate(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
        )

    def test_validate_success_exit_zero(self):
        p = self.run_cli("validate", "--sha", SHA_A, "--version", "1.0.0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"sha": SHA_A, "version": "1.0.0"})

    def test_validate_bad_sha_exit_one_actionable(self):
        p = self.run_cli("validate", "--sha", "deadbeef", "--version", "1.0.0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("error", json.loads(p.stdout))

    def test_decide_via_stdin(self):
        facts = {
            "sha": SHA_A,
            "authorized": True,
            "hold": False,
            "modrinth_required": True,
            "modrinth_configured": True,
            "existing_tag": None,
            "existing_release": None,
            "existing_modrinth_version": None,
        }
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "decide", "--facts", "-"],
            input=json.dumps(facts),
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["state"], "publishing")

    def test_decide_failed_state_exits_nonzero(self):
        facts = {
            "sha": SHA_A,
            "authorized": True,
            "hold": False,
            "modrinth_required": True,
            "modrinth_configured": False,
            "existing_tag": None,
            "existing_release": None,
            "existing_modrinth_version": None,
        }
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "decide", "--facts", "-"],
            input=json.dumps(facts),
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
