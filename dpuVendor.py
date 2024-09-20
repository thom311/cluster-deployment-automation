import jinja2
import re
import host
from k8sClient import K8sClient
from logger import logger
from abc import ABC
from imageRegistry import ImageRegistry
import os


class VendorPlugin(ABC):
    def build_push_start(self, h: host.Host, client: K8sClient, imgReg: ImageRegistry) -> None:
        raise NotImplementedError("Must implement build_and_start() for VSP")


class IpuPlugin(VendorPlugin):
    def __init__(self, name_suffix: str) -> None:
        self._repo = "https://github.com/intel/ipu-opi-plugins.git"
        self._vsp_ds_manifest = "./manifests/dpu/dpu_vsp_ds.yaml.j2"
        self.name_suffix = name_suffix

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def vsp_ds_manifest(self) -> str:
        return self._vsp_ds_manifest

    def import_from_url(self, url: str) -> None:
        lh = host.LocalHost()
        result = lh.run(f"podman load -q -i {url}")
        tag = result.out.strip().split("\n")[-1].split(":")[-1]
        lh.run_or_die(f"podman tag {tag} intel-ipuplugin:latest")

    def push(self, img_reg: ImageRegistry) -> None:
        lh = host.LocalHost()
        lh.run(f"podman push intel-ipuplugin:latest {self.vsp_image_name(img_reg)}")

    def vsp_image_name(self, img_reg: ImageRegistry) -> str:
        return f"{img_reg.url()}/intel_vsp:dev"

    def build_push_start(self, h: host.Host, client: K8sClient, imgReg: ImageRegistry) -> None:
        return self.start(self.build_push(h, imgReg), client)

    def build_push(self, h: host.Host, imgReg: ImageRegistry) -> str:
        logger.info("Building ipu-opi-plugin")
        h.run("rm -rf /root/ipu-opi-plugins")
        h.run_or_die(f"git clone {self.repo} /root/ipu-opi-plugins")
        fn = "/root/ipu-opi-plugins/ipu-plugin/images/Dockerfile"
        golang_img = extractContainerImage(h.read_file(fn))
        h.run_or_die(f"podman pull docker.io/library/{golang_img}")
        if h.is_localhost():
            env = os.environ.copy()
            env["IMGTOOL"] = "podman"
            ret = h.run("make -C /root/ipu-opi-plugins/ipu-plugin image", env=env)
        else:
            ret = h.run("IMGTOOL=podman make -C /root/ipu-opi-plugins/ipu-plugin image")
        if not ret.success():
            logger.error_and_exit("Failed to build vsp images")
        vsp_image = self.vsp_image_name(imgReg)
        h.run_or_die(f"podman tag intel-ipuplugin:latest {vsp_image}")
        h.run_or_die(f"podman push {vsp_image}")
        return vsp_image

    def start(self, vsp_image: str, client: K8sClient) -> None:
        self.render_dpu_vsp_ds_helper(vsp_image, "/tmp/vsp-ds.yaml")
        client.oc("delete -f /tmp/vsp-ds.yaml")
        client.oc_run_or_die("create -f /tmp/vsp-ds.yaml")

    def render_dpu_vsp_ds_helper(self, ipu_plugin_image: str, outfilename: str) -> None:
        with open(self.vsp_ds_manifest) as f:
            j2_template = jinja2.Template(f.read())
            rendered = j2_template.render(ipu_plugin_image=ipu_plugin_image)
            logger.info(rendered)
        lh = host.LocalHost()
        lh.write(outfilename, rendered)


class MarvellDpuPlugin(VendorPlugin):
    pass


def init_vendor_plugin(h: host.Host, *, dpu_kind: str) -> VendorPlugin:
    logger.info(f"Creating vendor plugin for dpu_kind {repr(dpu_kind)}")
    if dpu_kind == "marvell-dpu":
        return MarvellDpuPlugin()
    if dpu_kind == "intel-ipu":
        return IpuPlugin(h.run("uname -m").out)
    assert False


def extractContainerImage(dockerfile: str) -> str:
    match = re.search(r'FROM\s+([^\s]+)(?:\s+as\s+\w+)?', dockerfile, re.IGNORECASE)
    if match:
        first_image = match.group(1)
        return first_image
    else:
        logger.error_and_exit("Failed to find a Docker image in provided output")
