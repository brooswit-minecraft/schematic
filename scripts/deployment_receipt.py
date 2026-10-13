"""Bind a verified restart to the exact deployed release archive."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import zipfile


def build_receipt(archive, manifest_path, server, env):
    if env.get("RESTART_OUTCOME") != "success":
        raise ValueError("A verified successful restart is required")
    if not server or any(c.isspace() for c in server):
        raise ValueError("An explicit game server address is required")
    manifest = json.loads(Path(manifest_path).read_text())
    with zipfile.ZipFile(archive) as pack:
        index = json.loads(pack.read("modrinth.index.json"))
    if (manifest.get("schema") != 1 or index.get("formatVersion") != 1
            or index.get("game") != "minecraft"
            or not manifest.get("version")
            or index.get("versionId") != manifest["version"]
            or not manifest.get("dependencies")
            or index.get("dependencies") != manifest["dependencies"]):
        raise ValueError("Deployment manifest does not match the release archive")
    with Path(archive).open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "schemaVersion": 1,
        "server": server,
        "sha256": digest,
        "dependencies": manifest["dependencies"],
        "ready": True,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "version": manifest["version"],
        "repository": env.get("GITHUB_REPOSITORY"),
        "runId": env.get("GITHUB_RUN_ID"),
        "runAttempt": env.get("GITHUB_RUN_ATTEMPT"),
        "workflowCommit": env.get("GITHUB_SHA"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "manifest", "server", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    receipt = build_receipt(args.archive, args.manifest, args.server, os.environ)
    Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
