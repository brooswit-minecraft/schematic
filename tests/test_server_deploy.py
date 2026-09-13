import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
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
        return [SimpleNamespace(filename=p.name, st_mode=p.lstat().st_mode) for p in self.path(path).iterdir()]


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


if __name__ == "__main__":
    unittest.main()
