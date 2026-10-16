"""MINECRAFT-371: regression proving the reusable workflow's mechanism
checkout actually works for a consumer repo that has none of schematic's
own files.

John Winstead's review of PR #62 (finding 1) required a code-only
regression/fixture proving "a consumer without scripts/release_state.py
resolves and executes the actual helper" — not just a structural argument
about the workflow YAML. This test reproduces exactly what
`.github/workflows/reusable-accepted-merge-release.yml`'s "Check out
schematic mechanism helper" step does (an `actions/checkout` of
brooswit-minecraft/schematic at a pinned ref, into `.schematic-mechanism/`,
inside the consumer's own job workspace) using plain `git`, against a
throwaway "consumer" directory that contains NOTHING from schematic — no
scripts/, no pack.toml, no Makefile, nothing — then invokes the checked-out
helper exactly as the workflow does (`python3
.schematic-mechanism/scripts/release_state.py ...`, cwd'd into the
consumer directory) and asserts it resolves and runs correctly.

No network access: schematic's own git history (this repository) is used
as the "remote" via a local clone, pinned to this repo's own current HEAD
— which, since scripts/release_state.py is tracked in this very commit, is
a real, verifiable revision containing the helper (exactly the kind of
revision schematic-ref must be pinned to; see
docs/accepted-merge-release-contract.md).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
SHA_A = "a" * 40


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


class ConsumerWithoutHelperCanRunIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.consumer = Path(self.tmp.name) / "consumer-repo"
        self.consumer.mkdir()
        # Prove the premise: this "consumer" has nothing of schematic's own.
        self.assertFalse((self.consumer / "scripts").exists())
        self.assertFalse((self.consumer / "pack.toml").exists())

        # The exact revision scripts/release_state.py is tracked at in THIS
        # repo — a real, verifiable commit (not a hypothetical), the same
        # kind of value a caller pins `schematic-ref` to.
        self.helper_sha = git("rev-parse", "HEAD", cwd=REPO_ROOT).stdout.strip()
        self.assertRegex(self.helper_sha, r"^[0-9a-f]{40}$")

    def _checkout_mechanism_helper(self):
        """Reproduces the workflow's "Check out schematic mechanism helper"
        step: clone schematic at the pinned ref into
        <consumer>/.schematic-mechanism, using only git — no assumption
        that the consumer tree has anything else in it.
        """
        mechanism_dir = self.consumer / ".schematic-mechanism"
        git("clone", "--quiet", str(REPO_ROOT), str(mechanism_dir))
        git("checkout", "--quiet", self.helper_sha, cwd=mechanism_dir)
        return mechanism_dir

    def test_helper_is_absent_from_the_consumer_before_checkout(self):
        self.assertFalse((self.consumer / ".schematic-mechanism").exists())

    def test_mechanism_checkout_resolves_the_real_helper(self):
        mechanism_dir = self._checkout_mechanism_helper()
        helper = mechanism_dir / "scripts" / "release_state.py"
        self.assertTrue(helper.is_file())
        # It's the real file, not a stand-in: same content as this repo's.
        self.assertEqual(
            helper.read_text(), (REPO_ROOT / "scripts/release_state.py").read_text()
        )

    def test_consumer_executes_the_checked_out_helper_exactly_as_the_workflow_does(self):
        self._checkout_mechanism_helper()
        # Exactly the invocation the workflow's "Validate inputs" step uses,
        # run with cwd the consumer repo's own root — proving execution
        # works from a tree that has nothing of schematic's own in it
        # except the just-checked-out .schematic-mechanism/ subdirectory.
        proc = subprocess.run(
            [
                sys.executable,
                ".schematic-mechanism/scripts/release_state.py",
                "validate",
                "--sha",
                SHA_A,
                "--version",
                "v1.2.3",
            ],
            cwd=self.consumer,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"sha": SHA_A, "version": "1.2.3"})

    def test_consumer_can_also_run_the_decide_subcommand(self):
        self._checkout_mechanism_helper()
        facts = {
            "sha": SHA_A,
            "authorized": False,
            "hold": False,
            "modrinth_required": True,
            "modrinth_configured": False,
            "existing_tag": None,
            "existing_release": None,
            "existing_modrinth_version": None,
        }
        proc = subprocess.run(
            [
                sys.executable,
                ".schematic-mechanism/scripts/release_state.py",
                "decide",
                "--facts",
                "-",
            ],
            cwd=self.consumer,
            input=json.dumps(facts),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["state"], "build-only")

    def test_a_schematic_ref_missing_the_helper_is_caught_before_execution(self):
        # Mirrors the workflow's "Confirm the mechanism helper exists at
        # schematic-ref" step: a ref that resolves but predates the helper
        # (like today's v1 tag) must be caught by a file-existence check,
        # not produce a confusing traceback from the next step.
        #
        # Built as an independent, from-scratch repo (not by walking this
        # repo's own history) so the case holds regardless of whether this
        # test itself runs against a full or a shallow/single-commit
        # checkout of schematic (e.g. CI's default actions/checkout depth).
        no_helper_origin = Path(self.tmp.name) / "no-helper-origin"
        no_helper_origin.mkdir()
        git("init", "--quiet", str(no_helper_origin))
        git("config", "user.email", "test@example.invalid", cwd=no_helper_origin)
        git("config", "user.name", "test", cwd=no_helper_origin)
        (no_helper_origin / "README.md").write_text("pre-helper schematic\n")
        git("add", "README.md", cwd=no_helper_origin)
        git("commit", "--quiet", "-m", "commit predating the helper", cwd=no_helper_origin)

        mechanism_dir = self.consumer / ".schematic-mechanism"
        git("clone", "--quiet", str(no_helper_origin), str(mechanism_dir))
        self.assertFalse((mechanism_dir / "scripts" / "release_state.py").exists())


if __name__ == "__main__":
    unittest.main()
