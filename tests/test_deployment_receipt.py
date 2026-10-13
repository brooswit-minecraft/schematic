import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("receipt", Path(__file__).parents[1] / "scripts/deployment_receipt.py")
receipt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipt)


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "pack.mrpack"
        self.manifest = Path(self.tmp.name) / "manifest.json"
        self.dependencies = {"minecraft": "1.21.1", "neoforge": "21.1.252"}
        with zipfile.ZipFile(self.archive, "w") as pack:
            pack.writestr("modrinth.index.json", json.dumps({"formatVersion": 1, "game": "minecraft",
                "versionId": "0.54.0", "dependencies": self.dependencies}))
        self.manifest.write_text(json.dumps({"schema": 1, "version": "0.54.0", "dependencies": self.dependencies}))

    def test_binds_exact_archive_version_and_run(self):
        result = receipt.build_receipt(self.archive, self.manifest, "server.example:25565",
            {"RESTART_OUTCOME": "success", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"})
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(result["dependencies"], self.dependencies)
        self.assertEqual(result["version"], "0.54.0")
        self.assertEqual(result["runId"], "123")
        self.assertTrue(result["ready"])
        self.assertIn("+00:00", result["verifiedAt"])

    def test_no_receipt_for_skipped_failed_or_unverified_restart(self):
        for outcome in ("skipped", "failure", "cancelled", ""):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                receipt.build_receipt(self.archive, self.manifest, "server.example:25565", {"RESTART_OUTCOME": outcome})

    def test_rejects_wrong_deployed_version_or_dependencies(self):
        for field, value in (("version", "0.53.1"), ("dependencies", {"minecraft": "1.20.1"})):
            manifest = {"schema": 1, "version": "0.54.0", "dependencies": self.dependencies}
            manifest[field] = value
            self.manifest.write_text(json.dumps(manifest))
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipt.build_receipt(self.archive, self.manifest, "server.example:25565", {"RESTART_OUTCOME": "success"})

    def test_workflow_publishes_only_after_verified_restart(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/reusable-server-update.yml").read_text()
        self.assertIn("id: restart", workflow)
        self.assertEqual(workflow.count("if: inputs.server-address != '' && steps.restart.outcome == 'success'"), 2)
        self.assertIn("name: server-compatibility\n", workflow)
        self.assertIn("path: ${{ runner.temp }}/server-compatibility.json", workflow)
