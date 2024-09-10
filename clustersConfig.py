import os
import io
import logging
import threading
import re
import functools
import ipaddress
import typing
from typing import Optional
import xml.etree.ElementTree as et
import jinja2
from yaml import safe_load
import yaml
import host
from logger import logger
import secrets
import hashlib
import common
import collections.abc
import clusterInfo
from dataclasses import dataclass
from typing import Any
import ktoolbox.common as kcommon
import ktoolbox.netdev as knetdev

from ktoolbox.common import unwrap


def random_mac(*, rnd_seed: Optional[str] = None) -> str:
    if rnd_seed is None:
        hexstr = secrets.token_hex()
    else:
        hexstr = hashlib.sha256(f"cda-random-mac:{rnd_seed}".encode()).hexdigest()
    mac = "52:54:" + ":".join(re.findall("..", hexstr[:8]))
    valid, mac2 = _normalize_etheraddr(mac)
    if not valid or mac != mac2:
        raise RuntimeError(f"random_mac() did not generate a normalized MAC address but {repr(mac)}")
    return mac2


def _rnd_seed_join(*parts: str) -> str:
    return "".join(f"{len(s)}={{{s}}}" for s in parts)


def _normalize_etheraddr(ethaddr: str) -> tuple[bool, str]:
    ethaddr2 = knetdev.validate_ethaddr_or_none(ethaddr)
    if ethaddr2 is None:
        return False, ethaddr
    return True, ethaddr2


def _normalize_network_api_port(network_api_port: Optional[str]) -> Optional[str]:
    # in YAML, the auto port is represented with the string "auto" or "".
    # In HostConfig.network_api_port we map that to None.
    if network_api_port is None or network_api_port in ("auto", ""):
        return None
    return network_api_port


def is_openshift_like(cluster_kind: str) -> bool:
    return cluster_kind in ("openshift", "microshift")


@kcommon.strict_dataclass
@dataclass(frozen=True, kw_only=True)
class ExtraConfigArgs(kcommon.StructParseBaseNamed):
    name: str

    # OVN-K extra configs:
    # New ovn-k image to use.
    image: Optional[str]

    # Time to wait for new ovn-k to roll out.
    ovnk_rollout_timeout: Optional[str]

    mapping: Optional[tuple[collections.abc.Mapping[str, str], ...]]

    # With "sriov_network_operator", if true build the container images locally
    # and push them to the internal container registry of openshift.
    #
    # You will need authentication for fetching build containers.
    # Get the login token from [1]. Then `podman login registry.ci.openshift.org`
    # or create "$XDG_RUNTIME_DIR/containers/auth.json".
    # [1] https://oauth-openshift.apps.ci.l2s4.p1.openshiftapps.com/oauth/token/request
    #
    # If enabled, an existing "/root/sriov-network-operator" directory is not
    # wiped and you can prepare there the version you want to build and
    # install.
    sriov_network_operator_local: Optional[bool]

    # Custom config to the scheduler whether the masters are allowed to run workloads.
    schedulable: Optional[bool]
    # https://console.redhat.com/insights/connector/activation-keys
    organization_id: Optional[str]

    activation_key: Optional[str]

    dpu_operator_path: Optional[str]

    rebuild_dpu_operators_images: Optional[bool]

    dpu_net_interface: Optional[str]

    def serialize(self) -> dict[str, Any]:
        extra_1: dict[str, Any] = {}
        kcommon.dict_add_optional(extra_1, "image", self.image)
        kcommon.dict_add_optional(extra_1, "ovnk_rollout_timeout", self.ovnk_rollout_timeout)
        if self.mapping is not None:
            extra_1["mapping"] = [dict(d) for d in self.mapping]
        kcommon.dict_add_optional(extra_1, "sriov_network_operator_local", self.sriov_network_operator_local)
        kcommon.dict_add_optional(extra_1, "schedulable", self.schedulable)
        kcommon.dict_add_optional(extra_1, "organization_id", self.organization_id)
        kcommon.dict_add_optional(extra_1, "activation_key", self.activation_key)
        kcommon.dict_add_optional(extra_1, "dpu_operator_path", self.dpu_operator_path)
        kcommon.dict_add_optional(extra_1, "rebuild_dpu_operators_images", self.rebuild_dpu_operators_images)
        kcommon.dict_add_optional(extra_1, "dpu_net_interface", self.dpu_net_interface)
        return {
            **super().serialize(),
            **extra_1,
        }

    @staticmethod
    def parse(
        yamlidx: int,
        yamlpath: str,
        arg: Any,
    ) -> "ExtraConfigArgs":
        with kcommon.structparse_with_strdict(arg, yamlpath) as varg:

            name = kcommon.structparse_pop_str_name(*varg.for_name())

            def _invalid_with_name(key: str) -> ValueError:
                return ValueError(f"\"{yamlpath}.{key}\": parameter not valid for {repr(name)}")

            def _required_with_name(key: str) -> ValueError:
                return ValueError(f"\"{yamlpath}.{key}\": parameter is mandator for {repr(name)}")

            import extraConfigRunner

            valid_names = sorted(extraConfigRunner.EXTRA_CONFIGS.keys())
            if name not in valid_names:
                raise ValueError(f"\"{yamlpath}.name\": must be one of {repr(valid_names)}")

            image = kcommon.structparse_pop_str(
                *varg.for_key("image"),
                default=None,
            )
            is_valid = name in ("cno", "ovnk8s", "sriov")
            if image is None:
                if is_valid:
                    if name == "sriov":
                        # for "sriov", this parameter is optional.
                        pass
                    else:
                        raise _required_with_name("image")
            else:
                if not is_valid:
                    raise _invalid_with_name("image")

            ovnk_rollout_timeout = kcommon.structparse_pop_str(
                *varg.for_key("ovnk_rollout_timeout"),
                default=None,
            )
            is_valid = name == "ovnk8s"
            if ovnk_rollout_timeout is None:
                if is_valid:
                    ovnk_rollout_timeout = "20m"
            else:
                if not is_valid:
                    raise _invalid_with_name("ovnk_rollout_timeout")
                ovnk_rollout_timeout = ovnk_rollout_timeout.strip()
                if not re.search("^[0-9]+[smh]?$", ovnk_rollout_timeout):
                    raise ValueError(f"\"{yamlpath}.ovnk_rollout_timeout\": {repr(ovnk_rollout_timeout)} is not a valid timeout")

            def _construct_mapping(yamlidx2: int, yamlpath2: str, arg2: Any) -> tuple[dict[str, str], ...]:
                if isinstance(arg2, dict):
                    arg2 = [arg2]
                good = isinstance(arg2, list)
                good = good and all(isinstance(k, str) and isinstance(v, str) for d in arg2 for k, v in d.items())
                good = good and all(("worker" in d and "bf" in d) for d in arg2)
                if not good:
                    raise ValueError(f"\"{yamlpath2}\": expects a list of str:str dictionaries for the environment variables (mandatory keys \"worker\" and \"bf\")")
                return tuple(dict(d) for d in arg2)

            mapping = kcommon.structparse_pop_obj(
                *varg.for_key("mapping"),
                construct=_construct_mapping,
                default=None,
            )
            is_valid = name == "dpu_tenant"
            if mapping is None:
                if is_valid:
                    raise ValueError(f"\"{yamlpath}.mapping\": missing mandatory mapping parameter (list of strdicts)")
            else:
                if not is_valid:
                    raise _invalid_with_name("mapping")

            sriov_network_operator_local = kcommon.structparse_pop_bool(
                *varg.for_key("sriov_network_operator_local"),
                default=None,
            )
            is_valid = name == "sriov_network_operator"
            if sriov_network_operator_local is None:
                if is_valid:
                    sriov_network_operator_local = False
            else:
                if not is_valid:
                    raise _invalid_with_name("sriov_network_operator_local")

            schedulable = kcommon.structparse_pop_bool(
                *varg.for_key("schedulable"),
                default=None,
            )
            is_valid = name == "masters_schedulable"
            if schedulable is None:
                if is_valid:
                    schedulable = True
            else:
                if not is_valid:
                    raise _invalid_with_name("schedulable")

            organization_id = kcommon.structparse_pop_str(
                *varg.for_key("organization_id"),
                default=None,
            )
            is_valid = name == "rh_subscription"
            if organization_id is None:
                if is_valid:
                    raise _required_with_name("organization_id")
            else:
                if not is_valid:
                    raise _invalid_with_name("organization_id")

            activation_key = kcommon.structparse_pop_str(
                *varg.for_key("activation_key"),
                default=None,
            )
            is_valid = name == "rh_subscription"
            if activation_key is None:
                if is_valid:
                    raise _required_with_name("activation_key")
            else:
                if not is_valid:
                    raise _invalid_with_name("activation_key")

            dpu_operator_path = kcommon.structparse_pop_str(
                *varg.for_key("dpu_operator_path"),
                default=None,
            )
            is_valid = name in ("dpu_operator_dpu", "dpu_operator_host")
            if dpu_operator_path is None:
                if is_valid:
                    dpu_operator_path = "/root/dpu-operator"
            else:
                if not is_valid:
                    raise _invalid_with_name("dpu_operator_path")

            rebuild_dpu_operators_images = kcommon.structparse_pop_bool(
                *varg.for_key("rebuild_dpu_operators_images"),
                default=None,
            )
            is_valid = name in ("dpu_operator_dpu", "dpu_operator_host")
            if rebuild_dpu_operators_images is None:
                if is_valid:
                    rebuild_dpu_operators_images = True
            else:
                if not is_valid:
                    raise _invalid_with_name("rebuild_dpu_operators_images")

            dpu_net_interface = kcommon.structparse_pop_str(
                *varg.for_key("dpu_net_interface"),
                default=None,
            )
            is_valid = name == "dpu_operator_host"
            if dpu_net_interface is None:
                if is_valid:
                    dpu_net_interface = "ens2f0"
            else:
                if not is_valid:
                    raise _invalid_with_name("dpu_net_interface")
                dpu_net_interface2 = knetdev.validate_ifname_or_none(dpu_net_interface)
                if dpu_net_interface2 is None:
                    raise ValueError(f'"{yamlpath}.dpu_net_interface": {repr(dpu_net_interface)} is not a valid interface name')

        return ExtraConfigArgs(
            yamlidx=yamlidx,
            yamlpath=yamlpath,
            name=name,
            image=image,
            ovnk_rollout_timeout=ovnk_rollout_timeout,
            mapping=mapping,
            sriov_network_operator_local=sriov_network_operator_local,
            schedulable=schedulable,
            organization_id=organization_id,
            activation_key=activation_key,
            dpu_operator_path=dpu_operator_path,
            rebuild_dpu_operators_images=rebuild_dpu_operators_images,
            dpu_net_interface=dpu_net_interface,
        )

    def system_check(self) -> None:
        if self.name == "sriov_network_operator":
            if self.sriov_network_operator_local and not common.build_sriov_network_operator_check_permissions():
                raise ValueError(
                    f"\"{self.yamlpath}\": Building sriov_network_operator requires permissions to fetch. Get a token from https://oauth-openshift.apps.ci.l2s4.p1.openshiftapps.com/oauth/token/request and issue `podman login registry.ci.openshift.org`"
                )


@kcommon.strict_dataclass
@dataclass(frozen=True, kw_only=True)
class NodeConfig(kcommon.StructParseBaseNamed):
    kind: str
    node: str
    ip: Optional[str]
    mac_explicit: Optional[str]
    mac_random: str
    image_path: str
    bmc: Optional[str]
    bmc_user: Optional[str]
    bmc_password: Optional[str]
    os_variant: Optional[str]
    preallocated: Optional[bool]
    disk_size: Optional[int]
    ram: Optional[int]
    cpu: Optional[int]

    @property
    def mac(self) -> str:
        if self.mac_explicit is not None:
            return self.mac_explicit
        return self.mac_random

    def create_bmc(self) -> host.BMC:
        if self.bmc is None:
            raise ValueError(f"The node {self.name} has no BMC")
        return host.BMC.from_bmc(self.bmc, unwrap(self.bmc_user), unwrap(self.bmc_password))

    def create_rhost_bmc(self) -> host.Host:
        if self.bmc is None:
            raise ValueError(f"The node {self.name} has no BMC")
        rsh = host.RemoteHost(self.bmc)
        rsh.ssh_connect(unwrap(self.bmc_user), unwrap(self.bmc_password))
        return rsh

    def serialize(self) -> dict[str, Any]:
        extra_1: dict[str, Any] = {}
        kcommon.dict_add_optional(extra_1, "ip", self.ip)
        kcommon.dict_add_optional(extra_1, "mac", self.mac_explicit)
        extra_2: dict[str, Any] = {}
        kcommon.dict_add_optional(extra_2, "bmc", self.bmc)
        kcommon.dict_add_optional(extra_2, "bmc_user", self.bmc_user)
        kcommon.dict_add_optional(extra_2, "bmc_password", self.bmc_password)
        kcommon.dict_add_optional(extra_2, "os_variant", self.os_variant)
        kcommon.dict_add_optional(extra_2, "preallocated", self.preallocated)
        kcommon.dict_add_optional(extra_2, "disk_size", self.disk_size)
        kcommon.dict_add_optional(extra_2, "ram", self.ram)
        kcommon.dict_add_optional(extra_2, "cpu", self.cpu)
        return {
            **super().serialize(),
            "kind": self.kind,
            "node": self.node,
            **extra_1,
            "mac_random": self.mac_random,
            "image_path": self.image_path,
            **extra_2,
        }

    @staticmethod
    def parse(
        yamlidx: int,
        yamlpath: str,
        arg: Any,
        *,
        cluster_name: str,
        rnd_seed: Optional[str] = None,
    ) -> "NodeConfig":
        with kcommon.structparse_with_strdict(arg, yamlpath) as varg:

            name = kcommon.structparse_pop_str_name(*varg.for_name())

            kind_type = kcommon.structparse_pop_str(
                *varg.for_key("type"),
                default=None,
            )
            kind = kcommon.structparse_pop_str(
                *varg.for_key("kind"),
                default=None,
            )
            kind_property = "kind"
            if kind_type is not None:
                if kind is not None:
                    if kind != kind_type:
                        raise ValueError(f"\"{yamlpath}.kind\": the value {repr(kind)} differs from the deprected {yamlpath}.type ({repr(kind_type)})")
                else:
                    kind = kind_type
                    kind_property = "type"
            else:
                if kind is None:
                    raise ValueError(f"\"{yamlpath}.kind\": mandatory value missing")
            valid_kinds = ("physical", "vm", "bf", "marvell-dpu")
            if kind not in valid_kinds:
                raise ValueError(f"\"{yamlpath}.{kind_property}\": invalid value {repr(kind)} (must be one of {repr(valid_kinds)})")

            node = kcommon.structparse_pop_str(
                *varg.for_key("node"),
            )

            mac_random = kcommon.structparse_pop_str(
                *varg.for_key("mac_random"),
                default=None,
            )
            if mac_random is None:
                s = _rnd_seed_join(
                    "random_mac",
                    yamlpath,
                    cluster_name,
                    name,
                    rnd_seed if rnd_seed is not None else secrets.token_hex(),
                )
                mac_random = random_mac(rnd_seed=s)
            else:
                valid, mac_random = _normalize_etheraddr(mac_random)
                if not valid:
                    raise ValueError(f"\"{yamlpath}.mac_random\": invalid MAC address {repr(mac_random)}")

            mac_explicit = kcommon.structparse_pop_str(
                *varg.for_key("mac"),
                default=None,
            )
            if mac_explicit is not None:
                valid, mac_explicit = _normalize_etheraddr(mac_explicit)
                if not valid:
                    raise ValueError(f"\"{yamlpath}.mac\": invalid MAC address {repr(mac_explicit)}")

            bmc = kcommon.structparse_pop_str(
                *varg.for_key("bmc"),
                default=None,
            )

            bmc_user = kcommon.structparse_pop_str(
                *varg.for_key("bmc_user"),
                default="root" if bmc is not None else None,
            )

            bmc_password = kcommon.structparse_pop_str(
                *varg.for_key("bmc_password"),
                default="calvin" if bmc_user is not None else None,
            )

            if bmc is None:
                if kind in ("phyisical", "bf"):
                    raise ValueError(f"\"{yamlpath}.bmc\": BMC is mandatory for kind {kind}")

                # We allow the YAML to contain "bmc_user" and "bmc_password". However,
                # they are unused. Normalize them to NULL.
                bmc_user = None
                bmc_password = None

            ip = kcommon.structparse_pop_str(
                *varg.for_key("ip"),
                default=None,
            )
            if ip is not None:
                try:
                    ip, _ = knetdev.validate_ipaddr(ip)
                except Exception:
                    raise ValueError(f"\"{yamlpath}.ip\": invalid IP address {repr(ip)}") from None

            image_path = kcommon.structparse_pop_str(
                *varg.for_key("image_path"),
                default=f"/home/{cluster_name}_guests_images/{name}.qcow2",
            )

            os_variant: Optional[str] = kcommon.structparse_pop_str(
                *varg.for_key("os_variant"),
                default="rhel8.6",
                # flag for "virsh --os-variant" option with VMs
            )

            preallocated: Optional[bool] = kcommon.structparse_pop_bool(
                *varg.for_key("preallocated"),
                default=True,
                description="flag for \"qemu-img -o preallocated\" with VMs",
            )

            disk_size: Optional[int] = kcommon.structparse_pop_int(
                *varg.for_key("disk_size"),
                default=48,
                description="the disk size in GB",
                check=lambda x: x > 0,
            )

            ram: Optional[int] = kcommon.structparse_pop_int(
                *varg.for_key("ram"),
                default=32768,
                description="the RAM memory in MB",
                check=lambda x: x > 0,
            )

            cpu: Optional[int] = kcommon.structparse_pop_int(
                *varg.for_key("cpu"),
                default=8,
                description="the number of CPU cores for VM",
                check=lambda x: x > 0,
            )

        if kind != "vm":
            # Those value are normalized away unless for VM.
            os_variant = None
            preallocated = None
            disk_size = None
            ram = None
            cpu = None

        return NodeConfig(
            yamlidx=yamlidx,
            yamlpath=yamlpath,
            name=name,
            node=node,
            kind=kind,
            mac_explicit=mac_explicit,
            mac_random=mac_random,
            image_path=image_path,
            bmc=bmc,
            bmc_user=bmc_user,
            bmc_password=bmc_password,
            ip=ip,
            os_variant=os_variant,
            preallocated=preallocated,
            disk_size=disk_size,
            ram=ram,
            cpu=cpu,
        )


@kcommon.strict_dataclass
@dataclass(frozen=True, kw_only=True)
class HostConfig(kcommon.StructParseBaseNamed):
    # In YAML, if the value is set explicitly to "auto" or "", it
    # gets mapped to None here. It means to autodetect it.
    # See also _normalize_network_api_port().
    network_api_port: Optional[str]

    # If True, it means that the host entry did not have a network_api_port
    # key. Instead, network_api_port value is inherited from the default.
    network_api_port_is_default: bool

    username: str
    password: Optional[str]
    pre_installed: bool
    is_generated: bool

    def serialize(self) -> dict[str, Any]:
        extra_1 = {}
        extra_2 = {}
        if not self.network_api_port_is_default:
            extra_1["network_api_port"] = self.network_api_port or "auto"
        if self.is_generated:
            extra_2["is_generated"] = True
        return {
            **super().serialize(),
            **extra_1,
            "username": self.username,
            "password": self.password,
            "pre_installed": self.pre_installed,
            **extra_2,
        }

    @staticmethod
    def parse(
        yamlidx: int,
        yamlpath: str,
        arg: Any,
        *,
        default_network_api_port: Optional[str] = None,
        is_generated: bool = False,
    ) -> "HostConfig":
        with kcommon.structparse_with_strdict(arg, yamlpath) as varg:

            name = kcommon.structparse_pop_str_name(*varg.for_name())

            network_api_port_is_default = False
            network_api_port = kcommon.structparse_pop_str(
                *varg.for_key("network_api_port"),
                default=None,
            )
            if network_api_port is None:
                network_api_port_is_default = True
                network_api_port = default_network_api_port
            network_api_port = _normalize_network_api_port(network_api_port)
            if network_api_port is not None:
                network_api_port2 = knetdev.validate_ifname_or_none(network_api_port)
                if network_api_port2 is None:
                    if network_api_port_is_default:
                        raise ValueError(f'"{yamlpath}.network_api_port": default {repr(network_api_port)} is not a valid interface name')
                    raise ValueError(f'"{yamlpath}.network_api_port": {repr(network_api_port)} is not a valid interface name')
                network_api_port = network_api_port2

            username = kcommon.structparse_pop_str(
                *varg.for_key("username"),
                default="core",
            )

            password = kcommon.structparse_pop_str(
                *varg.for_key("password"),
                default=None,
            )

            pre_installed = kcommon.structparse_pop_bool(
                *varg.for_key("pre_installed"),
                default=True,
            )

            is_generated = kcommon.structparse_pop_bool(
                *varg.for_key("is_generated"),
                default=is_generated,
            )

        return HostConfig(
            yamlidx=yamlidx,
            yamlpath=yamlpath,
            name=name,
            network_api_port=network_api_port,
            network_api_port_is_default=network_api_port_is_default,
            username=username,
            password=password,
            pre_installed=pre_installed,
            is_generated=is_generated,
        )


@kcommon.strict_dataclass
@dataclass(frozen=True, kw_only=True)
class BridgeConfig:
    ip: str
    mask: str
    dynamic_ip_range: Optional[tuple[str, str]] = None


@kcommon.strict_dataclass
@dataclass(frozen=True, kw_only=True)
class ClusterConfig(kcommon.StructParseBaseNamed):
    kind: str
    kubeconfig: Optional[str]
    version: Optional[str]
    proxy: Optional[str]
    noproxy: Optional[str]
    api_vip: Optional[str]
    ingress_vip: Optional[str]
    network_api_port: Optional[str]
    external_port: Optional[str]
    ip_mask: Optional[str]
    ip_range: Optional[tuple[str, str]]
    real_ip_range: Optional[tuple[str, str]]
    local_bridge_config: Optional[BridgeConfig]
    remote_bridge_config: Optional[BridgeConfig]
    install_iso: Optional[str]
    ntp_source: Optional[str]
    base_dns_domain: Optional[str]
    masters: collections.abc.Mapping[str, NodeConfig]
    workers: collections.abc.Mapping[str, NodeConfig]
    hosts: collections.abc.Mapping[str, HostConfig]
    preconfig: tuple[ExtraConfigArgs, ...]
    postconfig: tuple[ExtraConfigArgs, ...]

    def serialize(self) -> dict[str, Any]:
        extra_1: dict[str, Any] = {}
        kcommon.dict_add_optional(extra_1, "kubeconfig", self.kubeconfig)
        kcommon.dict_add_optional(extra_1, "version", self.version)
        kcommon.dict_add_optional(extra_1, "proxy", self.proxy)
        kcommon.dict_add_optional(extra_1, "noproxy", self.noproxy)
        kcommon.dict_add_optional(extra_1, "api_vip", self.api_vip)
        kcommon.dict_add_optional(extra_1, "ingress_vip", self.ingress_vip)
        kcommon.dict_add_optional(extra_1, "network_api_port", self.network_api_port or "auto")
        kcommon.dict_add_optional(extra_1, "external_port", self.external_port or "auto")
        kcommon.dict_add_optional(extra_1, "ip_mask", self.ip_mask)
        kcommon.dict_add_optional(extra_1, "ip_range", "-".join(self.ip_range) if self.ip_range else None)
        kcommon.dict_add_optional(extra_1, "install_iso", self.install_iso)
        kcommon.dict_add_optional(extra_1, "ntp_source", self.ntp_source)
        kcommon.dict_add_optional(extra_1, "base_dns_domain", self.base_dns_domain)
        return {
            **super().serialize(),
            "kind": self.kind,
            **extra_1,
            "masters": [n.serialize() for n in self.masters.values()],
            "workers": [n.serialize() for n in self.workers.values()],
            "hosts": [h.serialize() for h in self.hosts.values() if not h.is_generated],
            "preconfig": [h.serialize() for h in self.preconfig],
            "postconfig": [h.serialize() for h in self.postconfig],
        }

    @staticmethod
    def parse(
        yamlidx: int,
        yamlpath: str,
        arg: Any,
        *,
        basedir: Optional[str] = None,
        rnd_seed: Optional[str] = None,
        get_last_ip: Optional[typing.Callable[[], Optional[str]]] = None,
    ) -> "ClusterConfig":
        with kcommon.structparse_with_strdict(arg, yamlpath) as varg:

            if basedir is None:
                basedir = os.getcwd()

            name = kcommon.structparse_pop_str_name(*varg.for_name())

            kind = kcommon.structparse_pop_str(
                *varg.for_key("kind"),
                default="openshift",
            )
            valid_kinds = ("openshift", "microshift", "iso")
            if kind not in valid_kinds:
                raise ValueError(f"\"{yamlpath}.kind\": invalid value {repr(kind)} (must be one of {repr(valid_kinds)})")

            ntp_source = kcommon.structparse_pop_str(
                *varg.for_key("ntp_source"),
                default="clock.redhat.com" if is_openshift_like(kind) else None,
            )

            base_dns_domain = kcommon.structparse_pop_str(
                *varg.for_key("base_dns_domain"),
                default="redhat.com" if is_openshift_like(kind) else None,
            )

            version = kcommon.structparse_pop_str(
                *varg.for_key("version"),
                default="4.14.0-nightly" if is_openshift_like(kind) else None,
            )

            network_api_port: Optional[str] = kcommon.structparse_pop_str(
                *varg.for_key("network_api_port"),
                default="auto",
            )
            network_api_port = _normalize_network_api_port(network_api_port)

            external_port: Optional[str] = kcommon.structparse_pop_str(
                *varg.for_key("external_port"),
                default="auto",
            )
            external_port = _normalize_network_api_port(external_port)

            api_vip = kcommon.structparse_pop_str(
                *varg.for_key("api_vip"),
                default=None,
            )
            if api_vip is not None:
                try:
                    api_vip, _ = knetdev.validate_ipaddr(api_vip, addr_family="4")
                except Exception:
                    raise ValueError(f"\"{yamlpath}.api_vip\": {repr(api_vip)} is not a valid IPv4 address")

            ingress_vip = kcommon.structparse_pop_str(
                *varg.for_key("ingress_vip"),
                default=None,
            )
            if ingress_vip is not None:
                try:
                    ingress_vip, _ = knetdev.validate_ipaddr(ingress_vip, addr_family="4")
                except Exception:
                    raise ValueError(f"\"{yamlpath}.ingress_vip\": {repr(ingress_vip)} is not a valid IPv4 address")

            kubeconfig = kcommon.structparse_pop_str(
                *varg.for_key("kubeconfig"),
                default=os.path.join(basedir, f"kubeconfig.{name}") if is_openshift_like(kind) else None,
            )

            proxy = kcommon.structparse_pop_str(
                *varg.for_key("proxy"),
                default=None,
            )

            noproxy = kcommon.structparse_pop_str(
                *varg.for_key("noproxy"),
                default=None,
            )

            install_iso = kcommon.structparse_pop_str(
                *varg.for_key("install_iso"),
                default=None,
            )

            ip_mask = kcommon.structparse_pop_str(
                *varg.for_key("ip_mask"),
                default="255.255.0.0" if is_openshift_like(kind) else None,
            )
            if ip_mask is not None:
                try:
                    ip_mask, _ = knetdev.validate_ipaddr(ip_mask, addr_family='4')
                except Exception:
                    raise ValueError(f"\"{yamlpath}.ip_mask\": invalid subnet mask {repr(ip_mask)} is not an IPv4 address")
                # TODO: normalize/validate that this is a subnet mask.

            ip_range: Optional[tuple[str, str]] = None
            ip_range_str = kcommon.structparse_pop_str(
                *varg.for_key("ip_range"),
                default="192.168.122.1-192.168.122.254" if is_openshift_like(kind) else None,
            )
            if ip_range_str is not None:
                try:
                    a, b = ip_range_str.split("-")
                    a, _ = knetdev.validate_ipaddr(a, addr_family='4')
                    b, _ = knetdev.validate_ipaddr(b, addr_family='4')
                except Exception:
                    raise ValueError(f"\"{yamlpath}.ip_range\": invalid IPv4 address range {repr(ip_mask)} not in the form \"<startIP>-<endIP>\"")
                ip_range = a, b
                # TODO: validate that the range is non-empty and within the ip_mask subnet.

            def _construct_node(yamlidx2: int, yamlpath2: str, arg: Any) -> NodeConfig:
                return NodeConfig.parse(
                    yamlidx2,
                    yamlpath2,
                    arg,
                    cluster_name=name,
                    rnd_seed=rnd_seed,
                )

            masters = kcommon.structparse_pop_objlist_to_dict(
                *varg.for_key("masters"),
                construct=_construct_node,
            )

            workers = kcommon.structparse_pop_objlist_to_dict(
                *varg.for_key("workers"),
                construct=_construct_node,
            )

            hosts = ClusterConfig._parse_hosts(
                varg,
                masters=masters,
                workers=workers,
                default_network_api_port=network_api_port,
            )

            preconfig = kcommon.structparse_pop_objlist(
                *varg.for_key("preconfig"),
                construct=ExtraConfigArgs.parse,
            )

            postconfig = kcommon.structparse_pop_objlist(
                *varg.for_key("postconfig"),
                construct=ExtraConfigArgs.parse,
            )

        if is_openshift_like(kind):
            assert kubeconfig
            assert version
            assert ip_range
            assert ip_mask
            if not ClusterConfig._is_sno(kind, len(masters)):
                if api_vip is None:
                    raise ValueError(f"\"{yamlpath}.api_vip\": missing parameter for cluster kind {kind}")
                if ingress_vip is None:
                    raise ValueError(f"\"{yamlpath}.ingress_vip\": missing parameter for cluster kind {kind}")
            else:
                # Silently unset values.
                api_vip = None
                ingress_vip = None
        else:
            if version is not None:
                raise ValueError(f"\"{yamlpath}.version\": value not allowed with kind {kind}")
            if kubeconfig is not None:
                raise ValueError(f"\"{yamlpath}.kubeconfig\": value not allowed with kind {kind}")
            if api_vip is not None:
                raise ValueError(f"\"{yamlpath}.api_vip\": value not allowed with kind {kind}")
            if ingress_vip is not None:
                raise ValueError(f"\"{yamlpath}.ingress_vip\": value not allowed with kind {kind}")
            if proxy is not None:
                raise ValueError(f"\"{yamlpath}.proxy\": value not allowed with kind {kind}")
            if noproxy is not None:
                raise ValueError(f"\"{yamlpath}.noproxy\": value not allowed with kind {kind}")
            if ip_mask is not None:
                raise ValueError(f"\"{yamlpath}.ip_mask\": value not allowed with kind {kind}")
            if ip_range is not None:
                raise ValueError(f"\"{yamlpath}.ip_range\": value not allowed with kind {kind}")
            if ntp_source is not None:
                raise ValueError(f"\"{yamlpath}.ntp_source\": value not allowed with kind {kind}")
            if base_dns_domain is not None:
                raise ValueError(f"\"{yamlpath}.base_dns_domain\": value not allowed with kind {kind}")

        if kind == "iso":
            if install_iso is None:
                raise ValueError(f"\"{yamlpath}.install_iso\": mandatory parameter missing for kind {kind}")
            if workers:
                raise ValueError(f"\"{yamlpath}.workers\": no workers allowed with kind {repr(kind)}")
            if len(masters) != 1:
                if not masters:
                    raise ValueError(f"\"{yamlpath}.masters\": requires one master with kind {repr(kind)}")
                raise ValueError(f"\"{yamlpath}.masters\": only one master allowed with kind {repr(kind)}")

            master = next(iter(masters.values()))
            if master.kind not in ("physical", "marvell-dpu"):
                raise ValueError(f"\"{yamlpath}.masters[0]\": for a cluster kind {repr(kind)} the master has an unexpected kind {master.kind}")
        else:
            if install_iso is not None:
                raise ValueError(f"\"{yamlpath}.install_iso\": only valid with kind \"iso\"")

        real_ip_range, local_bridge_config, remote_bridge_config = ClusterConfig._parse_ip_range(
            yamlpath=yamlpath,
            kind=kind,
            ip_range=ip_range,
            ip_mask=ip_mask,
            all_nodes=list(masters.values()) + list(workers.values()),
            get_last_ip=get_last_ip,
        )

        return ClusterConfig(
            yamlidx=yamlidx,
            yamlpath=yamlpath,
            name=name,
            kind=kind,
            kubeconfig=kubeconfig,
            version=version,
            ntp_source=ntp_source,
            base_dns_domain=base_dns_domain,
            proxy=proxy,
            noproxy=noproxy,
            install_iso=install_iso,
            api_vip=api_vip,
            ingress_vip=ingress_vip,
            ip_mask=ip_mask,
            ip_range=ip_range,
            real_ip_range=real_ip_range,
            local_bridge_config=local_bridge_config,
            remote_bridge_config=remote_bridge_config,
            network_api_port=network_api_port,
            external_port=external_port,
            masters=masters,
            workers=workers,
            hosts=hosts,
            preconfig=preconfig,
            postconfig=postconfig,
        )

    @staticmethod
    def _parse_ip_range(
        *,
        yamlpath: str,
        kind: str,
        ip_mask: Optional[str],
        ip_range: Optional[tuple[str, str]],
        all_nodes: list[NodeConfig],
        get_last_ip: Optional[typing.Callable[[], Optional[str]]] = None,
    ) -> tuple[Optional[tuple[str, str]], Optional[BridgeConfig], Optional[BridgeConfig]]:
        if kind != "openshift":
            return None, None, None

        # Reserve IPs for AI, masters and workers.
        ip_mask = unwrap(ip_mask)
        ip_range = unwrap(ip_range)

        n_nodes = len(all_nodes) + 1

        # Get the last IP used in the running cluster.
        if get_last_ip is not None:
            last_ip = get_last_ip()
        else:
            last_ip = None

        # Update the last IP based on the config.
        for node in all_nodes:
            if node.ip and last_ip and ipaddress.IPv4Address(node.ip) > ipaddress.IPv4Address(last_ip):
                last_ip = node.ip

        if last_ip and ipaddress.IPv4Address(last_ip) > ipaddress.IPv4Address(ip_range[0]) + n_nodes:
            real_ip_range = ip_range[0], str(ipaddress.ip_address(last_ip) + 1)
        else:
            real_ip_range = common.ip_range(ip_range[0], n_nodes)

        if common.ip_range_size(ip_range) < common.ip_range_size(real_ip_range):
            raise ValueError(f"\"{yamlpath}.ip_range\": the supplied IP range {ip_range} is too small for the number of nodes")

        dynamic_ip_range = common.ip_range(real_ip_range[1], common.ip_range_size(ip_range) - common.ip_range_size(real_ip_range))
        local_bridge_config = BridgeConfig(ip=real_ip_range[0], mask=ip_mask, dynamic_ip_range=dynamic_ip_range)
        remote_bridge_config = BridgeConfig(ip=ip_range[1], mask=ip_mask)

        return real_ip_range, local_bridge_config, remote_bridge_config

    @staticmethod
    def _parse_hosts(
        varg: kcommon.StructParseVarg,
        *,
        masters: dict[str, NodeConfig],
        workers: dict[str, NodeConfig],
        default_network_api_port: Optional[str],
    ) -> dict[str, HostConfig]:
        hosts = kcommon.structparse_pop_objlist_to_dict(
            *varg.for_key("hosts"),
            construct=lambda yamlidx2, yamlpath2, arg: HostConfig.parse(
                yamlidx2,
                yamlpath2,
                arg,
                default_network_api_port=default_network_api_port,
            ),
        )

        node_names2: set[str] = set()
        node_names2.update(n.node for n in masters.values())
        node_names2.update(n.node for n in workers.values())
        node_names2.discard("localhost")
        node_names3 = ["localhost"] + sorted(node_names2)

        for node_name in node_names3:
            if node_name not in hosts:
                # artificially create an entry for this host.
                yamlidx2 = len(hosts)
                hosts[node_name] = HostConfig.parse(
                    yamlidx2,
                    f"{varg.yamlpath}.hosts[{yamlidx2}]",
                    {"name": node_name},
                    default_network_api_port=default_network_api_port,
                    is_generated=True,
                )
        return hosts

    @staticmethod
    def _is_sno(kind: str, n_masters: int) -> bool:
        return n_masters == 1 and kind == "openshift"

    @property
    def is_sno(self) -> bool:
        return self._is_sno(self.kind, len(self.masters))

    def system_check(self) -> None:
        for c in self.preconfig:
            c.system_check()
        for c in self.postconfig:
            c.system_check()


@kcommon.strict_dataclass
@dataclass(frozen=True, kw_only=True)
class MainConfig(kcommon.StructParseBase):
    clusters: tuple[ClusterConfig, ...]

    def serialize(self) -> dict[str, Any]:
        return {
            "clusters": [n.serialize() for n in self.clusters],
        }

    @staticmethod
    def parse(
        yamlidx: int,
        yamlpath: str,
        arg: Any,
        *,
        basedir: Optional[str] = None,
        rnd_seed: Optional[str] = None,
        get_last_ip: Optional[typing.Callable[[], Optional[str]]] = None,
    ) -> "MainConfig":
        with kcommon.structparse_with_strdict(arg, yamlpath) as varg:

            if basedir is None:
                basedir = os.getcwd()

            clusters = kcommon.structparse_pop_objlist(
                *varg.for_key("clusters"),
                construct=lambda yamlidx2, yamlpath2, arg2: ClusterConfig.parse(
                    yamlidx2,
                    yamlpath2,
                    arg2,
                    basedir=basedir,
                    rnd_seed=rnd_seed,
                    get_last_ip=get_last_ip,
                ),
                allow_empty=False,
            )

        return MainConfig(
            yamlidx=yamlidx,
            yamlpath=yamlpath,
            clusters=clusters,
        )

    @staticmethod
    def _apply_jinja(
        contents: str,
        *,
        cluster_name: Optional[str],
        cluster_info_loader: clusterInfo.ClusterInfoLoader,
    ) -> str:
        @functools.cache
        def _ci() -> clusterInfo.ClusterInfo:
            lh = host.LocalHost()
            current_host = lh.run("hostname -f").out.strip()
            return cluster_info_loader.get_or_die(current_host)

        def worker_number(a: int) -> str:
            name = _ci().workers[a]
            lab_match = re.search(r"lab(\d+)", name)
            if lab_match:
                return lab_match.group(1)
            else:
                return re.sub("[^0-9]", "", name)

        def worker_name(a: int) -> str:
            return _ci().workers[a]

        def bmc(a: int) -> str:
            return _ci().bmcs[a]

        def api_network() -> str:
            return _ci().network_api_port

        def iso_server() -> str:
            return _ci().iso_server

        def activation_key() -> str:
            return _ci().activation_key

        def organization_id() -> str:
            return _ci().organization_id

        def imc_hostname(a: int) -> str:
            return _ci().bmc_imc_hostnames[a]

        def ipu_mac_address(a: int) -> str:
            return _ci().ipu_mac_addresses[a]

        format_string = contents

        template = jinja2.Template(format_string)
        template.globals['worker_number'] = worker_number
        template.globals['worker_name'] = worker_name
        template.globals['api_network'] = api_network
        template.globals['iso_server'] = iso_server
        template.globals['bmc'] = bmc
        template.globals['activation_key'] = activation_key
        template.globals['organization_id'] = organization_id
        template.globals['IMC_hostname'] = imc_hostname
        template.globals['IPU_mac_address'] = ipu_mac_address

        kwargs: dict[str, str] = {}
        kcommon.dict_add_optional(kwargs, "cluster_name", cluster_name)

        t: str = template.render(**kwargs)
        return t

    @staticmethod
    def load(
        filename: str,
        *,
        with_jinja: bool = True,
        cluster_info_loader: Optional[clusterInfo.ClusterInfoLoader] = None,
        basedir: Optional[str] = None,
        rnd_seed: Optional[str] = None,
        get_last_ip: Optional[typing.Callable[[], Optional[str]]] = None,
    ) -> 'MainConfig':
        if not os.path.exists(filename):
            raise ValueError(f"Missing YAML configuration at {repr(filename)}")

        try:
            with open(filename, 'r') as f:
                contents = f.read()
        except Exception as e:
            raise ValueError(f"Error reading YAML configuration at {repr(filename)}: {e}")

        try:
            yamldata = safe_load(io.StringIO(contents))
        except Exception as e:
            raise ValueError(f"Error reading YAML at {repr(filename)}{' before Jinja2 templating' if with_jinja else ''}: {e}")

        if with_jinja:
            try:
                cluster_name = yamldata["clusters"][0]["name"]
            except Exception:
                cluster_name = None

            if cluster_info_loader is None:
                cluster_info_loader = clusterInfo.ClusterInfoLoader()

            contents = MainConfig._apply_jinja(
                contents,
                cluster_name=cluster_name,
                cluster_info_loader=cluster_info_loader,
            )

            try:
                yamldata = safe_load(io.StringIO(contents))
            except Exception as e:
                raise ValueError(f"Error reading YAML at {repr(filename)} after Jinja2 templating: {e}")

        try:
            cc = MainConfig.parse(
                0,
                "",
                yamldata,
                basedir=basedir,
                rnd_seed=rnd_seed,
                get_last_ip=get_last_ip,
            )
        except Exception as e:
            raise ValueError(f"Error loading YAML at {repr(filename)}: {e}")

        return cc

    def system_check(self) -> None:
        for cluster_config in self.clusters:
            cluster_config.system_check()


class ClustersConfig:
    worker_range: common.RangeList
    main_config: MainConfig
    cluster_index: int
    secrets_path: str
    _external_port: Optional[str]
    _external_port_validated: bool
    _lock: threading.Lock

    def __init__(
        self,
        yaml_path: str,
        *,
        cluster_index: Optional[int] = None,
        secrets_path: str = "",
        worker_range: common.RangeList = common.RangeList.UNLIMITED,
        basedir: Optional[str] = None,
        rnd_seed: Optional[str] = None,
        get_last_ip: Optional[typing.Callable[[], Optional[str]]] = None,
        with_system_check: bool = True,
    ):
        self._lock = threading.Lock()
        self.secrets_path = secrets_path
        self.worker_range = worker_range

        if get_last_ip is None:
            get_last_ip = self.get_last_ip

        self.main_config = MainConfig.load(
            yaml_path,
            basedir=basedir,
            rnd_seed=rnd_seed,
            get_last_ip=get_last_ip,
        )

        if cluster_index is None:
            cluster_index = 0
            if len(self.main_config.clusters) != 1:
                raise ValueError(f"Error loading {repr(yaml_path)}: \"{self.main_config.yamlpath}.clusters\": only one entry in the clusters list is supported but {len(self.main_config.clusters)} given")
        else:
            if cluster_index < 0 or cluster_index >= len(self.main_config.clusters):
                raise ValueError(f"Error loading {repr(yaml_path)}: \"{self.main_config.yamlpath}.clusters\": requested cluster index {cluster_index} but only {len(self.main_config.clusters)} are configured")

        self.cluster_index = cluster_index

        self._external_port = self.cluster_config.external_port
        self._external_port_validated = False

        if with_system_check:
            try:
                self.system_check()
            except ValueError as e:
                raise ValueError(f"Failure checking {repr(yaml_path)}: {e}")

    @property
    def cluster_config(self) -> ClusterConfig:
        return self.main_config.clusters[self.cluster_index]

    @property
    def name(self) -> str:
        return self.cluster_config.name

    @property
    def kind(self) -> str:
        return self.cluster_config.kind

    @property
    def version(self) -> str:
        if self.cluster_config.version is None:
            raise ValueError(f"\"{self.cluster_config.yamlpath}.version\": parameter missing for cluster kind {repr(self.kind)}")
        return self.cluster_config.version

    @property
    def network_api_port(self) -> str:
        if self.cluster_config.network_api_port is None:
            # TODO: the upper layers should accept "auto" to be represented as
            # None.
            return "auto"
        return self.cluster_config.network_api_port

    @property
    def kubeconfig(self) -> str:
        if self.cluster_config.kubeconfig is None:
            raise ValueError(f"\"{self.cluster_config.yamlpath}.kubeconfig\": parameter missing for cluster kind {repr(self.kind)}")
        return self.cluster_config.kubeconfig

    @property
    def real_ip_range(self) -> tuple[str, str]:
        if self.cluster_config.ip_range is None:
            raise ValueError(f"IP range parameter missing for cluster kind {repr(self.kind)}")
        return self.cluster_config.ip_range

    @property
    def masters(self) -> list[NodeConfig]:
        return list(self.cluster_config.masters.values())

    @property
    def workers(self) -> list[NodeConfig]:
        return self.worker_range.filter(self.cluster_config.workers.values())

    @property
    def hosts(self) -> collections.abc.Mapping[str, HostConfig]:
        return self.cluster_config.hosts

    @staticmethod
    def get_last_ip() -> Optional[str]:
        hostconn = host.LocalHost()
        last_ip = "0.0.0.0"
        xml_str = hostconn.run("virsh net-dumpxml default").out
        if xml_str.strip():
            tree = et.fromstring(xml_str)
            ip_tree = next((it for it in tree.iter("ip")), et.Element(''))
            dhcp = next((it for it in ip_tree.iter("dhcp")), et.Element(''))
            for e in dhcp:
                if ipaddress.IPv4Address(e.get('ip', "0.0.0.0")) > ipaddress.IPv4Address(last_ip):
                    last_ip = e.get('ip', "0.0.0.0")
            return last_ip
        return None

    def get_external_port(self) -> str:
        with self._lock:
            if self._external_port_validated:
                return unwrap(self._external_port)
            rsh = host.LocalHost()
            if self._external_port is None:
                candidate = common.route_to_port(rsh, "default")
                if candidate is None:
                    raise RuntimeError(f"\"{self.cluster_config}.external_port\": Unable to detect external_port on localhost")
            else:
                candidate = self._external_port
            if not common.ip_links(rsh, ifname=candidate):
                if self._external_port is None:
                    raise RuntimeError(f"\"{self.cluster_config}.external_port\": Unable to use detected external port {repr(candidate)} on localhost")
                else:
                    raise RuntimeError(f"\"{self.cluster_config}.external_port\": Unable to use external_port {repr(candidate)} on localhost")
            self._external_port = candidate
            self._external_port_validated = True

        return candidate

    def validate_node_ips(self) -> None:
        ip_range = unwrap(self.cluster_config.real_ip_range)

        def validate_node_ip(n: NodeConfig) -> bool:
            if n.ip is not None and not common.ip_range_contains(ip_range, n.ip):
                logger.error(f"Node ({n.name} IP ({n.ip}) not in cluster subnet range: {ip_range[0]} - {ip_range[1]}.")
                return False
            return True

        if not all(validate_node_ip(n) for n in self.masters + list(self.cluster_config.workers.values())):
            logger.error(f"Not all master/worker IPs are in the reserved cluster IP range ({ip_range}).  Other hosts in the network might be offered those IPs via DHCP.")

    def all_nodes(self) -> list[NodeConfig]:
        return self.masters + self.workers

    def all_vms(self) -> list[NodeConfig]:
        return [x for x in self.all_nodes() if x.kind == "vm"]

    def worker_vms(self) -> list[NodeConfig]:
        return [x for x in self.workers if x.kind == "vm"]

    def master_vms(self) -> list[NodeConfig]:
        return [x for x in self.masters if x.kind == "vm"]

    def local_vms(self) -> list[NodeConfig]:
        return [x for x in self.all_vms() if x.node == "localhost"]

    def local_worker_vms(self) -> list[NodeConfig]:
        return [x for x in self.worker_vms() if x.node == "localhost"]

    def system_check(self) -> None:
        self.main_config.system_check()

    def log_config(self, *, log_level: int = logging.DEBUG) -> None:
        logger.log(log_level, f"config: {self.main_config.serialize_json()}")
        if len(self.main_config.clusters) != 1:
            logger.log(log_level, f"config: cluster-index {self.cluster_index}")
        if self.cluster_config.real_ip_range is not None:
            logger.log(log_level, f"config: ip-range={self.cluster_config.real_ip_range}, local_bridge_config={self.cluster_config.local_bridge_config}, remote_bridge_config={self.cluster_config.remote_bridge_config}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description='Cluster Deployment Automation Config')
    parser.add_argument("-S", '--no-system-check', dest='with_system_check', action='store_false', help="Disable system checks.")
    parser.add_argument('--system-check', dest='with_system_check', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--cluster-index', default=0, type=int, help="A configuration can contain multiple clusters. This is the index which one to load")
    parser.add_argument('-p', "--pretty", action='store_true', help='Print the normalized YAML')
    parser.add_argument('filenames', nargs='+', help="List of filenames")
    parser.set_defaults(with_system_check=True)

    args = parser.parse_args()

    for idx, f in enumerate(args.filenames):
        if not args.pretty:
            logger.info(f"Check file {repr(f)}")
        cc = ClustersConfig(
            f,
            with_system_check=args.with_system_check,
            cluster_index=args.cluster_index,
        )
        if not args.pretty:
            cc.log_config(log_level=logging.INFO)
            continue

        if idx > 0:
            print("---")
        print(f"# file: {f}")
        print(f"# secrets_path: {cc.secrets_path}")
        print(f"# cluster_index: {cc.cluster_index}")
        print(f"# ip_range: {cc.cluster_config.real_ip_range}")
        print(f"# local_bridge_config: {cc.cluster_config.local_bridge_config}")
        print(f"# remote_bridge_config: {cc.cluster_config.remote_bridge_config}")
        print(
            yaml.dump(
                cc.main_config.serialize(),
                default_flow_style=False,
                sort_keys=False,
            )
        )


if __name__ == "__main__":
    main()
