from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("deploy_state", Path(__file__).parents[1] / "scripts/deploy_state.py")
deploy_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_state)


class FakeSFTP:
    """Minimal local-filesystem fake covering only what deploy_state.py
    needs (lstat/open/posix_rename), matching test_server_deploy.py's
    FakeSFTP conventions."""

    def __init__(self, root):
        self.root = root

    def path(self, path):
        return self.root / path.lstrip("/")

    def lstat(self, path):
        return self.path(path).lstat()

    def open(self, path, mode):
        return self.path(path).open(mode)

    def posix_rename(self, source, target):
        self.path(source).replace(self.path(target))


def fixed_now():
    return datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


class DeployStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "server"
        self.root.mkdir()
        self.sftp = FakeSFTP(Path(self.tmp.name))

    def test_parse_version_orders_numerically_not_lexicographically(self):
        self.assertLess(deploy_state.parse_version("0.9.0"), deploy_state.parse_version("0.10.0"))
        self.assertEqual(deploy_state.parse_version("0.53.2"), (0, 53, 2))

    def test_parse_version_rejects_non_numeric(self):
        for bad in ("v1.2.3", "1.2.3-rc1", "", None, "1..2", "1.2."):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                deploy_state.parse_version(bad)

    def test_read_state_absent_is_none(self):
        self.assertIsNone(deploy_state.read_state(self.sftp, "/server"))

    def test_write_then_read_round_trips(self):
        written = deploy_state.write_state(self.sftp, "/server", "0.53.0", deploy_state.UPDATING, now=fixed_now)
        read = deploy_state.read_state(self.sftp, "/server")
        self.assertEqual(read, written)
        self.assertEqual(read["version"], "0.53.0")
        self.assertEqual(read["state"], deploy_state.UPDATING)
        self.assertEqual(read["updated_at"], "2026-10-16T12:00:00+00:00")

    def test_write_state_is_atomic_no_leftover_tmp_file(self):
        deploy_state.write_state(self.sftp, "/server", "0.53.0", deploy_state.HEALTHY, now=fixed_now)
        self.assertFalse(list((self.root).glob(deploy_state.STATE_FILE + ".tmp-*")))

    def test_write_state_rejects_invalid_state(self):
        with self.assertRaises(ValueError):
            deploy_state.write_state(self.sftp, "/server", "0.53.0", "bogus", now=fixed_now)

    def test_write_state_rejects_unparseable_version(self):
        with self.assertRaises(ValueError):
            deploy_state.write_state(self.sftp, "/server", "not-a-version", deploy_state.HEALTHY, now=fixed_now)

    def test_read_state_rejects_corrupt_record(self):
        (self.root / deploy_state.STATE_FILE).write_text("not json")
        with self.assertRaises(Exception):
            deploy_state.read_state(self.sftp, "/server")

    def test_read_state_rejects_invalid_state_value(self):
        (self.root / deploy_state.STATE_FILE).write_text(json.dumps(
            {"schema": 2, "version": "1.0.0", "state": "nonsense", "updated_at": "x", "installed_version": None}))
        with self.assertRaisesRegex(ValueError, "Invalid deployment-state record"):
            deploy_state.read_state(self.sftp, "/server")

    def test_read_state_rejects_old_schema_version(self):
        (self.root / deploy_state.STATE_FILE).write_text(json.dumps(
            {"schema": 1, "version": "1.0.0", "state": "healthy", "updated_at": "x"}))
        with self.assertRaisesRegex(ValueError, "Invalid deployment-state record"):
            deploy_state.read_state(self.sftp, "/server")

    def test_guard_allows_first_ever_deploy(self):
        deploy_state.guard_stale_release(self.sftp, "/server", "0.1.0")  # no exception

    def test_guard_blocks_older_release_over_healthy(self):
        deploy_state.write_state(self.sftp, "/server", "0.53.1", deploy_state.HEALTHY, now=fixed_now)
        with self.assertRaisesRegex(ValueError, "Stale release"):
            deploy_state.guard_stale_release(self.sftp, "/server", "0.53.0")

    def test_guard_blocks_older_release_over_in_flight_update(self):
        deploy_state.write_state(self.sftp, "/server", "0.53.1", deploy_state.UPDATING, now=fixed_now)
        with self.assertRaisesRegex(ValueError, "Stale release"):
            deploy_state.guard_stale_release(self.sftp, "/server", "0.53.0")

    def test_guard_allows_equal_version_retry(self):
        deploy_state.write_state(self.sftp, "/server", "0.53.1", deploy_state.HEALTHY, now=fixed_now)
        deploy_state.guard_stale_release(self.sftp, "/server", "0.53.1")  # no exception

    def test_guard_allows_newer_release(self):
        deploy_state.write_state(self.sftp, "/server", "0.53.1", deploy_state.HEALTHY, now=fixed_now)
        deploy_state.guard_stale_release(self.sftp, "/server", "0.53.2")  # no exception

    def test_guard_allows_any_version_after_failed(self):
        deploy_state.write_state(self.sftp, "/server", "0.53.1", deploy_state.FAILED, now=fixed_now)
        deploy_state.guard_stale_release(self.sftp, "/server", "0.10.0")  # no exception: no live content pinned

    def test_guard_allows_any_version_after_rolled_back(self):
        deploy_state.write_state(self.sftp, "/server", "0.53.1", deploy_state.ROLLED_BACK, now=fixed_now)
        deploy_state.guard_stale_release(self.sftp, "/server", "0.10.0")  # no exception

    def test_guard_compares_numerically_not_lexicographically(self):
        deploy_state.write_state(self.sftp, "/server", "0.9.0", deploy_state.HEALTHY, now=fixed_now)
        deploy_state.guard_stale_release(self.sftp, "/server", "0.10.0")  # 0.10.0 > 0.9.0 numerically

    # --- PR #63 review: installed_version must survive being overwritten
    # by a later attempt's own requested/failed record (regressions from
    # the reviewer's exact scenarios). ---

    def test_installed_version_survives_a_later_requested_overwrite(self):
        deploy_state.write_state(self.sftp, "/server", "2", deploy_state.HEALTHY, now=fixed_now)
        # A newer release being requested overwrites the "current attempt"
        # fields, but must not erase the installed_version floor that "2"
        # already set.
        record = deploy_state.write_state(self.sftp, "/server", "3", deploy_state.REQUESTED, now=fixed_now)
        self.assertEqual(record["installed_version"], "2")

    def test_stale_candidate_rejected_even_after_a_newer_request_overwrote_the_record(self):
        # healthy(2) -> requested(3) -> candidate(1) must still be rejected,
        # even though the "current attempt" by then reads requested/3, not
        # healthy/2 — this was the exact gap PR #63 review found.
        deploy_state.write_state(self.sftp, "/server", "2", deploy_state.HEALTHY, now=fixed_now)
        deploy_state.write_state(self.sftp, "/server", "3", deploy_state.REQUESTED, now=fixed_now)
        with self.assertRaisesRegex(ValueError, "Stale release"):
            deploy_state.guard_stale_release(self.sftp, "/server", "1")

    def test_failed_pre_upload_request_for_a_newer_version_leaves_installed_protected(self):
        # healthy(2) -> failed(3) (e.g. 3 never reached upload()) must still
        # protect 2 — and must still reject a stale candidate below 2.
        deploy_state.write_state(self.sftp, "/server", "2", deploy_state.HEALTHY, now=fixed_now)
        record = deploy_state.write_state(self.sftp, "/server", "3", deploy_state.FAILED, now=fixed_now)
        self.assertEqual(record["installed_version"], "2")
        with self.assertRaisesRegex(ValueError, "Stale release"):
            deploy_state.guard_stale_release(self.sftp, "/server", "1")

    def test_a_stale_jobs_failed_write_cannot_clobber_a_newer_installed_version(self):
        # A delayed/stale job for version 1 recording its own FAILED after
        # version 3 has already gone healthy must not move the installed
        # floor backwards.
        deploy_state.write_state(self.sftp, "/server", "3", deploy_state.HEALTHY, now=fixed_now)
        record = deploy_state.write_state(self.sftp, "/server", "1", deploy_state.FAILED, now=fixed_now)
        self.assertEqual(record["installed_version"], "3")
        deploy_state.guard_stale_release(self.sftp, "/server", "3")  # same-version retry still allowed
        with self.assertRaisesRegex(ValueError, "Stale release"):
            deploy_state.guard_stale_release(self.sftp, "/server", "2")

    def test_installed_version_only_advances_forward_never_backward(self):
        deploy_state.write_state(self.sftp, "/server", "3", deploy_state.HEALTHY, now=fixed_now)
        # Re-recording UPDATING for the same (not a newer) version must not
        # regress installed_version, and retrying the same version is fine.
        record = deploy_state.write_state(self.sftp, "/server", "3", deploy_state.UPDATING, now=fixed_now)
        self.assertEqual(record["installed_version"], "3")

    def test_write_state_round_trip_includes_installed_version(self):
        written = deploy_state.write_state(self.sftp, "/server", "0.53.0", deploy_state.UPDATING, now=fixed_now)
        self.assertEqual(written["installed_version"], "0.53.0")
        read = deploy_state.read_state(self.sftp, "/server")
        self.assertEqual(read, written)

    def test_read_state_rejects_record_missing_installed_version_type(self):
        (self.root / deploy_state.STATE_FILE).write_text(json.dumps(
            {"schema": 2, "version": "1.0.0", "state": "healthy", "updated_at": "x", "installed_version": 42}))
        with self.assertRaisesRegex(ValueError, "Invalid deployment-state record"):
            deploy_state.read_state(self.sftp, "/server")


if __name__ == "__main__":
    unittest.main()
