"""Durable deployment state and stale-release ordering (MINECRAFT-40).

A small JSON record, held on the server next to the pack-file manifest,
that survives across the separate CLI invocations (deploy, then restart)
that make up one rollout. It exists for two purposes:

1. Stale-release ordering: refuse to let an older or equal-version release
   overwrite a release that is already live (updating) or verified healthy.
   This is independent of, and in addition to, the `upload()` concurrency
   marker (.schematic-deploy-pending) in server_deploy.py, which only
   detects an *in-progress* upload, not version ordering between two
   completed-or-in-flight attempts.
2. Durable states (requested / updating / healthy / failed / rolled_back)
   with enough evidence (version, timestamp) that an operator or a later
   run can tell what actually happened without re-deriving it from logs.

Written with the same stage-then-posix_rename atomic-promotion pattern
server_deploy.py already uses for the pack manifest and world-archive
files, so a crash mid-write never leaves a half-written state file.
"""

import json
import re
import uuid


STATE_FILE = ".schematic-deploy-state.json"

REQUESTED = "requested"
UPDATING = "updating"
HEALTHY = "healthy"
FAILED = "failed"
ROLLED_BACK = "rolled_back"
STATES = (REQUESTED, UPDATING, HEALTHY, FAILED, ROLLED_BACK)

# States under which the server's pack files reflect (or are actively being
# made to reflect) the recorded version. An older/equal candidate must not
# deploy over either of these without an explicit newer version.
LIVE_STATES = (UPDATING, HEALTHY)


def parse_version(value):
    """Parse a dotted-numeric version ("0.53.2") into a tuple of ints for
    ordering comparison. Deliberately strict: this guards production
    deploy ordering, so an unparseable version must fail loudly rather than
    silently compare as equal/less via string comparison."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+)*", value or ""):
        raise ValueError("Unparseable release version for ordering comparison: " + repr(value))
    return tuple(int(part) for part in value.split("."))


def read_state(sftp, root):
    """Read the durable deployment-state record, or None if absent. Mirrors
    server_deploy.read_manifest's validation posture: a present-but-invalid
    record is a hard error, never silently treated as absent."""
    path = root.rstrip("/") + "/" + STATE_FILE
    try:
        attr = sftp.lstat(path)
    except OSError as error:
        if error.errno == 2:
            return None
        raise
    import stat as stat_module
    if not stat_module.S_ISREG(attr.st_mode):
        raise ValueError("Remote deployment-state path is not a regular file")
    with sftp.open(path, "rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Remote deployment-state file too large")
    record = json.loads(raw)
    if (record.get("schema") != 1 or record.get("state") not in STATES
            or not isinstance(record.get("version"), str) or not isinstance(record.get("updated_at"), str)):
        raise ValueError("Invalid deployment-state record")
    parse_version(record["version"])
    return record


def write_state(sftp, root, version, state, now):
    """Atomically replace the durable deployment-state record. `now` is a
    required callable (datetime.now(timezone.utc)-shaped caller) so tests
    control the timestamp; no default resolved at import time."""
    if state not in STATES:
        raise ValueError("Invalid deployment state: " + repr(state))
    parse_version(version)
    root = root.rstrip("/")
    record = {"schema": 1, "version": version, "state": state,
              "updated_at": now().isoformat()}
    temporary = root + "/" + STATE_FILE + ".tmp-" + uuid.uuid4().hex
    with sftp.open(temporary, "wb") as target:
        target.write(json.dumps(record, sort_keys=True).encode() + b"\n")
    sftp.posix_rename(temporary, root + "/" + STATE_FILE)
    return record


def guard_stale_release(sftp, root, candidate_version):
    """Raise ValueError if `candidate_version` must not deploy because the
    server already has an equal-or-newer release updating or healthy.
    A release equal to the currently live version IS allowed through (a
    legitimate retry of the same release), only a strictly older candidate
    is stale. A prior failed/rolled_back/requested-only record never blocks
    a retry at any version, since no live content is pinned to it."""
    current = read_state(sftp, root)
    if current is None or current["state"] not in LIVE_STATES:
        return
    if parse_version(candidate_version) < parse_version(current["version"]):
        raise ValueError(
            "Stale release: version %s is older than the %s release already %s; "
            "refusing to deploy an older release over it"
            % (candidate_version, current["version"], current["state"]))
