#!/usr/bin/env python3
"""Materialize a release mrpack and deploy pack-owned files to a server."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import struct
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile


MANIFEST = ".schematic-deploy.json"
PENDING = ".schematic-deploy-pending"
MAX_FILE = 1024 * 1024 * 1024
MAX_TOTAL = 4 * MAX_FILE
PROTECTED = {
    "world", "worlds", "world_nether", "world_the_end", "backups", "logs",
    "crash-reports", "server.properties", "eula.txt", "ops.json", "whitelist.json",
    "banned-ips.json", "banned-players.json", "usercache.json", "session.lock",
}


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


def connect_upload(output, env=os.environ):
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
                upload(sftp, output, env["SERVER_SFTP_PATH"])


def deploy(output, env=os.environ):
    # The v1 runtime refresh preserves a running server's power state. Keep it
    # running while atomically replacing pack-owned files, then let Hosting own
    # the stop/start lifecycle in the next workflow step.
    rcon_config(env)
    upload_config(env)
    wait_until_ready(env)
    connect_upload(output, env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("materialize", "deploy", "wait-ready", "wait-restarted"))
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
        elif args.command == "wait-ready":
            print(wait_until_ready(os.environ))
        else:
            print(wait_for_restart(os.environ))
    except Exception as error:
        # SSH/network exceptions may contain credentials, URLs or host details.
        print("Deployment failed (" + type(error).__name__ + "). Check RCON/SFTP configuration, pack integrity, remote ownership and pending marker.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
