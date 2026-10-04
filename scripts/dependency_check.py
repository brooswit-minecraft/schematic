#!/usr/bin/env python3
"""Pre-release check: every pinned mod's REQUIRED NeoForge dependencies are met.

Static dependency closure (MINECRAFT-38). For each packwiz mods/*.pw.toml pin:
download the file, verify the pinned hash, read META-INF/neoforge.mods.toml
(legacy mods.toml with mandatory = true is honoured too), and confirm every
required dependency is provided, inside its version range, by another pinned
jar or by a nested jarjar jar (META-INF/jarjar/metadata.json).

minecraft / neoforge / forge / java / fml dependencies are platform-provided.
Their ranges are only NOTED, never failed: a range that reads as excluding the
pack's Minecraft version can still load (Simple Clouds declares
`minecraft [1.21,1.21.1)` and starts fine on 1.21.1).

Exit status: 0 all satisfied, 1 unsatisfied dependency / hash mismatch /
unreadable jar, 2 usage error. Needs Python 3.11+ (tomllib).
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tomllib
import urllib.request
import zipfile


PLATFORM = {"minecraft", "neoforge", "forge", "java", "fml", "javafml"}
HASHES = {"sha1", "sha256", "sha512", "md5"}
MODS_TOML = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")
JARJAR = "META-INF/jarjar/metadata.json"
MAX_NEST = 4


class CheckError(Exception):
    pass


# ---------------------------------------------------------------- versions

def _tokens(version):
    out = []
    # SemVer build metadata ("+mc1.21.1") does not affect ordering.
    version = str(version).strip().split("+", 1)[0]
    for part in re.split(r"[.\-_]", version):
        for tok in re.findall(r"\d+|[A-Za-z]+", part):
            out.append((1, int(tok), "") if tok.isdigit() else (0, 0, tok.lower()))
    while out and out[-1] == (1, 0, ""):
        out.pop()
    return out


def compare(a, b):
    """Maven-ish ordering: numeric parts numerically, qualifiers before release."""
    ta, tb = _tokens(a), _tokens(b)
    for i in range(max(len(ta), len(tb))):
        x = ta[i] if i < len(ta) else (1, 0, "")
        y = tb[i] if i < len(tb) else (1, 0, "")
        if x != y:
            return -1 if x < y else 1
    return 0


_RANGE = re.compile(r"[\[(][^\[\]()]*[\])]|[^,\s\[\]()][^,\[\]()]*")


def in_range(version, spec):
    """True if `version` satisfies a Maven version range (empty = anything).

    A bare version ("1.2") is a soft requirement, treated as a minimum.
    """
    spec = (spec or "").strip()
    if not spec:
        return True
    for m in _RANGE.finditer(spec):
        part = m.group(0).strip()
        if part[0] not in "[(":
            if compare(version, part) >= 0:
                return True
            continue
        lo_incl, hi_incl = part[0] == "[", part[-1] == "]"
        body = part[1:-1]
        if "," not in body:
            if compare(version, body.strip()) == 0:
                return True
            continue
        lo, hi = (s.strip() for s in body.split(",", 1))
        if lo:
            c = compare(version, lo)
            if c < 0 or (c == 0 and not lo_incl):
                continue
        if hi:
            c = compare(version, hi)
            if c > 0 or (c == 0 and not hi_incl):
                continue
        return True
    return False


# ------------------------------------------------------------------- packwiz

def read_pins(mods_dir):
    pins = []
    for path in sorted(Path(mods_dir).glob("*.pw.toml")):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        download = data.get("download", {})
        pins.append({
            "pin": path.name,
            "name": data.get("name", path.name),
            "filename": data.get("filename", path.name),
            "url": download.get("url"),
            "hash_format": download.get("hash-format"),
            "hash": download.get("hash"),
            "mode": download.get("mode"),
        })
    return pins


def pack_versions(pack_toml):
    try:
        with open(pack_toml, "rb") as f:
            return tomllib.load(f).get("versions", {})
    except OSError:
        return {}


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "schematic-dependency-check"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def fetch_verified(pin, fetch=download):
    if not pin["url"]:
        raise CheckError(f"{pin['pin']}: no download.url (mode={pin['mode']!r}); cannot verify")
    fmt = (pin["hash_format"] or "").lower()
    if fmt not in HASHES:
        raise CheckError(f"{pin['pin']}: unsupported hash-format {fmt!r}")
    data = fetch(pin["url"])
    actual = hashlib.new(fmt, data).hexdigest()
    if actual.lower() != str(pin["hash"]).lower():
        raise CheckError(f"{pin['pin']}: {fmt} mismatch (pinned {pin['hash']}, got {actual})")
    return data


# ---------------------------------------------------------------------- jars

def _manifest_version(zf):
    try:
        text = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except KeyError:
        return None
    m = re.search(r"^Implementation-Version:\s*(\S+)", text, re.M)
    return m.group(1) if m else None


def read_mods(zf):
    """Return (mods, deps) from a jar: [{id, version}], [{owner, id, range, side}]."""
    for name in MODS_TOML:
        if name in zf.namelist():
            break
    else:
        return [], []
    meta = tomllib.loads(zf.read(name).decode("utf-8"))
    jar_ver = _manifest_version(zf)
    mods, deps = [], []
    for mod in meta.get("mods", []):
        mid = mod.get("modId")
        if not mid:
            continue
        ver = str(mod.get("version", ""))
        if ver.startswith("${") or not ver:
            ver = jar_ver  # unresolved placeholder; None = unknown
        mods.append({"id": mid, "version": ver})
    for owner, entries in (meta.get("dependencies") or {}).items():
        if not isinstance(entries, list):
            continue
        for d in entries:
            kind = str(d.get("type", "required" if d.get("mandatory", True) else "optional")).lower()
            if "type" not in d and "mandatory" in d:
                kind = "required" if d["mandatory"] else "optional"
            if kind != "required" or not d.get("modId"):
                continue
            deps.append({"owner": owner, "id": d["modId"],
                         "range": d.get("versionRange") or "",
                         "side": str(d.get("side", "BOTH")).upper()})
    return mods, deps


def load_jar(label, data, depth=0):
    """Parse a jar (bytes). Returns {label, mods, deps, nested:[jar...]}."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise CheckError(f"{label}: not a valid jar ({e})")
    with zf:
        mods, deps = read_mods(zf)
        nested = []
        if JARJAR in zf.namelist() and depth < MAX_NEST:
            meta = json.loads(zf.read(JARJAR).decode("utf-8"))
            for j in meta.get("jars", []):
                path = j.get("path")
                if not path or path not in zf.namelist():
                    raise CheckError(f"{label}: jarjar entry {path!r} missing from jar")
                nested.append(load_jar(f"{label} > {path.rsplit('/', 1)[-1]}",
                                       zf.read(path), depth + 1))
    return {"label": label, "mods": mods, "deps": deps, "nested": nested}


def flatten(jar):
    yield jar
    for n in jar["nested"]:
        yield from flatten(n)


def count_nested(jars):
    return sum(len(list(flatten(j))) - 1 for j in jars)


# ------------------------------------------------------------------- closure

def analyze(jars, versions=None):
    """Return (rows, notes). rows: one per required dependency evaluated."""
    versions = versions or {}
    providers = {}
    for top in jars:
        for j in flatten(top):
            for m in j["mods"]:
                providers.setdefault(m["id"], []).append((m["version"], j["label"]))
    rows, notes = [], []
    for top in jars:
        for j in flatten(top):
            for d in j["deps"]:
                row = {"jar": j["label"], "owner": d["owner"], "dep": d["id"],
                       "range": d["range"], "side": d["side"]}
                if d["id"] in PLATFORM:
                    row["status"] = "platform"
                    want = versions.get("minecraft" if d["id"] == "minecraft" else d["id"])
                    if want and d["range"] and not in_range(want, d["range"]):
                        notes.append(f"{j['label']}: {d['id']} {d['range']} does not match "
                                     f"pack {d['id']} {want} (noted, not failed; such ranges can still load)")
                    rows.append(row)
                    continue
                found = [p for p in providers.get(d["id"], []) if p[1] != j["label"]]
                if not found:
                    row["status"] = "missing"
                    row["detail"] = "[MISSING]"
                else:
                    ok = [p for p in found if p[0] is None or in_range(p[0], d["range"])]
                    if ok:
                        row["status"] = "satisfied"
                        row["detail"] = f"{ok[0][0] or 'unknown version'} from {ok[0][1]}"
                    else:
                        row["status"] = "version"
                        row["detail"] = "have " + ", ".join(f"{p[0]} ({p[1]})" for p in found)
                rows.append(row)
    return rows, notes


def run(mods_dir, pack_toml, fetch=download, out=print, strict=False):
    pins = read_pins(mods_dir)
    if not pins:
        out(f"ERROR: no *.pw.toml pins found in {mods_dir}")
        return 1
    failures, jars = 0, []
    for pin in pins:
        try:
            jars.append(load_jar(pin["filename"], fetch_verified(pin, fetch)))
        except CheckError as e:
            out(f"FAIL  {e}")
            failures += 1
    rows, notes = analyze(jars, pack_versions(pack_toml))
    for r in rows:
        if r["status"] == "platform":
            continue
        tag = {"satisfied": "OK   ", "missing": "FAIL ", "version": "FAIL "}[r["status"]]
        out(f"{tag} {r['owner']} ({r['jar']}) requires {r['dep']} {r['range'] or '*'}: "
            f"{r['status'].upper()} - {r['detail']}")
        failures += r["status"] != "satisfied"
    for n in notes:
        out(f"NOTE  {n}")
    checked = [r for r in rows if r["status"] != "platform"]
    satisfied = sum(r["status"] == "satisfied" for r in checked)
    nested = count_nested(jars)
    out(f"Summary: {len(jars)}/{len(pins)} jars read, {nested} nested jars, "
        f"{len(checked)} required non-platform deps checked, {satisfied} satisfied, "
        f"{failures} failure(s)")
    warnings = 0
    if jars and nested == 0:
        out("WARN  sweep found no nested (jarjar) jars at all; implausible for a real pack, "
            "so the sweep itself is suspect")
        warnings += 1
    if jars and checked and satisfied == 0 and failures == 0:
        out("WARN  no required dependency resolved as satisfied; control failed")
        warnings += 1
    return 1 if failures or (strict and warnings) else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--mods-dir", default="mods")
    p.add_argument("--pack", default="pack.toml")
    p.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = p.parse_args(argv)
    return run(args.mods_dir, args.pack, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
