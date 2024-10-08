import ipaddress
from dataclasses import dataclass, field
import re
from typing import Tuple
from logger import logger
import common
from pathlib import Path
from clustersConfig import NodeConfig
import shutil
import sys
import host


DEFAULT_NETMASK = "255.255.255.0"
DHCPD_CONFIG_PATH = "/etc/dhcp/dhcpd.conf"
DHCPD_CONFIG_BACKUP_PATH = "/etc/dhcp/dhcpd.conf.cda-backup"
CDA_TAG = "Generated by CDA"


@dataclass
class DhcpdSubnetConfig:
    subnet: str
    netmask: str
    range_start: str
    range_end: str
    broadcast_address: str
    routers: str
    dns_servers: list[str]
    domain_names: list[str] = field(default_factory=lambda: ["redhat.com", "anl.eng.bos2.dc.redhat.com"])
    ntp_servers: str = "clock.redhat.com"

    def to_string(self) -> str:
        dns_servers_str = ", ".join(self.dns_servers)
        domain_names_str = " ".join(self.domain_names)
        return (
            f"# {CDA_TAG}\n"
            f"subnet {self.subnet} netmask {self.netmask} {{\n"
            f"    range {self.range_start} {self.range_end};\n"
            f"    option domain-name-servers {dns_servers_str};\n"
            f"    option routers {self.routers};\n"
            f"    option broadcast-address {self.broadcast_address};\n"
            f"    option domain-name \"{domain_names_str}\";\n"
            f"    option ntp-servers {self.ntp_servers};\n"
            f"}}\n"
        )


@dataclass
class DhcpdHostConfig:
    hostname: str
    hardware_ethernet: str
    fixed_address: str

    def to_string(self) -> str:
        return f"# {CDA_TAG}\n" f"host {self.hostname} {{\n" f"    hardware ethernet {self.hardware_ethernet};\n" f"    fixed-address {self.fixed_address};\n" f"    option host-name {self.hostname};\n" f"}}\n"


class DhcpConfig:
    _subnet_configs: list[DhcpdSubnetConfig] = []
    _host_configs: list[DhcpdHostConfig] = []

    def _get_subnets_str(self) -> list[str]:
        subnets = []
        for subnet in self._subnet_configs:
            subnets.append(_convert_to_cidr(subnet.subnet, subnet.netmask))
        return subnets

    def add_host(self, hostname: str, hardware_ethernet: str, fixed_address: str) -> None:
        # Generate host / subnet configs for the current Node
        new_hostconfig = DhcpdHostConfig(hostname=hostname, hardware_ethernet=hardware_ethernet, fixed_address=fixed_address)
        subnetconfig = subnet_config_from_host_config(new_hostconfig)

        # Check if an existing subnet contains the host or subnet configuration, add a new entry if not
        if any(common.ip_in_subnet(new_hostconfig.fixed_address, subnet) for subnet in self._get_subnets_str()):
            logger.debug(f"Subnet config for {new_hostconfig.fixed_address} already exists at {DHCPD_CONFIG_PATH}")
        else:
            logger.debug(f"Subnet config for {new_hostconfig.fixed_address} does not exist, adding this")
            self._subnet_configs.append(subnetconfig)

        # Delete entries with same IP address or MAC address (but different hostnames).
        for idx, hc in reversed(list(enumerate(self._host_configs))):
            if hc.hostname != new_hostconfig.hostname:
                if new_hostconfig.fixed_address == hc.fixed_address or new_hostconfig.hardware_ethernet == hc.hardware_ethernet:
                    logger.warning(f"Remove overlapping dhcp entry {hc} for new entry {new_hostconfig}")
                    del self._host_configs[idx]

        matching = [(idx, hc) for idx, hc in enumerate(self._host_configs) if hc.hostname == new_hostconfig.hostname]
        if matching:
            for idx, hc in reversed(matching[1:]):
                logger.warning(f"Remove overlapping dhcp entry {hc} for new entry {new_hostconfig}")
                del self._host_configs[idx]
            idx, hc = matching[0]
            if hc != new_hostconfig:
                logger.info(f"Update dhcp entry {hc} for new entry {new_hostconfig}")
                self._host_configs[idx] = new_hostconfig
        else:
            logger.info(f"Add dhcp entry {new_hostconfig}")
            self._host_configs.append(new_hostconfig)

    def to_string(self) -> str:
        config_str = ""
        for subnet in self._subnet_configs:
            config_str += subnet.to_string()
        for h in self._host_configs:
            config_str += h.to_string()
        return config_str

    def write_to_file(self, file_path: str = DHCPD_CONFIG_PATH) -> None:
        with open(file_path, 'w') as file:
            file.write(self.to_string())


def dhcp_config_from_file(file_path: str = DHCPD_CONFIG_PATH) -> DhcpConfig:
    config = DhcpConfig()

    with open(file_path, 'r') as file:
        lines = file.readlines()

    subnet_pattern = re.compile(r'subnet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(\d+\.\d+\.\d+\.\d+)\s+\{')
    range_pattern = re.compile(r'\s*range\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+);')
    dns_pattern = re.compile(r'\s*option\s+domain-name-servers\s+([^;]+);')
    routers_pattern = re.compile(r'\s*option\s+routers\s+(\d+\.\d+\.\d+\.\d+);')
    broadcast_pattern = re.compile(r'\s*option\s+broadcast-address\s+(\d+\.\d+\.\d+\.\d+);')
    host_pattern = re.compile(r'host\s+(\S+)\s+\{')
    hardware_pattern = re.compile(r'\s*hardware\s+ethernet\s+([\dA-Fa-f:]+);')
    fixed_address_pattern = re.compile(r'\s*fixed-address\s+(\d+\.\d+\.\d+\.\d+);')
    domain_name_pattern = re.compile(r'\s*option\s+domain-name\s+"([^"]+)";')
    ntp_servers_pattern = re.compile(r'\s*option\s+ntp-servers\s+([^;]+);')

    current_subnet = None
    current_host = None

    for line in lines:
        subnet_match = subnet_pattern.match(line)
        if subnet_match:
            if current_subnet is not None:
                logger.error_and_exit(f"Malformed subnet in dhcpd config at {file_path}")
            current_subnet = {
                'subnet': subnet_match.group(1),
                'netmask': subnet_match.group(2),
                'range_start': None,
                'range_end': None,
                'broadcast_address': None,
                'routers': None,
                'dns_servers': [],
                'domain_names': [],
                'ntp_servers': None,
            }
            continue

        if current_subnet is not None:
            range_match = range_pattern.match(line)
            if range_match:
                current_subnet['range_start'] = range_match.group(1)
                current_subnet['range_end'] = range_match.group(2)
                continue

            dns_match = dns_pattern.match(line)
            if dns_match:
                current_subnet['dns_servers'] = [ip.strip() for ip in dns_match.group(1).split(',')]
                continue

            routers_match = routers_pattern.match(line)
            if routers_match:
                current_subnet['routers'] = routers_match.group(1)
                continue

            broadcast_match = broadcast_pattern.match(line)
            if broadcast_match:
                current_subnet['broadcast_address'] = broadcast_match.group(1)
                continue

            domain_name_match = domain_name_pattern.match(line)
            if domain_name_match:
                current_subnet['domain_names'] = [name.strip() for name in domain_name_match.group(1).split(' ')]
                continue

            ntp_servers_match = ntp_servers_pattern.match(line)
            if ntp_servers_match:
                current_subnet['ntp_servers'] = ntp_servers_match.group(1)

            if '}' in line:
                config._subnet_configs.append(DhcpdSubnetConfig(**current_subnet))
                current_subnet = None
                continue

        host_match = host_pattern.match(line)
        if host_match:
            if current_host is not None:
                logger.error_and_exit(f"Malformed host in dhcpd config {file_path}")
            current_host = {'hostname': host_match.group(1), 'hardware_ethernet': None, 'fixed_address': None}
            continue

        if current_host is not None:
            hardware_match = hardware_pattern.match(line)
            if hardware_match:
                current_host['hardware_ethernet'] = hardware_match.group(1)
                continue

            fixed_address_match = fixed_address_pattern.match(line)
            if fixed_address_match:
                current_host['fixed_address'] = fixed_address_match.group(1)
                continue

            if '}' in line:
                config._host_configs.append(DhcpdHostConfig(hostname=str(current_host['hostname']), hardware_ethernet=str(current_host['hardware_ethernet']), fixed_address=str(current_host['fixed_address'])))
                current_host = None
    return config


def get_subnet_ip(ipv4_address: str, subnet_mask: str) -> str:
    network = ipaddress.ip_network(f"{ipv4_address}/{subnet_mask}", strict=False)
    return str(network.network_address)


def get_subnet_range(ipv4_address: str, subnet_mask: str) -> Tuple[str, str]:
    network = ipaddress.ip_network(f"{ipv4_address}/{subnet_mask}", strict=False)
    range_start = network.network_address + 1
    range_end = network.broadcast_address - 1
    return str(range_start), str(range_end)


def get_router_ip(ipv4_address: str, subnet_mask: str) -> str:
    network = ipaddress.ip_network(f"{ipv4_address}/{subnet_mask}", strict=False)
    router_ip = network.network_address + 1
    return str(router_ip)


def subnet_config_from_host_config(hc: DhcpdHostConfig) -> DhcpdSubnetConfig:
    netmask = DEFAULT_NETMASK
    subnet_ip = get_subnet_ip(hc.fixed_address, netmask)
    range_start, range_end = get_subnet_range(hc.fixed_address, netmask)
    broadcast_address = str(ipaddress.ip_network(f"{hc.fixed_address}/{netmask}", strict=False).broadcast_address)
    routers = get_router_ip(hc.fixed_address, netmask)
    dns_servers = ["10.2.70.215", "10.11.5.160"]
    return DhcpdSubnetConfig(subnet=subnet_ip, netmask=netmask, range_start=range_start, range_end=range_end, broadcast_address=broadcast_address, routers=routers, dns_servers=dns_servers)


def _convert_to_cidr(ipv4_address: str, subnet_mask: str) -> str:
    network = ipaddress.ip_network(f"{ipv4_address}/{subnet_mask}", strict=False)
    return str(network)


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
