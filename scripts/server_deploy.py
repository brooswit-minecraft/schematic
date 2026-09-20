#!/usr/bin/env python3
"""Materialize a release mrpack and deploy pack-owned files to a server."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import struct
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile


MANIFEST = ".schematic-deploy.json"
PENDING = ".schematic-deploy-pending"
SUPERVISOR = ".schematic-supervisor.sh"
SUPERVISOR_BODY = b"""#!/bin/sh
set -u
cd "$(dirname "$0")"

# Minecraft exits normally after an RCON stop. Keep the hosting process alive
# so release automation can relaunch it without a privileged Hosting token.
child=""
shutdown() {
  trap - TERM INT HUP
  if [ -n "$child" ]; then
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
  exit 0
}
trap shutdown TERM INT HUP

failures=0
while :; do
  started=$(date +%s)
  ./run.sh nogui &
  child=$!
  wait "$child"
  status=$?
  child=""
  runtime=$(($(date +%s) - started))
  if [ "$runtime" -ge 60 ]; then
    failures=0
    delay=5
  else
    failures=$((failures + 1))
    if [ "$failures" -ge 5 ]; then
      printf 'Minecraft failed %s times during startup; supervisor exiting.\\n' "$failures" >&2
      exit "$status"
    fi
    delay=$((10 << (failures - 1)))
  fi
  printf 'Minecraft exited with status %s after %ss; restarting in %ss.\\n' "$status" "$runtime" "$delay" >&2
  sleep "$delay"
done
"""
MAX_FILE = 1024 * 1024 * 1024
MAX_TOTAL = 4 * MAX_FILE
PROTECTED = {
    "world", "worlds", "world_nether", "world_the_end", "backups", "logs",
    "crash-reports", "server.properties", "eula.txt", "ops.json", "whitelist.json",
    "banned-ips.json", "banned-players.json", "usercache.json", "session.lock",
}
BACKUP_SUFFIX = ".tar.gz"
BACKUP_STAGE_PREFIX = ".schematic-backup-stage-"
DEFAULT_BACKUP_RETENTION = 5


class RCONError(Exception):
    """The RCON peer returned an invalid response."""


class RCONAuthError(RCONError):
    """The RCON peer rejected the configured password."""


def rcon_packet(request_id, packet_type, body):
    if "\0" in body:
        raise ValueError("RCON payload contains a null byte")
    payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\0\0"
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError("RCON payload is too large")
    return struct.pack("<i", len(payload)) + payload


def receive_exact(connection, size):
    chunks = []
    while size:
        chunk = connection.recv(size)
        if not chunk:
            raise EOFError("RCON connection closed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def receive_rcon_packet(connection):
    length = struct.unpack("<i", receive_exact(connection, 4))[0]
    if not 10 <= length <= 4 * 1024 * 1024:
        raise RCONError("Invalid RCON packet length")
    payload = receive_exact(connection, length)
    request_id, packet_type = struct.unpack("<ii", payload[:8])
    if payload[-2:] != b"\0\0":
        raise RCONError("Invalid RCON packet terminator")
    try:
        body = payload[8:-2].decode("utf-8")
    except UnicodeDecodeError as error:
        raise RCONError("Invalid RCON response encoding") from error
    return request_id, packet_type, body


def rcon_command(host, port, password, command, timeout=10, connect=socket.create_connection):
    with connect((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(rcon_packet(1, 3, password))
        request_id, packet_type, _ = receive_rcon_packet(connection)
        if request_id == -1:
            raise RCONAuthError("RCON authentication failed")
        if request_id != 1 or packet_type != 2:
            raise RCONError("Unexpected RCON authentication response")
        connection.sendall(rcon_packet(2, 2, command))
        request_id, packet_type, body = receive_rcon_packet(connection)
        if request_id != 2 or packet_type != 0:
            raise RCONError("Unexpected RCON command response")
        return body


def rcon_config(env):
    if not env.get("SERVER_RCON_HOST") or not env.get("SERVER_RCON_PASSWORD"):
        raise ValueError("Missing required RCON configuration")
    port = int(env.get("SERVER_RCON_PORT") or "25575")
    timeout = float(env.get("SERVER_RCON_SHUTDOWN_TIMEOUT") or "120")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid RCON port")
    if not 1 <= timeout <= 600:
        raise ValueError("Invalid RCON shutdown timeout")
    return env["SERVER_RCON_HOST"], port, env["SERVER_RCON_PASSWORD"], timeout


def stop_and_wait(env, command=rcon_command, sleep=time.sleep, monotonic=time.monotonic):
    host, port, password, shutdown_timeout = rcon_config(env)
    command(host, port, password, "stop")
    deadline = monotonic() + shutdown_timeout
    unavailable = 0
    while monotonic() < deadline:
        sleep(1)
        try:
            command(host, port, password, "list", timeout=5)
            unavailable = 0
        except RCONAuthError:
            raise
        except (OSError, EOFError, TimeoutError):
            unavailable += 1
            if unavailable >= 2:
                return
    raise TimeoutError("Minecraft RCON remained available after stop command")


def wait_until_ready(env, command=rcon_command, sleep=time.sleep, monotonic=time.monotonic):
    host, port, password, startup_timeout = rcon_config(env)
    deadline = monotonic() + startup_timeout
    while monotonic() < deadline:
        try:
            return command(host, port, password, "list", timeout=5)
        except RCONAuthError:
            raise
        except (OSError, EOFError, TimeoutError):
            sleep(2)
    raise TimeoutError("Minecraft RCON did not become ready after runtime refresh")


def wait_for_restart(env, command=rcon_command, sleep=time.sleep, monotonic=time.monotonic):
    host, port, password, restart_timeout = rcon_config(env)
    deadline = monotonic() + restart_timeout
    while monotonic() < deadline:
        try:
            command(host, port, password, "list", timeout=5)
            sleep(1)
        except RCONAuthError:
            raise
        except (OSError, EOFError, TimeoutError):
            return wait_until_ready(env, command=command, sleep=sleep, monotonic=monotonic)
    raise TimeoutError("Minecraft RCON never went offline for the managed restart")


def restart_and_wait(env, command=rcon_command, sleep=time.sleep, monotonic=time.monotonic):
    host, port, password, _ = rcon_config(env)
    # Flush world state before stopping. A failed save must abort the rollout.
    command(host, port, password, "save-all flush")
    command(host, port, password, "stop")
    return wait_for_restart(env, command=command, sleep=sleep, monotonic=monotonic)


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid pack path")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise ValueError("Unsafe or hidden pack path")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Control character in pack path")
    if parts[0].lower() in PROTECTED:
        raise ValueError("Pack attempts to manage protected server data")
    return value


# Validates a single untrusted path SEGMENT (a level-name read from remote
# server.properties, or one filename entry from a remote directory listing)
# rather than a whole "/"-joined pack path — safe_path's own PROTECTED check
# would wrongly reject a level name of "world", which is exactly the value
# a default server has and must accept.
def safe_component(value):
    if not isinstance(value, str) or not value or "/" in value or "\\" in value or ":" in value:
        raise ValueError("Invalid path component")
    if value in (".", "..") or value.startswith("."):
        raise ValueError("Unsafe or hidden path component")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Control character in path component")
    return value


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("Download redirected away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Pack downloads must use HTTPS")
    return urllib.request.build_opener(HTTPSRedirect()).open(url, timeout=60)


def copy_checked(source, target, expected=None):
    hashes = {name: hashlib.new(name) for name in ("sha1", "sha512")}
    size = 0
    with target.open("wb") as out:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE or (expected and size > expected["fileSize"]):
                raise ValueError("Pack file exceeds declared or allowed size")
            out.write(chunk)
            for digest in hashes.values():
                digest.update(chunk)
    if expected:
        if size != expected["fileSize"] or any(
            hashes[name].hexdigest() != expected["hashes"][name].lower() for name in hashes
        ):
            raise ValueError("Pack download failed size/hash verification")
    return size


def file_hash(stream):
    digest = hashlib.sha512()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_FILE:
            raise ValueError("Remote file exceeds verification limit")
        digest.update(chunk)
    return digest.hexdigest()


# Unlike file_hash above (capped at MAX_FILE for pack-file download integrity,
# where an untrusted server response inflating a declared size is the threat
# being guarded against), a world archive is our own locally-built file and
# its own remote copy — legitimately larger than MAX_FILE for a real world —
# so this intentionally has no size ceiling.
def stream_hash(stream):
    digest = hashlib.sha512()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def local_hash(path):
    with open(path, "rb") as source:
        return stream_hash(source)


def materialize(archive, output, version, fetch=download):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Materialization directory must be empty")
    total = 0
    with zipfile.ZipFile(archive) as pack:
        names = pack.namelist()
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError("Duplicate or excessive archive entries")
        if pack.getinfo("modrinth.index.json").file_size > 16 * 1024 * 1024:
            raise ValueError("Pack index too large")
        index = json.loads(pack.read("modrinth.index.json"))
        if index.get("formatVersion") != 1 or index.get("game") != "minecraft":
            raise ValueError("Unsupported mrpack format")
        if index.get("versionId") != version:
            raise ValueError("Release and mrpack version differ")
        seen = set()
        for entry in index["files"]:
            path = safe_path(entry["path"])
            if path in seen:
                raise ValueError("Duplicate pack file")
            seen.add(path)
            support = entry.get("env", {}).get("server", "required")
            if support not in ("required", "optional", "unsupported"):
                raise ValueError("Invalid server environment")
            if support == "unsupported":
                continue
            if type(entry.get("fileSize")) is not int or not 0 <= entry["fileSize"] <= MAX_FILE:
                raise ValueError("Invalid download size")
            for name, length in (("sha1", 40), ("sha512", 128)):
                if not re.fullmatch(r"[0-9a-fA-F]{%d}" % length, entry.get("hashes", {}).get(name, "")):
                    raise ValueError("Missing or invalid download hash")
            target = output / path
            target.parent.mkdir(parents=True, exist_ok=True)
            downloaded = False
            for url in entry["downloads"]:
                try:
                    with fetch(url) as source:
                        size = copy_checked(source, target, entry)
                    downloaded = True
                    break
                except Exception:
                    target.unlink(missing_ok=True)
            if not downloaded:
                raise ValueError("No verified download available for pack file")
            total += size
            if total > MAX_TOTAL:
                raise ValueError("Pack exceeds total size limit")
        # Server overrides take precedence, as required by the mrpack format.
        for prefix in ("overrides/", "server-overrides/"):
            for info in pack.infolist():
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue
                path = safe_path(info.filename[len(prefix):])
                if stat.S_ISLNK(info.external_attr >> 16) or info.file_size > MAX_FILE:
                    raise ValueError("Unsafe archive member")
                target = output / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with pack.open(info) as source:
                    total += copy_checked(source, target)
                if total > MAX_TOTAL:
                    raise ValueError("Pack exceeds total size limit")
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file():
            with path.open("rb") as source:
                files[path.relative_to(output).as_posix()] = file_hash(source)
    manifest = {"schema": 1, "version": version, "dependencies": index.get("dependencies", {}), "files": files}
    (output / MANIFEST).write_text(json.dumps(manifest, sort_keys=True) + "\n")
    return manifest


def attributes(sftp, path):
    try:
        return sftp.lstat(path)
    except OSError as error:
        if error.errno == 2:
            return None
        raise


def regular(sftp, path):
    attr = attributes(sftp, path)
    if attr and not stat.S_ISREG(attr.st_mode):
        raise ValueError("Remote file is not a regular file")
    return attr


def directory(sftp, path, create=False):
    current = ""
    for part in PurePosixPath(path).parts[1:]:
        current += "/" + part
        attr = attributes(sftp, current)
        if attr is None and create:
            sftp.mkdir(current)
        elif attr is None or not stat.S_ISDIR(attr.st_mode):
            raise ValueError("Remote directory missing or is a symlink/non-directory")


def read_manifest(sftp, path):
    if not regular(sftp, path):
        return {"schema": 1, "files": {}}
    with sftp.open(path, "rb") as source:
        raw = source.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Remote manifest too large")
    result = json.loads(raw)
    if result.get("schema") != 1 or not isinstance(result.get("files"), dict):
        raise ValueError("Invalid deployment manifest")
    for path, digest in result["files"].items():
        safe_path(path)
        if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{128}", digest):
            raise ValueError("Invalid manifest hash")
    return result


def remote_hash(sftp, path):
    with sftp.open(path, "rb") as source:
        return file_hash(source)


def upload(sftp, output, root):
    if not root.startswith("/") or any(p in (".", "..") for p in root.split("/")) or "\\" in root:
        raise ValueError("SFTP path must be an absolute server directory")
    root = root.rstrip("/")
    directory(sftp, root or "/")
    if attributes(sftp, root + "/" + PENDING):
        raise ValueError("Incomplete deployment marker exists; inspect/restore server before retrying")
    stage = root + "/.schematic-stage-" + uuid.uuid4().hex
    # Lock before reading ownership/content, including across repositories.
    # A failure retains this marker for operator inspection, never blind retry.
    with sftp.open(root + "/" + PENDING, "wx") as marker:
        marker.write(stage.encode())
    output = Path(output)
    new = json.loads((output / MANIFEST).read_text())
    old = read_manifest(sftp, root + "/" + MANIFEST)
    for path, digest in new["files"].items():
        safe_path(path)
        with (output / path).open("rb") as source:
            if file_hash(source) != digest:
                raise ValueError("Local staging file changed")
    # Never silently mix untracked mods into the release's mod set.
    def check_mods(path, relative="mods"):
        attr = attributes(sftp, path)
        if not attr:
            return
        if not stat.S_ISDIR(attr.st_mode):
            raise ValueError("Remote mods path is not a directory")
        for item in sftp.listdir_attr(path):
            child = relative + "/" + item.filename
            safe_path(child)
            if stat.S_ISDIR(item.st_mode):
                check_mods(path + "/" + item.filename, child)
            elif not stat.S_ISREG(item.st_mode) or (child not in old["files"] and child not in new["files"]):
                raise ValueError("Untracked remote mods must be backed up and removed before deployment")
    check_mods(root + "/mods")
    for path in old["files"].keys() | new["files"].keys():
        # Missing parents are fine on first deployment, existing ones must be safe.
        current = root
        for part in PurePosixPath(path).parts[:-1]:
            current += "/" + part
            attr = attributes(sftp, current)
            if attr is None:
                break
            directory(sftp, current)
        target = root + "/" + path
        if regular(sftp, target):
            digest = remote_hash(sftp, target)
            if digest not in (old["files"].get(path), new["files"].get(path)):
                raise ValueError("Remote file differs from managed/desired content; refusing overwrite")
    sftp.mkdir(stage)
    # Probe atomic overwrite support before touching live pack files.
    for name in ("probe-a", "probe-b"):
        with sftp.open(stage + "/" + name, "wb") as target:
            target.write(b"probe")
    sftp.posix_rename(stage + "/probe-a", stage + "/probe-b")
    sftp.remove(stage + "/probe-b")
    for path, digest in new["files"].items():
        target = stage + "/" + path
        directory(sftp, str(PurePosixPath(target).parent), create=True)
        sftp.put(str(output / path), target)
        if remote_hash(sftp, target) != digest:
            raise ValueError("SFTP staging hash verification failed")
    sftp.put(str(output / MANIFEST), stage + "/" + MANIFEST)
    for path in new["files"]:
        target = root + "/" + path
        directory(sftp, str(PurePosixPath(target).parent), create=True)
        sftp.posix_rename(stage + "/" + path, target)
    for path in old["files"].keys() - new["files"].keys():
        target = root + "/" + path
        if regular(sftp, target):
            sftp.remove(target)
    sftp.posix_rename(stage + "/" + MANIFEST, root + "/" + MANIFEST)
    sftp.remove(root + "/" + PENDING)
    for path in sorted({str(PurePosixPath(p).parent) for p in new["files"]}, key=len, reverse=True):
        while path != ".":
            try:
                sftp.rmdir(stage + "/" + path)
            except OSError:
                break
            path = str(PurePosixPath(path).parent)
    sftp.rmdir(stage)


def install_supervisor(sftp, root):
    root = root.rstrip("/")
    target = root + "/" + SUPERVISOR
    regular(sftp, target)
    temporary = target + ".tmp-" + uuid.uuid4().hex
    with sftp.open(temporary, "wb") as output:
        output.write(SUPERVISOR_BODY)
    sftp.chmod(temporary, 0o755)
    sftp.posix_rename(temporary, target)


def upload_config(env):
    required = ("SERVER_SFTP_HOST", "SERVER_SFTP_USERNAME", "SERVER_SFTP_PATH", "SERVER_SFTP_KNOWN_HOSTS")
    if any(not env.get(key) for key in required):
        raise ValueError("Missing required SFTP configuration")
    if bool(env.get("SERVER_SFTP_PASSWORD")) == bool(env.get("SERVER_SFTP_PRIVATE_KEY")):
        raise ValueError("Configure exactly one SFTP password or private key")
    port = int(env.get("SERVER_SFTP_PORT") or "22")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SFTP port")
    return port


@contextlib.contextmanager
def sftp_session(env):
    """Open an authenticated SFTP session. Shared by connect_upload (pack
    files) and connect_backup (world archive) so the connection/host-key/
    credential-file plumbing exists exactly once."""
    import paramiko
    port = upload_config(env)
    with tempfile.TemporaryDirectory() as temporary:
        host_file = Path(temporary) / "known_hosts"
        host_file.write_text(env["SERVER_SFTP_KNOWN_HOSTS"])
        host_file.chmod(0o600)
        key_file = None
        if env.get("SERVER_SFTP_PRIVATE_KEY"):
            key_file = Path(temporary) / "key"
            key_file.write_text(env["SERVER_SFTP_PRIVATE_KEY"])
            key_file.chmod(0o600)
        with paramiko.SSHClient() as client:
            client.load_host_keys(str(host_file))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(env["SERVER_SFTP_HOST"], port=port, username=env["SERVER_SFTP_USERNAME"],
                           password=env.get("SERVER_SFTP_PASSWORD") or None,
                           key_filename=str(key_file) if key_file else None,
                           allow_agent=False, look_for_keys=False, timeout=30,
                           auth_timeout=30, banner_timeout=30)
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(60)
                yield sftp


def connect_upload(output, env=os.environ):
    with sftp_session(env) as sftp:
        upload(sftp, output, env["SERVER_SFTP_PATH"])
        install_supervisor(sftp, env["SERVER_SFTP_PATH"])


def deploy(output, env=os.environ):
    # The v1 runtime refresh preserves a running server's power state. Keep it
    # running while atomically replacing pack-owned files, then let Hosting own
    # the stop/start lifecycle in the next workflow step.
    rcon_config(env)
    upload_config(env)
    wait_until_ready(env)
    connect_upload(output, env)


# --- Pre-deploy world archive (opt-in; SCHEM-38) -----------------------------
#
# SFTP has no server-side archive/copy capability, so a tarball cannot be
# built on the host: files are streamed off the host over SFTP straight into
# a tar.gz on the runner (build_world_archive), then that single file is
# uploaded back to <SERVER_SFTP_PATH>/backups/ under a temp name and promoted
# with the same atomic-rename pattern `upload()` above already uses for pack
# files, verifying the STAGED COPY's size/hash before promoting it, never
# after — a corrupt upload must never become the archive retention counts on.


def read_level_name(sftp, root, default="world"):
    """Read `level-name` from the remote server.properties, defaulting to
    "world" when the key or the file itself is absent. The value is
    attacker/operator-controlled remote content, so it is validated with
    safe_component before being used to build any path."""
    attr = attributes(sftp, root + "/server.properties")
    if attr is None or not stat.S_ISREG(attr.st_mode):
        return default
    with sftp.open(root + "/server.properties", "rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Remote server.properties too large")
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "level-name":
            continue
        value = value.strip()
        return safe_component(value) if value else default
    return default


def walk_remote_files(sftp, path, relative):
    """Recursively list regular files under a remote directory as
    (relative_posix_path, absolute_remote_path, size) triples. Every
    filename is validated with safe_component (rejecting traversal/hidden
    segments), and any symlink or other non-regular/non-directory entry
    aborts the walk rather than being silently skipped — the same
    no-symlink-escape posture `upload()`'s check_mods and materialize's
    override handling already take, applied to a listing this time instead
    of a zip/local tree."""
    for item in sftp.listdir_attr(path):
        name = safe_component(item.filename)
        child_path = path + "/" + item.filename
        child_relative = relative + "/" + name
        if stat.S_ISLNK(item.st_mode):
            raise ValueError("Refusing to archive a symlink: " + child_relative)
        elif stat.S_ISDIR(item.st_mode):
            yield from walk_remote_files(sftp, child_path, child_relative)
        elif stat.S_ISREG(item.st_mode):
            yield child_relative, child_path, item.st_size
        else:
            raise ValueError("Refusing to archive a non-regular file: " + child_relative)


def build_world_archive(sftp, root, level_name, tar_path):
    """Stream the level's world directories (level_name, level_name+"_nether",
    level_name+"_the_end" — whichever exist) into a tar.gz at tar_path. Not
    logs, jars or the pack. The primary level directory must exist; the
    nether/end companions are optional (a brand-new world may not have
    generated them yet)."""
    primary = root + "/" + level_name
    attr = attributes(sftp, primary)
    if attr is None:
        raise ValueError("World directory not found on server: " + level_name)
    if not stat.S_ISDIR(attr.st_mode):
        raise ValueError("World path exists but is not a directory: " + level_name)
    total = 0
    count = 0
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tar:
        for name in (level_name, level_name + "_nether", level_name + "_the_end"):
            dir_attr = attributes(sftp, root + "/" + name)
            if dir_attr is None:
                continue
            if not stat.S_ISDIR(dir_attr.st_mode):
                raise ValueError("World path exists but is not a directory: " + name)
            for child_relative, child_path, size in walk_remote_files(sftp, root + "/" + name, name):
                info = tarfile.TarInfo(name=child_relative)
                info.size = size
                info.mtime = int(time.time())
                info.mode = 0o644
                with sftp.open(child_path, "rb") as source:
                    tar.addfile(info, fileobj=source)
                total += size
                count += 1
    return total, count


def backup_filename(level_name, tag, when):
    """<level>-<UTC timestamp>-<tag>.tar.gz — sortable lexicographically in
    chronological order, and distinctive enough that retention (below) can
    never mistake an unrelated file in backups/ for one of its own."""
    timestamp = when.strftime("%Y%m%dT%H%M%SZ")
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag) if tag else "deploy"
    return f"{level_name}-{timestamp}-{safe_tag}{BACKUP_SUFFIX}"


def backup_pattern(level_name):
    return re.compile(r"\A" + re.escape(level_name) + r"-\d{8}T\d{6}Z-[A-Za-z0-9._-]+" +
                       re.escape(BACKUP_SUFFIX) + r"\Z")


def parse_retention(value):
    if value is None or not re.fullmatch(r"[0-9]+", str(value).strip()):
        raise ValueError("backup retention must be a positive integer, got: " + repr(value))
    retention = int(value)
    if retention < 1:
        raise ValueError("backup retention must be a positive integer (0 is not \"unlimited\"), got: " + str(retention))
    return retention


def enforce_backup_retention(sftp, backups_dir, level_name, retention):
    """Keep the `retention` most recent archives matching THIS level's own
    naming pattern; delete only those, never anything else in backups/.
    Called only after the new archive has been written and verified, so a
    failed run never shrinks the number of good backups."""
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 1:
        raise ValueError("backup retention must be a positive integer")
    pattern = backup_pattern(level_name)
    matches = sorted(
        item.filename for item in sftp.listdir_attr(backups_dir)
        if stat.S_ISREG(item.st_mode) and pattern.match(item.filename)
    )
    excess = matches[:-retention] if retention < len(matches) else []
    for name in excess:
        sftp.remove(backups_dir + "/" + name)
    return excess


def archive_and_prune(sftp, root, retention, tar_dir, tag="deploy", now=None):
    """The SFTP-side half of the backup: build the archive, upload it to a
    staged name, verify the STAGED copy's size/hash, promote it with an
    atomic rename only once verified, then enforce retention. Takes an sftp
    object directly (not a connection) so it is testable against FakeSFTP
    exactly like upload()/install_supervisor() above, independent of RCON or
    paramiko."""
    root = root.rstrip("/")
    if not root.startswith("/") or any(p in (".", "..") for p in root.split("/")) or "\\" in root:
        raise ValueError("SFTP path must be an absolute server directory")
    directory(sftp, root or "/")
    backups_dir = root + "/backups"
    directory(sftp, backups_dir, create=True)

    level_name = read_level_name(sftp, root)
    when = now() if callable(now) else (now or datetime.now(timezone.utc))
    filename = backup_filename(level_name, tag, when)
    tar_path = Path(tar_dir) / filename

    world_bytes, file_count = build_world_archive(sftp, root, level_name, tar_path)
    digest, archive_bytes = local_hash(tar_path)

    remote_target = backups_dir + "/" + filename
    if attributes(sftp, remote_target) is not None:
        raise ValueError("Backup archive name collision on remote host: " + filename)
    stage = backups_dir + "/" + BACKUP_STAGE_PREFIX + uuid.uuid4().hex
    sftp.put(str(tar_path), stage)
    with sftp.open(stage, "rb") as source:
        remote_digest, remote_size = stream_hash(source)
    if remote_digest != digest or remote_size != archive_bytes:
        sftp.remove(stage)
        raise ValueError("Backup upload failed size/hash verification")
    sftp.posix_rename(stage, remote_target)

    removed = enforce_backup_retention(sftp, backups_dir, level_name, retention)
    return {"archive": filename, "archive_bytes": archive_bytes, "world_bytes": world_bytes,
            "files": file_count, "removed": removed}


def connect_backup(root, retention, tar_dir, env, tag):
    with sftp_session(env) as sftp:
        return archive_and_prune(sftp, root, retention, tar_dir, tag=tag, now=None)


def backup_env_config(env):
    rcon_config(env)
    upload_config(env)
    if not env.get("SERVER_SFTP_PATH"):
        raise ValueError("Missing required SFTP configuration")
    retention = parse_retention(env.get("SERVER_BACKUP_RETENTION") or str(DEFAULT_BACKUP_RETENTION))
    return env["SERVER_SFTP_PATH"], retention


def backup(env=os.environ, command=None, connect=None):
    """Archive the world over the existing SFTP access, before any pack
    upload. Consistency: save-off + save-all flush before archiving,
    save-on GUARANTEED afterwards via try/finally — even when the archive
    itself fails — so a failed backup can never leave autosave disabled on
    the live world. Any exception here (including one raised while the
    world is quiesced) propagates to the caller, which — run as its own
    workflow step with no `continue-on-error`/`if: always()` — aborts the
    job before the next step (the pack upload) ever starts.

    `command`/`connect` default to None (resolved to the real
    rcon_command/connect_backup INSIDE the function body, not as a default
    argument value) so callers/tests can `mock.patch.object(deploy, ...)`
    them, the same pattern `deploy()` above relies on for wait_until_ready/
    connect_upload — a default argument value is bound once at import time
    and would not observe a later patch."""
    root, retention = backup_env_config(env)
    host, port, password, _ = rcon_config(env)
    tag = env.get("SERVER_BACKUP_TAG") or "deploy"
    caller = command or rcon_command
    connector = connect or connect_backup
    try:
        caller(host, port, password, "save-off")
        caller(host, port, password, "save-all flush")
        with tempfile.TemporaryDirectory() as tar_dir:
            return connector(root, retention, tar_dir, env, tag)
    finally:
        caller(host, port, password, "save-on")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("materialize", "deploy", "restart", "wait-ready", "wait-restarted", "backup"))
    parser.add_argument("--output")
    parser.add_argument("--archive")
    parser.add_argument("--version")
    args = parser.parse_args()
    try:
        if args.command == "materialize":
            if not args.archive or not args.version or not args.output:
                parser.error("materialize requires --archive, --version and --output")
            result = materialize(args.archive, args.output, args.version)
            print("Verified server files:", len(result["files"]))
            print("Required runtime dependencies:", json.dumps(result["dependencies"]))
        elif args.command == "deploy":
            if not args.output:
                parser.error("deploy requires --output")
            deploy(args.output)
            print("Running server verified and exact release files uploaded atomically.")
        elif args.command == "restart":
            print(restart_and_wait(os.environ))
        elif args.command == "wait-ready":
            print(wait_until_ready(os.environ))
        elif args.command == "backup":
            result = backup(os.environ)
            print("World archived:", result["archive"], "(", result["world_bytes"], "bytes,",
                  result["files"], "files )")
            if result["removed"]:
                print("Pruned old archives:", ", ".join(result["removed"]))
        else:
            print(wait_for_restart(os.environ))
    except Exception as error:
        # SSH/network exceptions may contain credentials, URLs or host details.
        print("Deployment failed (" + type(error).__name__ + "). Check RCON/SFTP configuration, pack integrity, remote ownership and pending marker.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
