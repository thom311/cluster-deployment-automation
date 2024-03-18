#!/bin/bash

set -e

if [ -n "$PYTHON_CMD" ] ; then
    :
elif [ "$(which python)" = "$(pwd)/ocp-venv/bin/python" ]; then
    PYTHON_CMD="python"
else
    PYTHON_CMD="python3.11"
    # Install Python 3.11 if not using the virtual environment interpreter
    sudo dnf install -y python3.11
fi

$PYTHON_CMD -m ensurepip --upgrade
$PYTHON_CMD -m pip install --upgrade PyYAML --ignore-installed

dnf install -y \
        bash-completion \
        cockpit-composer \
        composer-cli \
        coreos-installer \
        dhcp-server \
        dnsmasq \
        firewalld \
        git \
        golang-bin \
        libvirt \
        lorax \
        make \
        osbuild-composer \
        podman \
        qemu-img \
        qemu-kvm \
        rust \
        virt-install \
        virt-viewer \
        wget

systemctl enable osbuild-composer.socket cockpit.socket --now

if ! command -v -- oc; then
    export OPENSHIFT_CLIENT_TOOLS_URL=https://mirror.openshift.com/pub/openshift-v4/$(uname -m)/clients/ocp/stable/openshift-client-linux.tar.gz
    sudo mkdir -p /usr/local/bin
    curl $OPENSHIFT_CLIENT_TOOLS_URL | sudo tar -U -C /usr/local/bin -xzf -
fi

cat requirements.txt  | xargs -n1 $PYTHON_CMD -m pip install --upgrade

sudo activate-global-python-argcomplete
