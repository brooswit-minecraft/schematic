"""SCHEM-38 evidence: exercise backup()/archive_and_prune() end-to-end over a
REAL SFTP protocol server on loopback (not the FakeSFTP used by
test_server_deploy.py) and a mock RCON server that speaks the real Source
RCON wire protocol, also on loopback. No live host is contacted anywhere in
this file — every socket here binds to 127.0.0.1 on an OS-assigned port.

Requires paramiko (the same dependency reusable-server-update.yml's sftp job
already installs: `pip install paramiko==4.0.0`); this whole module is
skipped, not failed, when it isn't installed, so test_server_deploy.py's own
suite (which needs no such dependency) is unaffected either way.

Run directly for the evidence log this proves (order of RCON commands,
resulting archive listing):

    python3 -m unittest tests.test_server_backup_loopback -v
"""

import importlib.util
import os
from pathlib import Path
import posixpath
import socket
import struct
import tarfile
import tempfile
import threading
import time
import unittest

SPEC = importlib.util.spec_from_file_location("deploy", Path(__file__).parents[1] / "scripts/server_deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

try:
    import paramiko
    HAVE_PARAMIKO = True
except ImportError:
    HAVE_PARAMIKO = False


if HAVE_PARAMIKO:

    class ChrootSFTPServer(paramiko.SFTPServerInterface):
        """Serves real SFTP protocol requests, backed by a real local
        directory tree — a minimal reference server, not a mock: every
        operation is a genuine filesystem call, so a client bug (like a
        wrong flag or an unhandled server_deploy.py path) fails exactly as
        it would against a real SFTP host."""

        def __init__(self, server, root, *largs, **kwargs):
            super().__init__(server, *largs, **kwargs)
            self.root = str(root)

        def _real(self, path):
            return self.root + posixpath.normpath("/" + path)

        def list_folder(self, path):
            try:
                names = os.listdir(self._real(path))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)
            out = []
            for name in names:
                attr = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(self._real(path), name)))
                attr.filename = name
                out.append(attr)
            return out

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)

        def lstat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)

        def open(self, path, flags, attr):
            real = self._real(path)
            try:
                fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o644)
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)
            if flags & os.O_WRONLY:
                mode = "ab" if flags & os.O_APPEND else "wb"
            elif flags & os.O_RDWR:
                mode = "rb+"
            else:
                mode = "rb"
            handle = paramiko.SFTPHandle(flags)
            fileobj = os.fdopen(fd, mode)
            handle.readfile = fileobj
            handle.writefile = fileobj
            return handle

        def remove(self, path):
            try:
                os.remove(self._real(path))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)
            return paramiko.SFTP_OK

        def rename(self, oldpath, newpath):
            try:
                os.rename(self._real(oldpath), self._real(newpath))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)
            return paramiko.SFTP_OK

        def posix_rename(self, oldpath, newpath):
            try:
                os.replace(self._real(oldpath), self._real(newpath))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)
            return paramiko.SFTP_OK

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._real(path))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)
            return paramiko.SFTP_OK

        def rmdir(self, path):
            try:
                os.rmdir(self._real(path))
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno)
            return paramiko.SFTP_OK

        def chattr(self, path, attr):
            return paramiko.SFTP_OK

        def canonicalize(self, path):
            return posixpath.normpath("/" + path)

    class BackupSSHServer(paramiko.ServerInterface):
        def __init__(self, username, password):
            self.username = username
            self.password = password

        def check_channel_request(self, kind, chanid):
            if kind == "session":
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
            if (username, password) == (self.username, self.password):
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

    class LoopbackSFTPHost:
        """A real SSH+SFTP server on 127.0.0.1, backed by a real local
        directory standing in for the remote server root."""

        def __init__(self, root, username="deploy", password="s3cr3t-sftp"):
            self.root = root
            self.username = username
            self.password = password
            self.host_key = paramiko.RSAKey.generate(2048)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(5)
            self.host, self.port = self.sock.getsockname()
            self._stop = False
            self._thread = threading.Thread(target=self._accept_loop, daemon=True)

        def known_hosts_line(self):
            return "[{}]:{} {} {}".format(self.host, self.port, self.host_key.get_name(),
                                           self.host_key.get_base64())

        def start(self):
            self._thread.start()

        def _accept_loop(self):
            self.sock.settimeout(0.5)
            while not self._stop:
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                threading.Thread(target=self._serve_connection, args=(conn,), daemon=True).start()

        def _serve_connection(self, conn):
            transport = paramiko.Transport(conn)
            try:
                transport.add_server_key(self.host_key)
                transport.set_subsystem_handler("sftp", paramiko.SFTPServer, ChrootSFTPServer, self.root)
                transport.start_server(server=BackupSSHServer(self.username, self.password))
                channel = transport.accept(20)
                if channel is None:
                    return
                deadline = time.monotonic() + 30
                while transport.is_active() and time.monotonic() < deadline:
                    time.sleep(0.05)
            except Exception:
                pass
            finally:
                transport.close()

        def stop(self):
            self._stop = True
            self.sock.close()


class LoopbackRCONHost:
    """Speaks the real Source RCON binary wire protocol on 127.0.0.1 — a
    mock RCON server (there is no real Minecraft process here), but the
    bytes on the wire, and server_deploy.py's rcon_command() client code
    that reads/writes them, are exactly what production uses. Records every
    non-auth command it receives, in order, for evidence."""

    def __init__(self, password="s3cr3t-rcon"):
        self.password = password
        self.commands = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.host, self.port = self.sock.getsockname()
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self):
        self._thread.start()

    def _accept_loop(self):
        self.sock.settimeout(0.5)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._serve_connection(conn)
            finally:
                conn.close()

    @staticmethod
    def _recv_exact(conn, size):
        chunks = []
        while size:
            chunk = conn.recv(size)
            if not chunk:
                raise EOFError("connection closed")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def _serve_connection(self, conn):
        conn.settimeout(5)
        while True:
            try:
                header = self._recv_exact(conn, 4)
            except (EOFError, OSError, socket.timeout):
                return
            length = struct.unpack("<i", header)[0]
            payload = self._recv_exact(conn, length)
            request_id, packet_type = struct.unpack("<ii", payload[:8])
            body = payload[8:-2].decode("utf-8")
            if packet_type == 3:  # SERVERDATA_AUTH
                if body == self.password:
                    conn.sendall(deploy.rcon_packet(request_id, 2, ""))
                else:
                    conn.sendall(deploy.rcon_packet(-1, 2, ""))
                    return
            else:
                self.commands.append(body)
                conn.sendall(deploy.rcon_packet(request_id, 0, "OK"))

    def stop(self):
        self._stop = True
        self.sock.close()


@unittest.skipUnless(HAVE_PARAMIKO, "pip install paramiko==4.0.0 to run this real-protocol evidence test")
class RealProtocolBackupEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.remote_root = Path(self.tmp.name) / "remote"
        (self.remote_root / "server").mkdir(parents=True)

        self.sftp_host = LoopbackSFTPHost(self.remote_root)
        self.sftp_host.start()
        self.addCleanup(self.sftp_host.stop)

        self.rcon_host = LoopbackRCONHost()
        self.rcon_host.start()
        self.addCleanup(self.rcon_host.stop)

    def env(self, retention="3"):
        return {
            "SERVER_RCON_HOST": self.rcon_host.host,
            "SERVER_RCON_PORT": str(self.rcon_host.port),
            "SERVER_RCON_PASSWORD": self.rcon_host.password,
            "SERVER_SFTP_HOST": self.sftp_host.host,
            "SERVER_SFTP_PORT": str(self.sftp_host.port),
            "SERVER_SFTP_PATH": "/server",
            "SERVER_SFTP_USERNAME": self.sftp_host.username,
            "SERVER_SFTP_PASSWORD": self.sftp_host.password,
            "SERVER_SFTP_KNOWN_HOSTS": self.sftp_host.known_hosts_line(),
            "SERVER_BACKUP_RETENTION": retention,
        }

    def test_backup_end_to_end_over_real_sftp_and_rcon_protocols(self):
        world = self.remote_root / "server/world"
        world.mkdir()
        (world / "level.dat").write_bytes(b"leveldata")
        region = world / "region"
        region.mkdir()
        (region / "r.0.0.mca").write_bytes(os.urandom(8192))

        result = deploy.backup(self.env())

        self.assertEqual(self.rcon_host.commands, ["save-off", "save-all flush", "save-on"])

        backups_dir = self.remote_root / "server/backups"
        archive_path = backups_dir / result["archive"]
        self.assertTrue(archive_path.exists())
        with tarfile.open(archive_path) as tar:
            names = sorted(tar.getnames())
        self.assertEqual(names, ["world/level.dat", "world/region/r.0.0.mca"])
        self.assertFalse(list(backups_dir.glob(deploy.BACKUP_STAGE_PREFIX + "*")))

        print("\n--- SCHEM-38 real-protocol evidence: archive + verify ---")
        print("RCON commands received by the mock RCON server, in order:", self.rcon_host.commands)
        print("Archive written over real SFTP:", archive_path)
        print("Resulting backups/ listing:", sorted(p.name for p in backups_dir.iterdir()))

    def test_backup_retention_over_real_protocol_removes_only_the_oldest(self):
        world = self.remote_root / "server/world"
        world.mkdir()
        (world / "level.dat").write_bytes(b"leveldata")

        archives = []
        for _ in range(4):
            result = deploy.backup(self.env(retention="3"))
            archives.append(result["archive"])
            # backup_filename has one-second resolution; force distinct
            # timestamps so all 4 runs produce 4 distinctly-named archives.
            time.sleep(1.1)

        backups_dir = self.remote_root / "server/backups"
        remaining = sorted(p.name for p in backups_dir.glob("world-*"))
        self.assertEqual(len(remaining), 3)
        self.assertNotIn(archives[0], remaining, "the (N+1)th run must remove the OLDEST archive")
        self.assertEqual(set(remaining), set(archives[1:]))

        print("\n--- SCHEM-38 real-protocol evidence: retention ---")
        print("Archives created across 4 runs (retention=3), oldest first:", archives)
        print("Archives remaining on the host after the 4th run:", remaining)

    def test_backup_forced_failure_aborts_before_upload_and_still_saves_on(self):
        # Deliberately no "world" directory on the remote host at all, so
        # build_world_archive() raises before any file is ever uploaded.
        with self.assertRaisesRegex(ValueError, "not found"):
            deploy.backup(self.env())

        self.assertEqual(self.rcon_host.commands, ["save-off", "save-all flush", "save-on"],
                          "save-on must be issued even though the archive failed")
        backups_dir = self.remote_root / "server/backups"
        if backups_dir.exists():
            self.assertEqual(list(backups_dir.iterdir()), [], "a forced failure must leave no archive behind")

        print("\n--- SCHEM-38 real-protocol evidence: forced failure ---")
        print("RCON commands received (save-on still issued despite the failure):", self.rcon_host.commands)
        print("backups/ left behind by the failed run:",
              sorted(p.name for p in backups_dir.iterdir()) if backups_dir.exists() else "(directory not created)")


if __name__ == "__main__":
    unittest.main()
