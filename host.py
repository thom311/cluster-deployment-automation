import socket
import subprocess
import io
import os
import re
import time
import shlex
import shutil
import sys
import logging
import tempfile
import uuid
from typing import Optional
from typing import Union
from collections.abc import Iterable
from collections.abc import Mapping
from functools import lru_cache
from ailib import Redfish
import paramiko
from paramiko import ssh_exception, RSAKey, Ed25519Key
from logger import logger
from abc import ABC, abstractmethod


def default_id_rsa_path() -> str:
    return os.path.join(os.environ["HOME"], ".ssh/id_rsa")


def default_ed25519_path() -> str:
    return os.path.join(os.environ["HOME"], ".ssh/id_ed25519")


class Result:
    def __init__(self, out: str, err: str, returncode: int):
        self.out = out
        self.err = err
        self.returncode = returncode

    def __str__(self) -> str:
        return f"(returncode: {self.returncode}, error: {self.err})"

    def success(self) -> bool:
        return self.returncode == 0


class Login(ABC):
    def __init__(self, hostname: str, username: str) -> None:
        self._username = username
        self._hostname = hostname
        self.host = paramiko.SSHClient()
        self.host.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def debug_details(self) -> str:
        return str({k: v for k, v in vars(self).items() if k not in ['_key', '_password']})

    @abstractmethod
    def login(self) -> paramiko.SSHClient:
        pass


class KeyLogin(Login):
    def __init__(self, hostname: str, username: str, key_path: str) -> None:
        super().__init__(hostname, username)
        self._key_path = key_path
        with open(key_path, "r") as f:
            self._key = f.read().strip()

        key_loader = self._key_loader()
        self._pkey = key_loader.from_private_key(io.StringIO(self._key))

    def _key_loader(self) -> Union[type[Ed25519Key], type[RSAKey]]:
        if self._is_rsa():
            return RSAKey
        else:
            return Ed25519Key

    def _is_rsa(self) -> bool:
        lh = LocalHost()
        result = lh.run(f"ssh-keygen -vvv -l -f {self._key_path}")
        logger.debug(result.out)
        return "---[RSA " in result.out

    def login(self) -> paramiko.SSHClient:
        logger.info(f"Logging in into {self._hostname} with {self._key_path}")
        self.host.connect(self._hostname, username=self._username, pkey=self._pkey, look_for_keys=False, allow_agent=False)
        return self.host


class PasswordLogin(Login):
    def __init__(self, hostname: str, username: str, password: str) -> None:
        super().__init__(hostname, username)
        self._password = password

    def login(self) -> paramiko.SSHClient:
        logger.info(f"Logging into {self._hostname} with password")
        self.host.connect(self._hostname, username=self._username, password=self._password, look_for_keys=False, allow_agent=False)
        return self.host


class AutoLogin(Login):
    def __init__(self, hostname: str, username: str) -> None:
        super().__init__(hostname, username)

    def login(self) -> paramiko.SSHClient:
        logger.info(f"Logging into {self._hostname} with Paramiko 'Auto key discovery' & 'Ssh-Agent'")
        self.host.connect(self._hostname, username=self._username, look_for_keys=True, allow_agent=True)
        return self.host


class BMC:
    def __init__(self, full_url: str, user: str = "root", password: str = "calvin"):
        self.url = full_url
        self.user = user
        self.password = password
        logger.info(f"{full_url} {user} {password}")

    @staticmethod
    def from_url(url: str, user: str = "root", password: str = "calvin") -> 'BMC':
        url = f"{url}/redfish/v1/Systems/System.Embedded.1"
        return BMC(url, user, password)

    @staticmethod
    def from_bmc(ip_or_hostname: str, user: str = "root", password: str = "calvin") -> 'BMC':
        if ip_or_hostname == "":
            logger.error("BMC not defined")
            sys.exit(-1)
        url = f"https://{ip_or_hostname}/redfish/v1/Systems/System.Embedded.1"
        return BMC(url, user, password)

    """
    Red Fish is used to boot ISO images with virtual media.
    Make sure redfish is enabled on your server. You can verify this by
    visiting the BMC's web address:
      https://<ip>/redfish/v1/Systems/System.Embedded.1 (For Dell)
    Red Fish uses HTTP POST messages to trigger actions. Some requires
    data. However the Red Fish library takes care of this for you.

    Red Fish heavily depends on iDRAC and IPMI working. For Dell servers:
    Log into iDRAC, default user is "root" and default password is "calvin".
     1) Try rebooting iDRAC
      a) Go to "Maintenance" tab at the top
      b) Go to the "Diagnostics" sub-tab below the "Maintenance" panel.
      c) Press the "Reboot iDRAC"
      d) Wait a while for iDRAC to come up.
      e) Once the web interface is available, go back to the "Dashboard" tab.
      f) Monitor the system to post after the "Dell" blue screen.
     2) Try upgrading firmware
      a) Go to "Maintenance" tab at the top
      b) Go to the "System Update" sub-tab below the "Maintenance" panel.
      c) Change the "Location Type" to "HTTP"
      d) Under the "HTTP Server Settings", set the "HTTP Address" to be
         "downloads.dell.com".
      e) Click "Check for Update".
      f) Depending on the missing updates, select what is needed then press
         "Install and Reboot"
      g) Wait a while for iDRAC to come up.
      h) Once the web interface is available, go back to the "Dashboard" tab.
      i) Monitor the system to post after the "Dell" blue screen.

    """

    def boot_iso_redfish(self, iso_path: str, retries: int = 10, retry_delay: int = 60) -> None:
        assert ":" in iso_path
        for attempt in range(retries):
            try:
                self.boot_iso_with_retry(iso_path)
                return
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                else:
                    time.sleep(retry_delay)

    def boot_iso_with_retry(self, iso_path: str) -> None:
        logger.info(iso_path)
        logger.info(f"Trying to boot {self.url} using {iso_path}")
        red = self._redfish()
        try:
            red.eject_iso()
        except Exception as e:
            logger.info(e)
            logger.info("eject failed, but continuing")
        logger.info(f"inserting iso {iso_path}")
        red.insert_iso(iso_path)
        try:
            red.set_iso_once()
        except Exception as e:
            logger.info(e)
            raise e

        logger.info("setting to boot from iso")
        red.restart()
        time.sleep(10)
        logger.info(f"Finished sending boot to {self.url}")

    def _redfish(self) -> Redfish:
        return Redfish(self.url, self.user, self.password, model='dell', debug=False)

    def stop(self) -> None:
        self._redfish().stop()

    def start(self) -> None:
        self._redfish().start()

    def cold_boot(self) -> None:
        self.stop()
        time.sleep(10)
        self.start()
        time.sleep(5)


class Host:
    def __new__(cls, hostname: str) -> 'Host':
        key = (cls, hostname)
        if key not in host_instances:
            host_instances[key] = super().__new__(cls)
            logger.debug(f"new instance for {hostname}")
        return host_instances[key]

    def __init__(self, hostname: str):
        self._hostname = hostname
        self._logins: list[Login] = []
        self.sudo_needed = False

    @lru_cache(maxsize=None)
    def is_localhost(self) -> bool:
        return self._hostname in ("localhost", socket.gethostname())

    @staticmethod
    def ssh_run_poll_result(stdout: paramiko.ChannelFile, stderr: paramiko.channel.ChannelStderrFile) -> Result:
        returncode = -1
        datas = [bytearray(), bytearray()]
        state = [0, 0]
        sources = (stdout, stderr)

        while state[0] != 2 and state[1] != 2:
            any_data = False
            for i in (0, 1):
                if state[i] == 2:
                    continue
                source = sources[i]
                channel = source.channel
                if i == 0:
                    while channel.recv_ready():
                        any_data = True
                        d = channel.recv(32768)
                        datas[i].extend(d)
                else:
                    while channel.recv_stderr_ready():
                        any_data = True
                        d = channel.recv_stderr(32768)
                        datas[i].extend(d)
                if state[i] == 1:
                    any_data = True
                    source.close()
                    if i == 0:
                        returncode = source.channel.recv_exit_status()
                    state[i] = 2
                elif channel.exit_status_ready():
                    # We don't finish right away. Try one more time to receive
                    # data. Note sure this is necessary. It shouldn't hurt.
                    any_data = True
                    state[i] = 1
            if not any_data:
                # Yes, naive polling with sleep. I guess, we could use
                # channel's fileno() for not busy looping.
                time.sleep(0.001)

        b_stdout, b_stderr = datas
        return Result(
            b_stdout.decode("utf-8", errors="strict"),
            b_stderr.decode("utf-8", errors="strict"),
            returncode,
        )

    @staticmethod
    def ssh_run(sshclient: paramiko.SSHClient, cmd: str, log_prefix: str, log_level: int = logging.DEBUG) -> Result:
        _, stdout, stderr = sshclient.exec_command(cmd)
        return Host.ssh_run_poll_result(stdout, stderr)

    def ssh_connect(self, username: str, password: Optional[str] = None, discover_auth: bool = True, rsa_path: str = default_id_rsa_path(), ed25519_path: str = default_ed25519_path()) -> None:
        assert not self.is_localhost()
        logger.info(f"waiting for '{self._hostname}' to respond to ping")
        self.wait_ping()
        logger.info(f"{self._hostname} up, connecting with {username}")

        self._logins = []

        if password is not None:
            pw = PasswordLogin(self._hostname, username, password)
            self._logins.append(pw)

        if os.path.exists(rsa_path):
            try:
                id_rsa = KeyLogin(self._hostname, username, rsa_path)
                self._logins.append(id_rsa)
            except Exception:
                pass

        if os.path.exists(ed25519_path):
            try:
                id_ed25519 = KeyLogin(self._hostname, username, ed25519_path)
                self._logins.append(id_ed25519)
            except Exception:
                pass

        if discover_auth:
            auto = AutoLogin(self._hostname, username)
            self._logins.append(auto)

        self.ssh_connect_looped(self._logins)

    def ssh_connect_looped(self, logins: list[Login], timeout: float = 3600) -> None:
        if not logins:
            raise RuntimeError("No usable logins found")

        login_details = ", ".join([login.debug_details() for login in logins])
        logger.info(f"Attempting SSH connections on {self._hostname} with logins: {login_details}")

        first_attempt = True
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            for login in logins:
                try:
                    self._host = login.login()
                    logger.info(f"Login successful on {self._hostname}")
                    return
                except (ssh_exception.AuthenticationException, ssh_exception.NoValidConnectionsError, ssh_exception.SSHException, socket.error, socket.timeout) as e:
                    if first_attempt:
                        logger.info(f"{type(e).__name__} - {str(e)} for login {login.debug_details()} on host {self._hostname}")
                        first_attempt = False
                    else:
                        logger.debug(f"{type(e).__name__} - {str(e)} for login {login.debug_details()} on host {self._hostname}")
                    time.sleep(10)
                except Exception as e:
                    logger.exception(f"SSH connect, login {login.debug_details()} user {login._username} on host {self._hostname}: {type(e).__name__} - {str(e)}")
                    raise e

        raise ConnectionError(f"Failed to establish an SSH connection to {self._hostname}")

    def _rsa_login(self) -> Optional[KeyLogin]:
        for x in self._logins:
            if isinstance(x, KeyLogin) and x._is_rsa():
                return x
        return None

    def remove(self, source: str) -> None:
        if self.is_localhost():
            if os.path.exists(source):
                os.remove(source)
        else:
            assert self._host is not None
            self.run(["rm", "-f", source])

    # Copying local_file to "Host", which can be local or remote
    def copy_to(
        self,
        src_file: str,
        dst_file: str,
        sudo: Optional[bool] = None,
    ) -> None:
        if not os.path.exists(src_file):
            raise FileNotFoundError(2, f"No such file or dir: {src_file}")
        if sudo is None:
            sudo = self.sudo_needed

        orig_dst = None
        if sudo:
            # With sudo, the file may not be writable as normal user.
            # Copy first to a temp location.
            orig_dst = dst_file
            dst_file = "/tmp/cda-" + str(uuid.uuid4()) + ".tmp"

        if self.is_localhost():
            shutil.copy(src_file, dst_file)
        else:
            while True:
                try:
                    sftp = self._host.open_sftp()
                    sftp.put(src_file, dst_file)
                    break
                except Exception as e:
                    logger.info(e)
                    logger.info("Disconnected during sftpd, reconnecting...")
                    self.ssh_connect_looped(self._logins)

        if orig_dst is not None:
            # The file should be owned by whoever is this sudoer. Create another
            # file, and copy over the owner:group that we got thereby.
            self.run(
                f"rc=0 ; cat {shlex.quote(dst_file)} > {shlex.quote(orig_dst)} || rc=$? ; rm -rf {shlex.quote(dst_file)} ; exit $rc ",
                sudo=sudo,
            )

    def need_sudo(self) -> None:
        self.sudo_needed = True

    def _cmd_to_script(self, cmd: str | Iterable[str]) -> str:
        # "run()" currently only supports that "cmd" is a shell script (passed
        # via "-c" argument).
        #
        # However, often the command is just a program name and arguments.
        # In that case, the user would have to take care to quote the string
        # like rsh.run(f"ls {shlex.quote(dir)}").
        #
        # For convenience, allow an alternative form rsh.run(["ls", dir])
        # which does the quoting internally.
        #
        # The command in that case is still run via shell and this is only
        # a convenience to build up the shell script with correct quoting.
        if isinstance(cmd, str):
            return cmd
        return shlex.join(cmd)

    def run(
        self,
        cmd: str | Iterable[str],
        log_level: int = logging.DEBUG,
        *,
        env: Optional[Mapping[str, Optional[str]]] = None,
        quiet: bool = False,
        cwd: Optional[str] = None,
        sudo: Optional[bool] = None,
        log_prefix: str = "",
        log_level_result: Optional[int] = None,
        log_level_fail: Optional[int] = None,
    ) -> Result:
        if sudo is None:
            sudo = self.sudo_needed

        cmd = self._cmd_to_script(cmd)

        if not quiet:
            logger.log(log_level, f"{log_prefix}running command {repr(cmd)} on {self._hostname}")
        if self.is_localhost():
            ret_val = self._run_local(cmd, env=env, quiet=quiet, cwd=cwd, sudo=sudo)
        else:
            ret_val = self._run_remote(cmd, log_level, env=env, quiet=quiet, cwd=cwd, sudo=sudo)

        if ret_val.returncode != 0 and log_level_fail is not None:
            level = log_level_fail
        elif log_level_result is not None:
            level = log_level_result
        else:
            level = log_level
        status = f"failed (rc={ret_val.returncode})" if ret_val.returncode != 0 else "succeeded"
        logger.log(level, f"{log_prefix}command {repr(cmd)} on {self._hostname} {status}{':' if ret_val.out or ret_val.err else ''}{f' out={repr(ret_val.out)};' if ret_val.out else ''}{f' err={repr(ret_val.err)};' if ret_val.err else ''}")

        return ret_val

    def _run_local(
        self,
        cmd: str,
        *,
        env: Optional[Mapping[str, Optional[str]]] = None,
        quiet: bool = False,
        cwd: Optional[str] = None,
        sudo: bool = False,
    ) -> Result:
        is_shell = True
        if sudo:
            is_shell = False
            argv = ["sudo", "sh", "-c", cmd]
        else:
            argv = None
        full_env: Optional[dict[str, str]] = None
        if env:
            if sudo:
                extra = []
                for k, v in env.items():
                    assert k == shlex.quote(k)
                    if v is not None:
                        extra.append(f"{k}={shlex.quote(v)}")
                assert argv
                argv = [argv[0], *extra, *(argv[1:])]
            else:
                full_env = os.environ.copy()
                for k, v in env.items():
                    if v is None:
                        full_env.pop(k, None)
                    else:
                        full_env[k] = v
        res = subprocess.run(argv or cmd, shell=is_shell, capture_output=True, env=full_env, cwd=cwd)
        return Result(
            res.stdout.decode("utf-8"),
            res.stderr.decode("utf-8"),
            res.returncode,
        )

    def _run_remote(
        self,
        cmd: str,
        log_level: int,
        *,
        env: Optional[Mapping[str, Optional[str]]] = None,
        quiet: bool = False,
        cwd: Optional[str] = None,
        sudo: bool = False,
    ) -> Result:
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} || exit 10\n{cmd}"

        if sudo:
            cmd2 = "sudo"
            if env:
                for k, v in env.items():
                    assert k == shlex.quote(k)
                    if v is not None:
                        cmd2 += f" {k}={shlex.quote(v)}"
            cmd = cmd2 + " sh -c " + shlex.quote(cmd)
        else:
            if env:
                # Assume we have a POSIX shell, and we can define variables via `export VAR=...`.
                cmd2 = ""
                for k, v in env.items():
                    assert k == shlex.quote(k)
                    if v is None:
                        cmd2 += f"unset  -v {k}\n"
                    else:
                        cmd2 += f"export {k}={shlex.quote(v)}\n"
                cmd = cmd2 + cmd

        while True:
            try:
                _, stdout, stderr = self._host.exec_command(cmd)
                break
            except Exception as e:
                logger.log(log_level, e)
                cmd_str = ""
                if not quiet:
                    cmd_str = cmd
                logger.log(log_level, f"Connection lost while running command {cmd_str}, reconnecting...")
                self.ssh_connect_looped(self._logins)

        return self.ssh_run_poll_result(stdout, stderr)

    def run_or_die(
        self,
        cmd: str | Iterable[str],
        *,
        env: Optional[Mapping[str, Optional[str]]] = None,
        cwd: Optional[str] = None,
        sudo: Optional[bool] = None,
    ) -> Result:
        ret = self.run(cmd, env=env, cwd=cwd, sudo=sudo)
        if ret.returncode:
            logger.error(f"{self._cmd_to_script(cmd)} failed: {ret.err}")
            sys.exit(-1)
        else:
            logger.debug(ret.out.strip())
        return ret

    def close(self) -> None:
        assert self._host is not None
        self._host.close()

    def wait_ping(self) -> None:
        while not self.ping():
            pass

    def ping(self) -> bool:
        lh = Host("localhost")
        ping_cmd = f"timeout 1 ping -4 -c 1 {self._hostname}"
        r = lh.run(ping_cmd)
        return r.returncode == 0

    def os_release(self) -> dict[str, str]:
        d = {}
        for e in self.read_file("/etc/os-release").split("\n"):
            split_e = e.split("=", maxsplit=1)
            if len(split_e) != 2:
                continue
            k, v = split_e
            v = v.strip("\"'")
            d[k] = v
        return d

    def running_fcos(self) -> bool:
        d = self.os_release()
        return (d["NAME"], d["VARIANT"]) == ('Fedora Linux', 'CoreOS')

    def vm_is_running(self, name: str) -> bool:
        def state_running(out: str) -> bool:
            return re.search("State:.*running", out) is not None

        ret = self.run(f"virsh dominfo {name}", logging.DEBUG)
        return not ret.returncode and state_running(ret.out)

    def write(
        self,
        fn: str,
        contents: str | bytes,
        *,
        sudo: Optional[bool] = None,
    ) -> None:
        if sudo is None:
            sudo = self.sudo_needed
        if isinstance(contents, str):
            b_contents = contents.encode('utf-8')
        else:
            b_contents = contents
        if not sudo and self.is_localhost():
            with open(fn, "wb") as f:
                f.write(b_contents)
        else:
            with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                tmp_filename = tmp_file.name
                tmp_file.write(b_contents)
            self.copy_to(tmp_filename, fn, sudo=sudo)
            os.remove(tmp_filename)

    def read_file(self, file_name: str) -> str:
        if self.is_localhost():
            with open(file_name, newline='') as f:
                return f.read()
        else:
            ret = self.run(f"cat {file_name}")
            if ret.returncode == 0:
                return ret.out
            raise Exception(f"Error reading {file_name}")

    def listdir(self, path: Optional[str] = None) -> list[str]:
        if self.is_localhost():
            return os.listdir(path)
        path = path if path is not None else ""
        ret = self.run(f"ls {path}")
        if ret.returncode == 0:
            return ret.out.strip().split("\n")
        raise Exception(f"Error listing dir {path}")

    def hostname(self) -> str:
        return self._hostname

    def home_dir(self, *path_components: str) -> str:
        ret = self.run("bash -c 'echo -n ~'")
        path = ret.out
        if not ret.success() or not path or path[0] != "/":
            raise RuntimeError("Failure getting home directory")
        if path_components:
            path = os.path.join(path, *path_components)
        return path

    def exists(self, path: str) -> bool:
        return self.run(f"stat {path}", logging.DEBUG).returncode == 0


class HostWithCX(Host):
    def cx_firmware_upgrade(self) -> Result:
        logger.info("Upgrading CX firmware")
        return self.run_in_container("/cx_fwup")

    def run_in_container(self, cmd: str, interactive: bool = False) -> Result:
        name = "cx"
        setup = f"sudo podman run --pull always --replace --pid host --network host --user 0 --name {name} -dit --privileged -v /dev:/dev quay.io/bnemeth/bf"
        r = self.run(setup, logging.DEBUG)
        if r.returncode != 0:
            return r
        it = "-it" if interactive else ""
        return self.run(f"sudo podman exec {it} {name} {cmd}")


class HostWithBF2(Host):
    def connect_to_bf(self, bf_addr: str) -> None:
        self.ssh_connect("core")
        prov_host = self._host
        rsa_login = self._rsa_login()
        if rsa_login is None:
            logger.error("Missing login with key")
            sys.exit(-1)
        pkey = rsa_login._pkey

        logger.info(f"Connecting to BF through host {self._hostname}")

        jumpbox_private_addr = '172.31.100.1'  # TODO

        transport = prov_host.get_transport()
        if transport is None:
            return
        src_addr = (jumpbox_private_addr, 22)
        dest_addr = (bf_addr, 22)
        chan = transport.open_channel("direct-tcpip", dest_addr, src_addr)

        self._bf_host = paramiko.SSHClient()
        self._bf_host.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._bf_host.connect(bf_addr, username='core', pkey=pkey, sock=chan)

    def run_on_bf(self, cmd: str, log_level: int = logging.DEBUG) -> Result:
        return self.ssh_run(self._bf_host, cmd, log_prefix=f"{self._hostname}:BF", log_level=log_level)

    def run_in_container(self, cmd: str, interactive: bool = False) -> Result:
        name = "bf"
        setup = f"sudo podman run --pull always --replace --pid host --network host --user 0 --name {name} -dit --privileged -v /dev:/dev quay.io/bnemeth/bf"
        r = self.run(setup, logging.DEBUG)
        if r.returncode != 0:
            return r
        it = "-it" if interactive else ""
        return self.run(f"sudo podman exec {it} {name} {cmd}")

    def bf_pxeboot(self, nfs_iso: str, nfs_key: str) -> Result:
        cmd = "sudo killall python3"
        self.run(cmd)
        logger.info("starting pxe server and booting bf")
        cmd = f"/pxeboot {nfs_iso} -w {nfs_key}"
        return self.run_in_container(cmd, True)

    def bf_firmware_upgrade(self) -> Result:
        logger.info("Upgrading BF firmware")
        # We need to temporarily pin the BF-2 firmware due to an issue with the latest release: https://issues.redhat.com/browse/OCPBUGS-29882
        # Without this, the sriov-network-operator will fail to put the card into NIC mode
        return self.run_in_container("/fwup -v 24.39.2048")

    def bf_firmware_defaults(self) -> Result:
        logger.info("Setting firmware config to defaults")
        return self.run_in_container("/fwdefaults")

    def bf_set_mode(self, mode: str) -> Result:
        return self.run_in_container(f"/set_mode {mode}")

    def bf_get_mode(self) -> Result:
        return self.run_in_container("/getmode")

    def bf_firmware_version(self) -> Result:
        return self.run_in_container("fwversion")

    def bf_load_bfb(self) -> Result:
        logger.info("Loading BFB image")
        return self.run_in_container("/bfb")


host_instances: dict[tuple[type[Host], str], Host] = {}


def sync_time(src: Host, dst: Host) -> Result:
    date = src.run("date").out.strip()
    return dst.run(f"sudo date -s \"{date}\"")


def LocalHost() -> Host:
    return Host("localhost")


def RemoteHost(ip: str) -> Host:
    return Host(ip)
