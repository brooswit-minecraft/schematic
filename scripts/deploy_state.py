"""Durable deployment state and stale-release ordering (MINECRAFT-40).

A small JSON record, held on the server next to the pack-file manifest,
that survives across the separate CLI invocations (mark-requested, deploy,
restart, ...) that make up one rollout. It exists for two purposes:

1. Stale-release ordering: refuse to let an older release deploy over one
   that is already live (updating) or verified healthy. This is
   independent of, and in addition to, the `upload()` concurrency marker
   (.schematic-deploy-pending) in server_deploy.py, which only detects an
   *in-progress* upload, not version ordering between two
   completed-or-in-flight attempts.
2. Durable states (requested / updating / healthy / failed / rolled_back)
   for the most recent ATTEMPT, with enough evidence (version, timestamp)
   that an operator or a later run can tell what actually happened without
   re-deriving it from logs.

These two purposes are deliberately tracked by two SEPARATE fields, not
one. A single "current attempt" field is not enough for (1): once a later
attempt's own requested/failed record overwrites it, the version-ordering
guard would have nothing left to compare against and would silently stop
protecting whatever is actually live (caught in PR #63 review: a sequence
like healthy(2) -> requested(3) -> stale candidate(1) must still reject
the stale candidate("1"), even though the "current attempt" by then
reads requested/3, not healthy/2). So `installed_version` is a separate,
monotonically-non-decreasing high-water mark — the highest version this
record has ever recorded as `updating` or `healthy` — that every write
carries forward untouched unless the write ITSELF is advancing it. A
`requested`/`failed`/`rolled_back` write for some other version can
freely update "what the current attempt is," but can never move
`installed_version` backwards or clear it.

Written with the same stage-then-posix_rename atomic-promotion pattern
server_deploy.py already uses for the pack manifest and world-archive
files, so a crash mid-write never leaves a half-written state file.
"""

import json
import re
import stat as stat_module
import uuid


STATE_FILE = ".schematic-deploy-state.json"

REQUESTED = "requested"
UPDATING = "updating"
HEALTHY = "healthy"
FAILED = "failed"
ROLLED_BACK = "rolled_back"
STATES = (REQUESTED, UPDATING, HEALTHY, FAILED, ROLLED_BACK)

# Writing one of these states for version V advances the durable
# installed_version high-water mark to V (if V is higher than what it
# already records) — these are the only states under which the server's
# pack files reflect, or are actively being made to reflect, V.
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
    if not stat_module.S_ISREG(attr.st_mode):
        raise ValueError("Remote deployment-state path is not a regular file")
    with sftp.open(path, "rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Remote deployment-state file too large")
    record = json.loads(raw)
    if (record.get("schema") != 2 or record.get("state") not in STATES
            or not isinstance(record.get("version"), str) or not isinstance(record.get("updated_at"), str)
            or not isinstance(record.get("installed_version"), (str, type(None)))):
        raise ValueError("Invalid deployment-state record")
    parse_version(record["version"])
    if record["installed_version"] is not None:
        parse_version(record["installed_version"])
    return record


def write_state(sftp, root, version, state, now):
    """Atomically replace the durable deployment-state record for the
    CURRENT ATTEMPT (`version`, `state`), after reading back the existing
    record (if any) to carry its `installed_version` high-water mark
    forward — advancing it to `version` when `state` is UPDATING/HEALTHY
    and it is not already >= `version`, otherwise leaving it untouched.
    `now` is a required callable (datetime.now(timezone.utc)-shaped
    caller) so tests control the timestamp; no default resolved at import
    time."""
    if state not in STATES:
        raise ValueError("Invalid deployment state: " + repr(state))
    parse_version(version)
    root = root.rstrip("/")
    existing = read_state(sftp, root)
    installed_version = existing["installed_version"] if existing else None
    if state in LIVE_STATES and (installed_version is None or parse_version(version) > parse_version(installed_version)):
        installed_version = version
    record = {"schema": 2, "version": version, "state": state,
              "installed_version": installed_version, "updated_at": now().isoformat()}
    temporary = root + "/" + STATE_FILE + ".tmp-" + uuid.uuid4().hex
    with sftp.open(temporary, "wb") as target:
        target.write(json.dumps(record, sort_keys=True).encode() + b"\n")
    sftp.posix_rename(temporary, root + "/" + STATE_FILE)
    return record


def guard_stale_release(sftp, root, candidate_version):
    """Raise ValueError if `candidate_version` is older than the durable
    installed_version high-water mark — the highest version ever recorded
    as updating/healthy, which (unlike the current attempt's own
    version/state) survives being overwritten by a later requested/failed
    attempt for some other version. A release equal to installed_version
    IS allowed through (an ordinary retry of the currently-live version);
    only a strictly older candidate is stale. No record yet, or a record
    whose installed_version is still None (nothing has ever reached
    updating/healthy), blocks nothing."""
    current = read_state(sftp, root)
    if current is None or current["installed_version"] is None:
        return
    if parse_version(candidate_version) < parse_version(current["installed_version"]):
        raise ValueError(
            "Stale release: version %s is older than version %s, already installed; "
            "refusing to deploy an older release over it"
            % (candidate_version, current["installed_version"]))
