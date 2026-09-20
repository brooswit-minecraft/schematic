from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile


SPEC = importlib.util.spec_from_file_location("deploy", Path(__file__).parents[1] / "scripts/server_deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def entry(path="mods/example.jar", data=b"server", server="required"):
    return {"path": path, "fileSize": len(data), "env": {"server": server},
            "hashes": {name: hashlib.new(name, data).hexdigest() for name in ("sha1", "sha512")},
            "downloads": ["https://example.invalid/file"]}


class FakeSFTP:
    """Local filesystem fake; no credentials, network or server required."""

    def __init__(self, root):
        self.root = root
        self.promotions = 0
        self.fail_promotion = False
        self.atomic = True

    def path(self, path):
        return self.root / path.lstrip("/")

    def lstat(self, path):
        return self.path(path).lstat()

    def mkdir(self, path):
        self.path(path).mkdir()

    def rmdir(self, path):
        self.path(path).rmdir()

    def remove(self, path):
        self.path(path).unlink()

    def open(self, path, mode):
        return self.path(path).open("xb" if mode == "wx" else mode)

    def put(self, source, target):
        shutil.copyfile(source, self.path(target))

    def chmod(self, path, mode):
        self.path(path).chmod(mode)

    def posix_rename(self, source, target):
        if not self.atomic:
            raise OSError("Extension unavailable")
        if "/.schematic-stage-" not in target:
            self.promotions += 1
            if self.fail_promotion:
                raise OSError("Simulated disconnect")
        self.path(source).replace(self.path(target))

    def listdir_attr(self, path):
        return [SimpleNamespace(filename=p.name, st_mode=p.lstat().st_mode, st_size=p.lstat().st_size)
                for p in self.path(path).iterdir()]


class FakeRCONSocket:
    def __init__(self, responses):
        self.responses = bytearray(b"".join(responses))
        self.sent = []
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        # Deliberately fragment reads to exercise receive_exact.
        size = min(size, 3, len(self.responses))
        result = bytes(self.responses[:size])
        del self.responses[:size]
        return result


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.remote = self.root / "remote"
        (self.remote / "server").mkdir(parents=True)
        self.sftp = FakeSFTP(self.remote)

    def pack(self, entries=None, overrides=None, version="1.2.3"):
        archive = self.root / "release.mrpack"
        with zipfile.ZipFile(archive, "w") as out:
            out.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "game": "minecraft", "versionId": version,
                "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.0"},
                "files": entries if entries is not None else [entry()],
            }))
            for path, data in (overrides or {}).items():
                out.writestr(path, data)
        return archive

    def materialize(self, **kwargs):
        return deploy.materialize(self.pack(**kwargs), self.output, "1.2.3", fetch=lambda _: io.BytesIO(b"server"))

    def test_server_selection_and_override_precedence(self):
        result = self.materialize(entries=[entry(), entry("mods/client.jar", server="unsupported"),
                                          entry("mods/optional.jar", server="optional")],
                                  overrides={"overrides/config/test.toml": b"common",
                                             "server-overrides/config/test.toml": b"server",
                                             "client-overrides/client.txt": b"ignore"})
        self.assertEqual(set(result["files"]), {"mods/example.jar", "mods/optional.jar", "config/test.toml"})
        self.assertEqual((self.output / "config/test.toml").read_bytes(), b"server")
        self.assertEqual(result["dependencies"]["minecraft"], "1.21.1")

    def test_missing_environment_defaults_to_required(self):
        file = entry()
        del file["env"]
        self.assertIn(file["path"], self.materialize(entries=[file])["files"])

    def test_wrong_release_version(self):
        with self.assertRaisesRegex(ValueError, "version differ"):
            self.materialize(version="9.9.9")

    def test_hash_mismatch(self):
        file = entry(data=b"badbad")
        with self.assertRaisesRegex(ValueError, "verified download"):
            self.materialize(entries=[file])

    def test_sha1_also_verified(self):
        file = entry()
        file["hashes"]["sha1"] = "0" * 40
        with self.assertRaises(ValueError):
            self.materialize(entries=[file])

    def test_size_mismatch(self):
        file = entry()
        file["fileSize"] = 1
        with self.assertRaises(ValueError):
            self.materialize(entries=[file])

    def test_duplicate_paths(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.materialize(entries=[entry(), entry()])

    def test_unsafe_paths_and_server_data(self):
        for path in ("../outside", "/absolute", "mods/../../world", "mods\\escape", "C:/escape",
                     "world/region/file", "server.properties", ".ssh/key", "mods/a\n.jar"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                deploy.safe_path(path)

    def test_override_traversal(self):
        with self.assertRaises(ValueError):
            self.materialize(overrides={"server-overrides/../outside": b"bad"})

    def test_override_symlink(self):
        archive = self.pack(entries=[])
        with zipfile.ZipFile(archive, "a") as out:
            info = zipfile.ZipInfo("overrides/config/link")
            info.external_attr = 0o120777 << 16
            out.writestr(info, "/etc/passwd")
        with self.assertRaises(ValueError):
            deploy.materialize(archive, self.output, "1.2.3")

    def test_non_https_download_rejected_without_network(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            deploy.download("http://example.invalid/file")

    def test_clean_upload_and_idempotent_repeat(self):
        self.materialize()
        deploy.upload(self.sftp, self.output, "/server")
        deploy.upload(self.sftp, self.output, "/server")
        self.assertEqual((self.remote / "server/mods/example.jar").read_bytes(), b"server")
        self.assertFalse((self.remote / "server" / deploy.PENDING).exists())
        self.assertFalse(list((self.remote / "server").glob(".schematic-stage-*")))

    def test_supervisor_is_installed_atomically_and_executable(self):
        deploy.install_supervisor(self.sftp, "/server")
        target = self.remote / "server" / deploy.SUPERVISOR
        self.assertEqual(target.read_bytes(), deploy.SUPERVISOR_BODY)
        self.assertTrue(target.stat().st_mode & 0o100)
        deploy.install_supervisor(self.sftp, "/server")
        self.assertFalse(list((self.remote / "server").glob(deploy.SUPERVISOR + ".tmp-*")))

    def test_update_removes_only_tracked_stale_files(self):
        self.materialize()
        deploy.upload(self.sftp, self.output, "/server")
        world = self.remote / "server/world/region"
        world.mkdir(parents=True)
        (world / "save").write_bytes(b"keep")
        shutil.rmtree(self.output)
        self.materialize(entries=[entry("mods/new.jar")])
        deploy.upload(self.sftp, self.output, "/server")
        self.assertFalse((self.remote / "server/mods/example.jar").exists())
        self.assertTrue((self.remote / "server/mods/new.jar").exists())
        self.assertEqual((world / "save").read_bytes(), b"keep")

    def test_untracked_mod_refused(self):
        self.materialize()
        mods = self.remote / "server/mods"
        mods.mkdir()
        (mods / "old.jar").write_bytes(b"untracked")
        with self.assertRaisesRegex(ValueError, "Untracked"):
            deploy.upload(self.sftp, self.output, "/server")
        self.assertEqual(self.sftp.promotions, 0)

    def test_modified_managed_file_refused(self):
        self.materialize()
        deploy.upload(self.sftp, self.output, "/server")
        (self.remote / "server/mods/example.jar").write_bytes(b"manual edit")
        with self.assertRaisesRegex(ValueError, "differs"):
            deploy.upload(self.sftp, self.output, "/server")

    def test_remote_symlink_refused(self):
        self.materialize()
        outside = self.root / "outside"
        outside.mkdir()
        (self.remote / "server/mods").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            deploy.upload(self.sftp, self.output, "/server")
        self.assertEqual(list(outside.iterdir()), [])

    def test_atomic_extension_required_before_promotion(self):
        self.materialize()
        self.sftp.atomic = False
        with self.assertRaises(OSError):
            deploy.upload(self.sftp, self.output, "/server")
        self.assertEqual(self.sftp.promotions, 0)
        self.assertFalse((self.remote / "server/mods").exists())

    def test_interrupted_promotion_blocks_retry(self):
        self.materialize()
        self.sftp.fail_promotion = True
        with self.assertRaises(OSError):
            deploy.upload(self.sftp, self.output, "/server")
        self.assertTrue((self.remote / "server" / deploy.PENDING).exists())
        self.sftp.fail_promotion = False
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            deploy.upload(self.sftp, self.output, "/server")

    def test_rcon_packet_and_command(self):
        connection = FakeRCONSocket([
            deploy.rcon_packet(1, 2, ""),
            deploy.rcon_packet(2, 0, "Stopping the server"),
        ])
        result = deploy.rcon_command("server.invalid", 25575, "secret", "stop",
                                     connect=lambda *_args, **_kwargs: connection)
        self.assertEqual(result, "Stopping the server")
        self.assertEqual(connection.timeout, 10)
        self.assertEqual(connection.sent, [deploy.rcon_packet(1, 3, "secret"),
                                           deploy.rcon_packet(2, 2, "stop")])

    def test_rcon_authentication_failure_is_distinct(self):
        connection = FakeRCONSocket([deploy.rcon_packet(-1, 2, "")])
        with self.assertRaises(deploy.RCONAuthError):
            deploy.rcon_command("server.invalid", 25575, "wrong", "list",
                                connect=lambda *_args, **_kwargs: connection)

    def test_rcon_rejects_invalid_packets_and_payloads(self):
        connection = FakeRCONSocket([b"\x01\x00\x00\x00x"])
        with self.assertRaises(deploy.RCONError):
            deploy.receive_rcon_packet(connection)
        with self.assertRaisesRegex(ValueError, "null byte"):
            deploy.rcon_packet(1, 2, "bad\0command")

    def test_stop_waits_for_two_consecutive_unavailable_probes(self):
        calls = []
        probes = iter(("online", OSError("down"), OSError("still down")))

        def command(host, port, password, operation, **_kwargs):
            calls.append(operation)
            if operation == "stop":
                return "Stopping"
            outcome = next(probes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "secret"}
        clock = iter((0, 0, 1, 2))
        deploy.stop_and_wait(config, command=command, sleep=lambda _: None,
                             monotonic=lambda: next(clock))
        self.assertEqual(calls, ["stop", "list", "list", "list"])

    def test_stop_does_not_treat_auth_failure_as_shutdown(self):
        def command(_host, _port, _password, operation, **_kwargs):
            if operation == "list":
                raise deploy.RCONAuthError("bad password")
            return "Stopping"

        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "wrong"}
        with self.assertRaises(deploy.RCONAuthError):
            deploy.stop_and_wait(config, command=command, sleep=lambda _: None,
                                 monotonic=lambda: 0)

    def test_stop_times_out_while_rcon_remains_available(self):
        calls = []

        def command(_host, _port, _password, operation, **_kwargs):
            calls.append(operation)
            return "online"

        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "secret",
                  "SERVER_RCON_SHUTDOWN_TIMEOUT": "1"}
        clock = iter((0, 0, 1))
        with self.assertRaises(TimeoutError):
            deploy.stop_and_wait(config, command=command, sleep=lambda _: None,
                                 monotonic=lambda: next(clock))
        self.assertEqual(calls, ["stop", "list"])

    def test_wait_until_ready_retries_then_returns_status(self):
        outcomes = iter((OSError("down"), TimeoutError("starting"), "online"))
        calls = []

        def command(_host, _port, _password, operation, **_kwargs):
            calls.append(operation)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "secret"}
        clock = iter((0, 0, 1, 2, 3))
        result = deploy.wait_until_ready(config, command=command, sleep=lambda _: None,
                                         monotonic=lambda: next(clock))
        self.assertEqual(result, "online")
        self.assertEqual(calls, ["list", "list", "list"])

    def test_wait_for_restart_observes_outage_then_readiness(self):
        outcomes = iter(("still online", OSError("restarting"), OSError("starting"), "online"))
        calls = []

        def command(_host, _port, _password, operation, **_kwargs):
            calls.append(operation)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "secret"}
        clock = iter((0, 0, 1, 2, 2, 3, 4))
        result = deploy.wait_for_restart(config, command=command, sleep=lambda _: None,
                                         monotonic=lambda: next(clock))
        self.assertEqual(result, "online")
        self.assertEqual(calls, ["list", "list", "list", "list"])

    def test_wait_for_restart_rejects_auth_failure(self):
        def command(*_args, **_kwargs):
            raise deploy.RCONAuthError("bad password")

        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "wrong"}
        with self.assertRaises(deploy.RCONAuthError):
            deploy.wait_for_restart(config, command=command, sleep=lambda _: None,
                                    monotonic=lambda: 0)

    def test_restart_flushes_saves_stops_and_waits_for_return(self):
        calls = []
        outcomes = iter((OSError("down"), "online"))

        def command(_host, _port, _password, operation, **_kwargs):
            calls.append(operation)
            if operation in ("save-all flush", "stop"):
                return "ok"
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "secret"}
        clock = iter((0, 0, 0, 1, 2))
        result = deploy.restart_and_wait(config, command=command, sleep=lambda _: None,
                                         monotonic=lambda: next(clock))
        self.assertEqual(result, "online")
        self.assertEqual(calls, ["save-all flush", "stop", "list", "list"])

    def test_deploy_verifies_readiness_before_opening_sftp(self):
        order = []
        config = {key: "test" for key in ("SERVER_RCON_HOST", "SERVER_RCON_PASSWORD",
                                            "SERVER_SFTP_HOST", "SERVER_SFTP_USERNAME",
                                            "SERVER_SFTP_PATH", "SERVER_SFTP_KNOWN_HOSTS")}
        config["SERVER_SFTP_PASSWORD"] = "secret"
        with mock.patch.object(deploy, "wait_until_ready", side_effect=lambda _env: order.append("ready")), \
             mock.patch.object(deploy, "connect_upload", side_effect=lambda _output, _env: order.append("upload")):
            deploy.deploy("output", config)
        self.assertEqual(order, ["ready", "upload"])

    def test_deploy_validates_all_configuration_before_readiness_probe(self):
        with mock.patch.object(deploy, "wait_until_ready") as ready:
            with self.assertRaises(ValueError):
                deploy.deploy("output", {"SERVER_RCON_HOST": "host", "SERVER_RCON_PASSWORD": "secret"})
        ready.assert_not_called()

    def test_configuration_requires_rcon_sftp_and_one_credential(self):
        config = {key: "test" for key in ("SERVER_SFTP_HOST", "SERVER_SFTP_USERNAME", "SERVER_SFTP_PATH", "SERVER_SFTP_KNOWN_HOSTS")}
        config["SERVER_SFTP_PASSWORD"] = "not-a-real-password"
        self.assertEqual(deploy.upload_config(config), 22)
        config["SERVER_SFTP_PRIVATE_KEY"] = "not-a-real-key"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            deploy.upload_config(config)
        self.assertEqual(deploy.rcon_config({"SERVER_RCON_HOST": "host", "SERVER_RCON_PASSWORD": "password"}),
                         ("host", 25575, "password", 120.0))
        with self.assertRaisesRegex(ValueError, "RCON"):
            deploy.rcon_config({})

    # --- Pre-deploy world archive (SCHEM-38) ---------------------------

    def backup_config(self, **overrides):
        config = {"SERVER_RCON_HOST": "server.invalid", "SERVER_RCON_PASSWORD": "secret",
                  "SERVER_SFTP_HOST": "server.invalid", "SERVER_SFTP_USERNAME": "user",
                  "SERVER_SFTP_PATH": "/server", "SERVER_SFTP_KNOWN_HOSTS": "kh",
                  "SERVER_SFTP_PASSWORD": "sftp-secret"}
        config.update(overrides)
        return config

    def test_safe_component_rejects_unsafe_values(self):
        for value in ("", ".", "..", ".hidden", "a/b", "a\\b", "a:b", "a\nb", None, 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.safe_component(value)
        self.assertEqual(deploy.safe_component("world"), "world")

    def test_backup_filename_and_pattern(self):
        when = datetime(2026, 9, 20, 6, 30, 0, tzinfo=timezone.utc)
        name = deploy.backup_filename("world", "1.2.3", when)
        self.assertEqual(name, "world-20260920T063000Z-1.2.3.tar.gz")
        self.assertRegex(name, deploy.backup_pattern("world"))
        self.assertNotRegex("otherlevel-20260920T063000Z-1.2.3.tar.gz", deploy.backup_pattern("world"))
        self.assertNotRegex("world-20260920T063000Z-1.2.3.tar.gz.bak", deploy.backup_pattern("world"))
        self.assertEqual(deploy.backup_filename("world", "", when), "world-20260920T063000Z-deploy.tar.gz")

    def test_parse_retention_rejects_non_positive_integers(self):
        for bad in ("0", "-1", "abc", "5.5", "", None, " "):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                deploy.parse_retention(bad)
        self.assertEqual(deploy.parse_retention("5"), 5)
        self.assertEqual(deploy.parse_retention(5), 5)

    def test_backup_level_name_defaults_and_reads_configured_value(self):
        self.assertEqual(deploy.read_level_name(self.sftp, "/server"), "world")
        (self.remote / "server/server.properties").write_text("motd=hi\n")
        self.assertEqual(deploy.read_level_name(self.sftp, "/server"), "world")
        (self.remote / "server/server.properties").write_text("# comment\nlevel-name=myworld\n")
        self.assertEqual(deploy.read_level_name(self.sftp, "/server"), "myworld")

    def test_backup_level_name_rejects_traversal_and_hidden_values(self):
        for bad in ("../escape", "/absolute", ".hidden", "a/b"):
            (self.remote / "server/server.properties").write_text(f"level-name={bad}\n")
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                deploy.read_level_name(self.sftp, "/server")

    def test_backup_missing_world_directory_refused(self):
        with tempfile.TemporaryDirectory() as tar_dir:
            with self.assertRaisesRegex(ValueError, "not found"):
                deploy.build_world_archive(self.sftp, "/server", "world", Path(tar_dir) / "x.tar.gz")

    def test_backup_refuses_symlink_in_world(self):
        world = self.remote / "server/world"
        world.mkdir()
        (world / "level.dat").write_bytes(b"x")
        outside = self.root / "outside"
        outside.mkdir()
        (world / "escape").symlink_to(outside, target_is_directory=True)
        with tempfile.TemporaryDirectory() as tar_dir:
            with self.assertRaises(ValueError):
                deploy.build_world_archive(self.sftp, "/server", "world", Path(tar_dir) / "x.tar.gz")

    def test_backup_creates_verified_archive_of_world_dirs_only(self):
        (self.remote / "server/world/region").mkdir(parents=True)
        (self.remote / "server/world/level.dat").write_bytes(b"leveldata")
        (self.remote / "server/world/region/r.0.0.mca").write_bytes(b"regiondata")
        (self.remote / "server/world_nether").mkdir()
        (self.remote / "server/world_nether/level.dat").write_bytes(b"netherdata")
        (self.remote / "server/logs").mkdir()
        (self.remote / "server/logs/latest.log").write_bytes(b"not archived")
        when = datetime(2026, 9, 20, 6, 30, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tar_dir:
            result = deploy.archive_and_prune(self.sftp, "/server", 5, tar_dir, tag="1.2.3", now=when)
        self.assertEqual(result["archive"], "world-20260920T063000Z-1.2.3.tar.gz")
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["removed"], [])
        archived = self.remote / "server/backups" / result["archive"]
        self.assertTrue(archived.exists())
        with tarfile.open(archived) as tar:
            names = sorted(tar.getnames())
        self.assertEqual(names, ["world/level.dat", "world/region/r.0.0.mca", "world_nether/level.dat"])
        self.assertFalse(list((self.remote / "server/backups").glob(deploy.BACKUP_STAGE_PREFIX + "*")))

    def test_backup_rejects_remote_name_collision(self):
        when = datetime(2026, 9, 20, 6, 30, 0, tzinfo=timezone.utc)
        backups = self.remote / "server/backups"
        backups.mkdir(parents=True)
        (backups / "world-20260920T063000Z-1.2.3.tar.gz").write_bytes(b"existing")
        (self.remote / "server/world").mkdir()
        (self.remote / "server/world/level.dat").write_bytes(b"x")
        with tempfile.TemporaryDirectory() as tar_dir:
            with self.assertRaisesRegex(ValueError, "collision"):
                deploy.archive_and_prune(self.sftp, "/server", 5, tar_dir, tag="1.2.3", now=when)

    def test_backup_upload_hash_mismatch_detected_and_stage_cleaned_up(self):
        (self.remote / "server/world").mkdir()
        (self.remote / "server/world/level.dat").write_bytes(b"leveldata")
        original_put = self.sftp.put

        def corrupting_put(source, target):
            original_put(source, target)
            self.sftp.path(target).write_bytes(b"corrupted")

        self.sftp.put = corrupting_put
        with tempfile.TemporaryDirectory() as tar_dir:
            with self.assertRaisesRegex(ValueError, "verification"):
                deploy.archive_and_prune(self.sftp, "/server", 5, tar_dir)
        self.assertFalse(list((self.remote / "server/backups").glob(deploy.BACKUP_STAGE_PREFIX + "*")))

    def test_backup_partial_put_failure_cleans_up_stage_and_propagates(self):
        (self.remote / "server/world").mkdir()
        (self.remote / "server/world/level.dat").write_bytes(b"leveldata")

        def failing_put(source, target):
            # Simulate a connection drop partway through the upload: the
            # staged file is left half-written, then the call raises.
            self.sftp.path(target).write_bytes(b"only-half-the-archive")
            raise ConnectionError("simulated disconnect during put")

        self.sftp.put = failing_put
        with tempfile.TemporaryDirectory() as tar_dir:
            with self.assertRaisesRegex(ConnectionError, "simulated disconnect"):
                deploy.archive_and_prune(self.sftp, "/server", 5, tar_dir)
        self.assertFalse(list((self.remote / "server/backups").glob(deploy.BACKUP_STAGE_PREFIX + "*")),
                          "a partial stage file from a failed put must not be left behind")

    def test_backup_sweeps_stale_stage_file_from_killed_run_before_archiving(self):
        (self.remote / "server/world").mkdir()
        (self.remote / "server/world/level.dat").write_bytes(b"leveldata")
        backups = self.remote / "server/backups"
        backups.mkdir(parents=True)
        stale = backups / (deploy.BACKUP_STAGE_PREFIX + "deadbeef")
        stale.write_bytes(b"leftover from a run that was killed before it cleaned up")
        unrelated = backups / "not-a-backup.txt"
        unrelated.write_bytes(b"leave me alone")

        with tempfile.TemporaryDirectory() as tar_dir:
            result = deploy.archive_and_prune(self.sftp, "/server", 5, tar_dir)

        self.assertFalse(stale.exists(), "a stale stage file from a killed prior run must be swept")
        self.assertTrue(unrelated.exists(), "the sweep must never touch files outside its own prefix")
        self.assertTrue((backups / result["archive"]).exists())

    def test_backup_retention_keeps_n_most_recent_and_ignores_unrelated_files(self):
        backups = self.remote / "server/backups"
        backups.mkdir(parents=True)
        for day in (1, 2, 3):
            (backups / f"world-2026010{day}T000000Z-x.tar.gz").write_bytes(b"old")
        (backups / "not-a-backup.txt").write_bytes(b"keep me")
        (backups / "otherlevel-20260101T000000Z-x.tar.gz").write_bytes(b"different level, keep me")

        removed = deploy.enforce_backup_retention(self.sftp, "/server/backups", "world", 3)
        self.assertEqual(removed, [])

        (backups / "world-20260104T000000Z-x.tar.gz").write_bytes(b"new, this is the (N+1)th")
        removed = deploy.enforce_backup_retention(self.sftp, "/server/backups", "world", 3)
        self.assertEqual(removed, ["world-20260101T000000Z-x.tar.gz"])
        self.assertFalse((backups / "world-20260101T000000Z-x.tar.gz").exists())
        self.assertTrue((backups / "not-a-backup.txt").exists())
        self.assertTrue((backups / "otherlevel-20260101T000000Z-x.tar.gz").exists())
        remaining = sorted(p.name for p in backups.glob("world-*"))
        self.assertEqual(remaining, ["world-20260102T000000Z-x.tar.gz", "world-20260103T000000Z-x.tar.gz",
                                      "world-20260104T000000Z-x.tar.gz"])

    def test_backup_retention_rejects_invalid_n_before_touching_backups(self):
        for bad in (0, -1, 1.5, True, "5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                deploy.enforce_backup_retention(self.sftp, "/server/backups", "world", bad)

    def test_backup_env_config_validates_and_defaults_retention(self):
        config = self.backup_config()
        self.assertEqual(deploy.backup_env_config(config), ("/server", deploy.DEFAULT_BACKUP_RETENTION))
        config["SERVER_BACKUP_RETENTION"] = "3"
        self.assertEqual(deploy.backup_env_config(config), ("/server", 3))
        config["SERVER_BACKUP_RETENTION"] = "0"
        with self.assertRaises(ValueError):
            deploy.backup_env_config(config)
        del config["SERVER_RCON_HOST"]
        with self.assertRaises(ValueError):
            deploy.backup_env_config(config)

    def test_backup_validates_configuration_before_any_rcon_call(self):
        config = self.backup_config(SERVER_BACKUP_RETENTION="0")
        with mock.patch.object(deploy, "rcon_command") as rcon_fn:
            with self.assertRaises(ValueError):
                deploy.backup(config)
        rcon_fn.assert_not_called()

    def test_backup_flushes_world_archives_then_saves_on(self):
        calls = []

        def command(_host, _port, _password, operation, **_kwargs):
            calls.append(operation)
            return "ok"

        def connect(root, retention, tar_dir, env, tag):
            calls.append("archive:" + root)
            return {"archive": "world-x.tar.gz", "removed": []}

        result = deploy.backup(self.backup_config(), command=command, connect=connect)
        self.assertEqual(calls, ["save-off", "save-all flush", "archive:/server", "save-on"])
        self.assertEqual(result["archive"], "world-x.tar.gz")

    def test_backup_failure_still_issues_save_on_and_propagates_before_any_upload(self):
        calls = []

        def command(_host, _port, _password, operation, **_kwargs):
            calls.append(operation)
            return "ok"

        def connect(root, retention, tar_dir, env, tag):
            raise ValueError("simulated archive failure")

        with self.assertRaisesRegex(ValueError, "simulated archive failure"):
            deploy.backup(self.backup_config(), command=command, connect=connect)
        self.assertEqual(calls, ["save-off", "save-all flush", "save-on"])

    def test_backup_save_on_guaranteed_even_when_flush_itself_fails(self):
        calls = []

        def command(_host, _port, _password, operation, **_kwargs):
            calls.append(operation)
            if operation == "save-all flush":
                raise OSError("rcon hiccup")
            return "ok"

        def connect(*_args, **_kwargs):
            self.fail("must not archive when the pre-archive flush failed")

        with self.assertRaises(OSError):
            deploy.backup(self.backup_config(), command=command, connect=connect)
        self.assertEqual(calls, ["save-off", "save-all flush", "save-on"])

    def test_deploy_makes_no_backup_or_rcon_calls_when_backup_not_invoked(self):
        order = []
        config = {key: "test" for key in ("SERVER_RCON_HOST", "SERVER_RCON_PASSWORD",
                                            "SERVER_SFTP_HOST", "SERVER_SFTP_USERNAME",
                                            "SERVER_SFTP_PATH", "SERVER_SFTP_KNOWN_HOSTS")}
        config["SERVER_SFTP_PASSWORD"] = "secret"
        with mock.patch.object(deploy, "wait_until_ready", side_effect=lambda _env: order.append("ready")), \
             mock.patch.object(deploy, "connect_upload", side_effect=lambda _output, _env: order.append("upload")), \
             mock.patch.object(deploy, "backup") as backup_fn, \
             mock.patch.object(deploy, "archive_and_prune") as archive_fn, \
             mock.patch.object(deploy, "rcon_command") as rcon_fn:
            deploy.deploy("output", config)
        self.assertEqual(order, ["ready", "upload"])
        backup_fn.assert_not_called()
        archive_fn.assert_not_called()
        rcon_fn.assert_not_called()

    def test_backup_cli_error_never_leaks_secrets(self):
        config = self.backup_config(SERVER_RCON_PASSWORD="topsecret-rcon",
                                     SERVER_SFTP_PASSWORD="topsecret-sftp")
        with mock.patch.object(deploy, "rcon_command",
                                side_effect=RuntimeError("auth failed with topsecret-rcon / topsecret-sftp")), \
             mock.patch.object(sys, "argv", ["server_deploy.py", "backup"]), \
             mock.patch.dict(os.environ, config, clear=True), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = deploy.main()
        self.assertEqual(code, 1)
        output = stderr.getvalue()
        self.assertIn("RuntimeError", output)
        self.assertNotIn("topsecret", output)


if __name__ == "__main__":
    unittest.main()
