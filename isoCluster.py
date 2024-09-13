import sys
import os
import shlex
from pathlib import Path
import shutil
import time
import typing
import urllib.parse
from logger import logger
from clustersConfig import NodeConfig
from dhcpConfig import dhcp_config_from_file, DHCPD_CONFIG_PATH, DHCPD_CONFIG_BACKUP_PATH, CDA_TAG, get_subnet_range
import host
import common
from ktoolbox.common import unwrap


"""
ExtraConfigIPU is used to provision and IPUs specified via Redfish through the IMC.
This works by making some assumptions about the current state of the IPU:
- The IMC is on MeV 1.2 / Mev 1.3
- BMD_CONF has been set to allow for iso Boot
- ISCSI attempt has been added to allow for booting into the installed media
- The specified ISO contains full installation kickstart / kargs required for automated boot
- The specified ISO handles installing dependencies like dhclient and microshift
- The specified ISO architecture is aarch64
- There is an additional connection between the provisioning host and the acc on an isolated subnet to serve dhcp / provide acc with www
"""


def render_dhcpd_conf(mac: str, ip: str, name: str) -> None:
    logger.debug("Rendering dhcpd conf")
    file_path = DHCPD_CONFIG_PATH

    # If a config already exists, check if it was generated by CDA.
    file = Path(file_path)
    if file.exists():
        logger.debug("Existing dhcpd configuration detected")
        with file.open('r') as f:
            line = f.readline()
        # If not created by CDA, save as a backup to maintain idempotency
        if CDA_TAG not in line:
            logger.info(f"Backing up existing dhcpd conf to {DHCPD_CONFIG_BACKUP_PATH}")
            shutil.move(file_path, DHCPD_CONFIG_BACKUP_PATH)
    file.touch()

    dhcp_config = dhcp_config_from_file(DHCPD_CONFIG_PATH)

    dhcp_config.add_host(hostname=name, hardware_ethernet=mac, fixed_address=ip)

    dhcp_config.write_to_file()


def configure_dhcpd(node: NodeConfig) -> None:
    logger.info("Configuring dhcpd entry")

    render_dhcpd_conf(node.mac, str(node.ip), node.name)
    lh = host.LocalHost()
    ret = lh.run("systemctl restart dhcpd")
    if ret.returncode != 0:
        logger.error(f"Failed to restart dhcpd with err: {ret.err}")
        sys.exit(-1)


def configure_iso_network_port(api_port: str, node_ip: str) -> None:
    start, _ = get_subnet_range(node_ip, "255.255.255.0")
    lh = host.LocalHost()
    logger.info(f"Flushing cluster port {api_port} and setting ip to {start}")
    lh.run_or_die(f"ip addr flush dev {api_port}")
    lh.run_or_die(f"ip addr add {start}/24 dev {api_port}")
    lh.run(f"ip link set {api_port} up")


def enable_acc_connectivity(node: NodeConfig) -> None:
    logger.info(f"Establishing connectivity to {node.name}")
    if node.dpu_kind == "marvell-dpu":
        pass
    elif node.dpu_kind == "intel-ipu":
        ipu_imc = node.create_rhost_bmc()

        # """
        # We need to ensure the ACC physical port connectivity is enabled during reboot to ensure dhcp gets an ip.
        # Trigger an acc reboot and try to run python /usr/bin/scripts/cfg_acc_apf_x2.py. This will fail until the
        # ACC_LAN_APF_VPORTs are ready. Once this succeeds, we can try to connect to the ACC
        # """
        logger.info("Rebooting IMC to trigger ACC reboot")
        ipu_imc.run("systemctl reboot")
        time.sleep(30)
        ipu_imc = node.create_rhost_bmc()
        logger.info(f"Attempting to enable ACC connectivity from IMC {node.bmc} on reboot")
        retries = 30
        for _ in range(retries):
            ret = ipu_imc.run("/usr/bin/scripts/cfg_acc_apf_x2.py")
            if ret.returncode == 0:
                logger.info("Enabled ACC physical port connectivity")
                break
            logger.debug(f"ACC SPF script failed with returncode {ret.returncode}")
            logger.debug(f"out: {ret.out}\n err: {ret.err}")
            time.sleep(15)
        else:
            logger.error_and_exit("Failed to enable ACC connectivity")
    else:
        assert False

    ipu_acc = host.RemoteHost(str(node.ip))
    ipu_acc.ping()
    ipu_acc.ssh_connect("root", "redhat")
    if node.dpu_kind == "intel-ipu":
        ipu_acc.run("nmcli con mod enp0s1f0 ipv4.route-metric 0")
        ipu_acc.run("ip route delete default via 192.168.0.1")  # remove imc default route to avoid conflict
    logger.info(f"{node.name} connectivity established")
    if node.dpu_kind == "intel-ipu":
        ensure_ipu_netdevs_available(node)


# TODO: Remove this workaround once rebooting the IMC no longer causes the netdevs on the IPU host to be removed
def host_from_imc(imc: str) -> str:
    ipu_host = imc.split('-intel-ipu-imc')[0]
    return ipu_host


# TODO: Remove this workaround once rebooting the IMC no longer causes the netdevs on the IPU host to be removed
def ensure_ipu_netdevs_available(node: NodeConfig) -> None:
    # This is a hack, iso_cluster deployments in general should not need to know about the x86 host they are connected to.
    # However, since we need to cold boot the corresponding host, for the time being, infer this from the IMC address
    # rather than requiring the user to provide this information.
    ipu_host_name = host_from_imc(unwrap(node.bmc))
    ipu_host_bmc = host.BMC.from_bmc(ipu_host_name + "-drac.anl.eng.bos2.dc.redhat.com", "root", "calvin")
    ipu_host = host.Host(host_from_imc(unwrap(node.bmc)), ipu_host_bmc)
    ipu_host.ssh_connect("core")
    ret = ipu_host.run("test -d /sys/class/net/ens2f0")
    retries = 3
    while ret.returncode != 0:
        logger.error(f"{ipu_host.hostname()} does not have a network device ens2f0 cold booting node to try to recover")
        ipu_host.cold_boot()
        logger.info("Cold boot triggered, waiting for host to reboot")
        time.sleep(60)
        ipu_host.ssh_connect("core")
        retries = retries - 1
        if retries == 0:
            logger.error_and_exit(f"Failed to bring up IPU net device on {ipu_host.hostname()}")
        ret = ipu_host.run("test -d /sys/class/net/ens2f0")


def is_http_url(url: str) -> bool:
    try:
        result = urllib.parse.urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def _redfish_boot_ipu(
    *,
    node: NodeConfig,
    iso: str,
    get_external_port: typing.Callable[[], str],
) -> None:
    def helper(node: NodeConfig) -> str:
        logger.info(f"Booting {node.bmc} with {iso_address}")
        bmc = node.create_bmc()
        bmc.boot_iso_redfish(iso_path=iso_address, retries=5, retry_delay=15)

        # TODO: Remove once https://issues.redhat.com/browse/RHEL-32696 is solved
        logger.info("Waiting for 25m (workaround)")
        time.sleep(25 * 60)
        return f"Finished booting imc {node.bmc}"

    # Ensure dhcpd is stopped before booting the IMC to avoid unintentionally setting the ACC hostname during the installation
    # https://issues.redhat.com/browse/RHEL-32696
    lh = host.LocalHost()
    lh.run("systemctl stop dhcpd")

    # If an http address is provided, we will boot from here.
    # Otherwise we will assume a local file has been provided and host it.
    if is_http_url(iso):
        logger.debug(f"Booting IPU from iso served at {iso}")
        iso_address = iso

        logger.info(helper(node))
    else:
        logger.debug(f"Booting IPU from local iso {iso}")
        if not os.path.exists(iso):
            logger.error(f"ISO file {iso} does not exist, exiting")
            sys.exit(-1)
        serve_path = os.path.dirname(iso)
        iso_name = os.path.basename(iso)
        lh = host.LocalHost()
        lh_ip = common.port_to_ip(lh, get_external_port())

        with common.HttpServerManager(serve_path, 8000) as http_server:
            iso_address = f"http://{lh_ip}:{str(http_server.port)}/{iso_name}"
            logger.info(helper(node))


def _pxeboot_marvell_dpu(name: str, node: str, mac: str, ip: str, iso: str) -> None:
    rsh = host.RemoteHost(node)
    rsh.ssh_connect("core")

    ip_addr = f"{ip}/24"
    ip_gateway, _ = get_subnet_range(ip, "255.255.255.0")

    # An empty entry means to use the host's "id_ed25519.pub". We want that.
    ssh_keys = [""]
    for pub_file, pub_key_content, priv_key_file in common.iterate_ssh_keys():
        ssh_keys.append(pub_key_content)

    ssh_key_options = [f"--ssh-key={shlex.quote(s)}" for s in ssh_keys]

    image = os.environ.get("CDA_MARVELL_TOOLS_IMAGE", "quay.io/sdaniele/marvell-tools:latest")

    r = rsh.run(
        "sudo "
        "podman "
        "run "
        "--pull always "
        "--rm "
        "--replace "
        "--privileged "
        "--pid host "
        "--network host "
        "--user 0 "
        "--name marvell-tools "
        "-i "
        "-v /:/host "
        "-v /dev:/dev "
        f"{shlex.quote(image)} "
        "./pxeboot.py "
        f"--dpu-name={shlex.quote(name)} "
        "--host-mode=coreos "
        f"--nm-secondary-cloned-mac-address={shlex.quote(mac)} "
        f"--nm-secondary-ip-address={shlex.quote(ip_addr)} "
        f"--nm-secondary-ip-gateway={shlex.quote(ip_gateway)} "
        "--yum-repos=rhel-nightly "
        f"{' '.join(ssh_key_options)} "
        f"{shlex.quote(iso)} "
        "2>&1"
    )
    if not r.success():
        raise RuntimeError(f"Failure to to pxeboot: {r}")


def IPUIsoBoot(
    *,
    node: NodeConfig,
    iso: str,
    network_api_port: str,
    get_external_port: typing.Callable[[], str],
) -> None:
    if node.dpu_kind == "marvell-dpu":
        _pxeboot_marvell_dpu(node.name, node.node, node.mac, unwrap(node.ip), iso)
    elif node.dpu_kind == "intel-ipu":
        _redfish_boot_ipu(
            node=node,
            iso=iso,
            get_external_port=get_external_port,
        )
    else:
        assert False
    configure_iso_network_port(network_api_port, unwrap(node.ip))
    configure_dhcpd(node)
    enable_acc_connectivity(node)


def main() -> None:
    pass


if __name__ == "__main__":
    main()
