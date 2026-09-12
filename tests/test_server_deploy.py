import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
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

    def test_configuration_requires_stopped_and_one_credential(self):
        config = {key: "test" for key in ("SERVER_SFTP_HOST", "SERVER_SFTP_USERNAME", "SERVER_SFTP_PATH", "SERVER_SFTP_KNOWN_HOSTS")}
        config["SERVER_SFTP_PASSWORD"] = "not-a-real-password"
        with self.assertRaisesRegex(ValueError, "Stop"):
            deploy.upload_config(config)
        config["SERVER_SFTP_STOPPED"] = "true"
        self.assertEqual(deploy.upload_config(config), 22)
        config["SERVER_SFTP_PRIVATE_KEY"] = "not-a-real-key"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            deploy.upload_config(config)


if __name__ == "__main__":
    unittest.main()
