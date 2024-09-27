import dataclasses
import json
import os
import gspread
import typing
import threading
from oauth2client.service_account import ServiceAccountCredentials
from logger import logger
import ktoolbox.common as kcommon


SHEET = "ANL lab HW enablement clusters and connections"
URL = "https://docs.google.com/spreadsheets/d/1lXvcodJ8dmc_hcp0hzbPDU8t6-hCnA"


@dataclasses.dataclass(kw_only=True)
class ClusterInfo:
    name: str
    provision_host: str = ""
    network_api_port: str = ""
    iso_server: str = ""
    organization_id: str = ""
    activation_key: str = ""
    bmc_imc_hostnames: list[str] = dataclasses.field(default_factory=list)
    ipu_mac_addresses: list[str] = dataclasses.field(default_factory=list)
    workers: list[str] = dataclasses.field(default_factory=list)
    bmcs: list[str] = dataclasses.field(default_factory=list)


def read_sheet() -> list[dict[str, str]]:
    logger.info(f"Reading cluster information from sheet {repr(SHEET)} ( {URL} )")
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    cred_paths = [
        os.path.join(os.getcwd(), "credentials.json"),
        os.path.join(os.environ["HOME"], "credentials.json"),
        os.path.join(os.environ["HOME"], ".config/gspread/credentials.json"),
    ]
    cred_path = None
    for e in cred_paths:
        if os.path.exists(e):
            cred_path = e
            break
    try:
        if cred_path is None:
            raise ValueError(f"Credentials not found in {cred_paths}")
        credentials = ServiceAccountCredentials.from_json_keyfile_name(cred_path, scopes)
        file = gspread.auth.authorize(credentials)
        sheet = file.open(SHEET)
    except Exception as e:
        raise ValueError(f"Failure accessing google sheet: {e}. See https://docs.gspread.org/en/latest/oauth2.html#for-bots-using-service-account and share {repr(SHEET)} sheet ( {URL} )")
    sheet1 = sheet.sheet1
    return [{k: str(v) for k, v in record.items()} for record in sheet1.get_all_records()]


def load_all_cluster_info() -> dict[str, ClusterInfo]:
    cluster = None
    ret = []
    for row in read_sheet():
        if row["Name"].startswith("Cluster"):
            if cluster is not None:
                ret.append(cluster)
            cluster = ClusterInfo(name=row["Name"])
        if cluster is None:
            continue
        if "BF2" in row["Name"]:
            continue
        if row["Card type"] == "IPU-Cluster":
            cluster.bmc_imc_hostnames.append(row["BMC/IMC hostname"])
            cluster.ipu_mac_addresses.append(row["MAC"])
            cluster.iso_server = row["ISO server"]
            cluster.activation_key = row["Activation Key"]
            cluster.organization_id = row["Organization ID"]
        if row["Provision host"] == "yes":
            cluster.provision_host = row["Name"]
            cluster.network_api_port = row["Ports"]
        elif row["Provision host"] == "no":
            cluster.workers.append(row["Name"])
            bmc_host = row["BMC/IMC hostname"][8:] if "https://" in row["BMC/IMC hostname"] else row["BMC/IMC hostname"]
            cluster.bmcs.append(bmc_host)
    if cluster is not None:
        ret.append(cluster)
    return {x.provision_host: x for x in ret}


def validate_cluster_info(cluster_info: ClusterInfo) -> None:
    if cluster_info.provision_host == "":
        raise ValueError(f"Provision host missing for cluster {cluster_info.name}")
    if cluster_info.network_api_port == "":
        raise ValueError(f"Network api port missing for cluster {cluster_info.name}")
    for e in cluster_info.workers:
        if e == "":
            raise ValueError(f"Unnamed worker found for cluster {cluster_info.name}")
    for e in cluster_info.bmcs:
        if e == "":
            raise ValueError(f"Unfilled IMPI address found for cluster {cluster_info.name}")


def load_cluster_info(provision_host: str) -> ClusterInfo:
    all_cluster_info = load_all_cluster_info()
    for ci in all_cluster_info.values():
        validate_cluster_info(ci)
    return all_cluster_info[provision_host]


class ClusterInfoLoader:
    def __init__(self, all_cluster_info: typing.Optional[dict[str, ClusterInfo]] = None) -> None:
        self._lock = threading.Lock()
        self._all_cluster_info = all_cluster_info

    def get_all(self) -> dict[str, ClusterInfo]:
        with self._lock:
            if self._all_cluster_info is None:
                self._all_cluster_info = load_all_cluster_info()
                vdict = {k: kcommon.dataclass_to_dict(ci) for k, ci in self._all_cluster_info.items()}
                logger.debug(f"all-cluster-info: {json.dumps(vdict)}")
            return self._all_cluster_info

    @typing.overload
    def get(
        self,
        hostname: str,
        *,
        try_hostname: bool = True,
        required: typing.Literal[True],
    ) -> ClusterInfo:
        pass

    @typing.overload
    def get(
        self,
        hostname: str,
        *,
        try_hostname: bool = True,
        required: bool = False,
    ) -> typing.Optional[ClusterInfo]:
        pass

    def get(
        self,
        hostname: str,
        *,
        try_hostname: bool = True,
        required: bool = False,
    ) -> typing.Optional[ClusterInfo]:
        all_cluster_info = self.get_all()

        ci = all_cluster_info.get(hostname)
        if ci is None and try_hostname:
            # Also try without the FQDN.
            if "." in hostname:
                ci = all_cluster_info.get(hostname.split(".")[0])

        if ci is not None:
            return ci

        if required:
            raise ValueError(f"Cannot find cluster info for host {repr(hostname)}")

        return None
