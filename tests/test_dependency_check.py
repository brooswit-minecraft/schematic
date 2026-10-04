"""MINECRAFT-38: offline tests for scripts/dependency_check.py.

Fixture jars are built in-test with zipfile; nothing here touches the network.
Needs Python 3.11+ (tomllib), same as the script.
"""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location("depcheck", Path(__file__).parents[1] / "scripts/dependency_check.py")
dc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dc)


def mods_toml(mod_id, version="1.0.0", deps=(), legacy=False):
    text = f'modLoader="javafml"\nloaderVersion="[1,)"\n[[mods]]\nmodId="{mod_id}"\nversion="{version}"\n'
    for dep_id, rng, kind in deps:
        text += f'[[dependencies.{mod_id}]]\nmodId="{dep_id}"\n'
        if legacy:
            text += f'mandatory={"true" if kind == "required" else "false"}\n'
        else:
            text += f'type="{kind}"\n'
        text += f'versionRange="{rng}"\nside="BOTH"\n'
    return text


def make_jar(mod_id, version="1.0.0", deps=(), nested=(), legacy=False, manifest_version=None):
    """nested: iterable of jar bytes bundled under META-INF/jarjar/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        name = "META-INF/mods.toml" if legacy else "META-INF/neoforge.mods.toml"
        zf.writestr(name, mods_toml(mod_id, version, deps, legacy))
        if manifest_version:
            zf.writestr("META-INF/MANIFEST.MF", f"Implementation-Version: {manifest_version}\n")
        jars = []
        for i, data in enumerate(nested):
            path = f"META-INF/jarjar/nested{i}.jar"
            zf.writestr(path, data)
            jars.append({"identifier": {"group": "g", "artifact": f"a{i}"}, "path": path})
        if jars:
            zf.writestr("META-INF/jarjar/metadata.json", json.dumps({"jars": jars}))
    return buf.getvalue()


def load(jars):
    return [dc.load_jar(f"{i}.jar", data) for i, data in enumerate(jars)]


def rows_by_dep(jars, versions=None):
    rows, notes = dc.analyze(load(jars), versions)
    return {r["dep"]: r for r in rows}, notes


class VersionRangeTests(unittest.TestCase):
    def test_ranges(self):
        cases = [
            ("0.7.3", "[0.7.3,)", True), ("0.7.2", "[0.7.3,)", False),
            ("1.2.0", "[1.0,2.0)", True), ("2.0", "[1.0,2.0)", False),
            ("2.0", "[1.0,2.0]", True), ("1.0", "(1.0,2.0)", False),
            ("1.5", "[1.5]", True), ("1.6", "[1.5]", False),
            ("1.0", "(,1.0]", True), ("1.1", "(,1.0]", False),
            ("3.5", "[1,2),[3,4)", True), ("2.5", "[1,2),[3,4)", False),
            ("1.10", "[1.9,)", True), ("5", "", True), ("1.2", "1.3", False),
            ("1.2.3-beta", "[1.2.3,)", False), ("1.2.3", "[1.2.3-beta,)", True),
            ("1.0.82+mc1.21.1", "[1.0.82,)", True), ("1.0.81+mc1.21.1", "[1.0.82,)", False),
        ]
        for version, spec, want in cases:
            with self.subTest(version=version, spec=spec):
                self.assertEqual(dc.in_range(version, spec), want)


class ClosureTests(unittest.TestCase):
    def test_control_known_satisfied_dep_shows_satisfied(self):
        a = make_jar("a", deps=[("b", "[1.0,)", "required")])
        b = make_jar("b", "1.4.0")
        rows, _ = rows_by_dep([a, b])
        self.assertEqual(rows["b"]["status"], "satisfied")
        self.assertIn("1.4.0", rows["b"]["detail"])

    def test_missing_required_dep(self):
        # The sickos shape: projectatmosphere needs simpleclouds, nobody ships it.
        pa = make_jar("projectatmosphere", deps=[("simpleclouds", "[0.7.3,)", "required")])
        other = make_jar("other")
        rows, _ = rows_by_dep([pa, other])
        self.assertEqual(rows["simpleclouds"]["status"], "missing")

    def test_version_out_of_range(self):
        a = make_jar("a", deps=[("b", "[2.0,)", "required")])
        b = make_jar("b", "1.0.0")
        rows, _ = rows_by_dep([a, b])
        self.assertEqual(rows["b"]["status"], "version")

    def test_satisfied_by_nested_jarjar(self):
        a = make_jar("a", deps=[("lib", "[1.0,2.0)", "required")], nested=[make_jar("lib", "1.5")])
        rows, _ = rows_by_dep([a])
        self.assertEqual(rows["lib"]["status"], "satisfied")

    def test_nested_jar_dependencies_are_checked_too(self):
        inner = make_jar("inner", deps=[("ghost", "", "required")])
        outer = make_jar("outer", nested=[inner])
        rows, _ = rows_by_dep([outer])
        self.assertEqual(rows["ghost"]["status"], "missing")

    def test_optional_and_non_required_ignored(self):
        a = make_jar("a", deps=[("b", "", "optional"), ("c", "", "incompatible")])
        rows, _ = rows_by_dep([a])
        self.assertEqual(rows, {})

    def test_legacy_mandatory_true(self):
        a = make_jar("a", deps=[("b", "[1,)", "required"), ("c", "", "optional")], legacy=True)
        rows, _ = rows_by_dep([a])
        self.assertEqual(rows["b"]["status"], "missing")
        self.assertNotIn("c", rows)

    def test_own_provider_does_not_satisfy_itself(self):
        a = make_jar("a", deps=[("a", "", "required")])
        rows, _ = rows_by_dep([a])
        self.assertEqual(rows["a"]["status"], "missing")

    def test_platform_deps_never_fail_only_noted(self):
        # simpleclouds-style: minecraft [1.21,1.21.1) reads as excluding 1.21.1.
        a = make_jar("a", deps=[("minecraft", "[1.21,1.21.1)", "required"),
                                ("neoforge", "[99,)", "required"),
                                ("java", "[21,)", "required")])
        rows, notes = rows_by_dep([a], {"minecraft": "1.21.1", "neoforge": "21.1.248"})
        for dep in ("minecraft", "neoforge", "java"):
            self.assertEqual(rows[dep]["status"], "platform")
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("minecraft" in n for n in notes))

    def test_placeholder_version_falls_back_to_manifest(self):
        a = make_jar("a", deps=[("b", "[2,)", "required")])
        b = make_jar("b", "${file.jarVersion}", manifest_version="2.1")
        rows, _ = rows_by_dep([a, b])
        self.assertEqual(rows["b"]["status"], "satisfied")

    def test_jar_without_metadata_is_tolerated(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.txt", "hi")
        rows, _ = rows_by_dep([buf.getvalue()])
        self.assertEqual(rows, {})

    def test_bad_jar_and_dangling_jarjar_raise(self):
        with self.assertRaises(dc.CheckError):
            dc.load_jar("x.jar", b"not a zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(dc.JARJAR, json.dumps({"jars": [{"path": "META-INF/jarjar/gone.jar"}]}))
        with self.assertRaises(dc.CheckError):
            dc.load_jar("x.jar", buf.getvalue())


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "mods").mkdir()
        (self.root / "pack.toml").write_text('[versions]\nminecraft = "1.21.1"\nneoforge = "21.1.248"\n')
        self.files = {}
        self.lines = []

    def pin(self, name, data, bad_hash=False):
        url = f"https://example.invalid/{name}.jar"
        digest = "0" * 128 if bad_hash else hashlib.sha512(data).hexdigest()
        (self.root / "mods" / f"{name}.pw.toml").write_text(
            f'name = "{name}"\nfilename = "{name}.jar"\n[download]\nurl = "{url}"\n'
            f'hash-format = "sha512"\nhash = "{digest}"\n')
        self.files[url] = data

    def run_check(self, **kw):
        def fetch(url):
            return self.files[url]
        return dc.run(self.root / "mods", self.root / "pack.toml", fetch=fetch, out=self.lines.append, **kw)

    def text(self):
        return "\n".join(self.lines)

    def test_pass_with_nested_and_control(self):
        self.pin("a", make_jar("a", deps=[("b", "[1,)", "required"), ("lib", "", "required")],
                               nested=[make_jar("lib")]))
        self.pin("b", make_jar("b"))
        self.assertEqual(self.run_check(), 0)
        self.assertIn("2 satisfied", self.text())
        self.assertNotIn("WARN", self.text())

    def test_missing_dep_fails_run(self):
        self.pin("pa", make_jar("pa", deps=[("simpleclouds", "[0.7.3,)", "required")], nested=[make_jar("x")]))
        self.assertEqual(self.run_check(), 1)
        self.assertIn("simpleclouds", self.text())
        self.assertIn("MISSING", self.text())

    def test_hash_mismatch_fails(self):
        self.pin("a", make_jar("a"), bad_hash=True)
        self.assertEqual(self.run_check(), 1)
        self.assertIn("mismatch", self.text())

    def test_warns_when_no_nested_jars_anywhere(self):
        self.pin("a", make_jar("a", deps=[("b", "", "required")]))
        self.pin("b", make_jar("b"))
        self.assertEqual(self.run_check(), 0)
        self.assertIn("no nested (jarjar) jars", self.text())
        self.lines.clear()
        self.assertEqual(self.run_check(strict=True), 1)

    def test_platform_range_noted_not_failed(self):
        self.pin("a", make_jar("a", deps=[("minecraft", "[1.21,1.21.1)", "required")], nested=[make_jar("n")]))
        self.assertEqual(self.run_check(), 0)
        self.assertIn("NOTE", self.text())

    def test_pin_without_url_fails(self):
        (self.root / "mods" / "cf.pw.toml").write_text('name = "cf"\nfilename = "cf.jar"\n')
        self.assertEqual(self.run_check(), 1)
        self.assertIn("no download.url", self.text())

    def test_empty_mods_dir_fails(self):
        self.assertEqual(self.run_check(), 1)


if __name__ == "__main__":
    unittest.main()
