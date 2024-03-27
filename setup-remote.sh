#!/bin/bash

set -e

die() {
    printf "%s\n" "$*" >&2
    exit 1
}

FORCE="${FORCE}"
REMOTE="$1"

if ! [[ "$REMOTE" == *@* ]] ; then
    REMOTE="root@$REMOTE"
fi

cd "$(dirname "$0")" || die "Cannot change to base directory for $0"

_rsh() {
    ssh "$REMOTE" "$@"
}

_rsh_cda() {
    _rsh "cd ~/cluster-deployment-automation && $*"
}

_rsh true || die "Failure to login $REMOTE"

ADDR4="$(host -4 "${REMOTE##*@}" | sed -n 's/.* has address //p; q')"
DEV=
CONUUID=
if [ -n "$ADDR4" ] ; then
    DEV="$(_rsh ip -4 addr | tac | sed "1,/$ADDR4/d" | sed -n 's/^[0-9]\+: \([^:@]\+\).*/\1/p' | head -n1)"
fi
if [ -n "$DEV" ] ; then
    CONUUID="$(_rsh nmcli -g CON-UUID,DEVICE device | sed -n "s/:$DEV//p" | head -n1)"
fi
if [ -n "$CONUUID" ] ; then
    _rsh nmcli connection modify uuid "$CONUUID" ipv6.method disabled
    _rsh nmcli device reapply "$DEV"
fi

_rsh dnf install -y rsync vim git

_rsh '
    set -ex
    grep -q "alias l=" /etc/bashrc || echo "alias l=\"ls -la\"" >> /etc/bashrc
'

_rsh '
    set -ex
    if [ "'"$(printf '%q' "$FORCE")"'" = 1 ] ; then
        rm -rf ./cluster-deployment-automation/
    fi
    if [ ! -d ./cluster-deployment-automation/ ] ; then
        git clone https://github.com/bn222/cluster-deployment-automation.git
        cd ./cluster-deployment-automation/
        git remote add --no-tags thom311 https://github.com/thom311/cluster-deployment-automation.git
        git remote set-url thom311 --push git@github.com:thom311/cluster-deployment-automation.git
    else
        cd ./cluster-deployment-automation/
    fi
    git fetch --all
'

if [ -f .git/pull_secret.json ] ; then
    scp .git/pull_secret.json "$REMOTE:cluster-deployment-automation/pull_secret.json"
fi

for f in $(ls -1 .git/cluster*.yaml 2>/dev/null) ; do
    scp "$f" "$REMOTE:$(basename "$f")"
done

for f in $(ls -1 .git/gitconfig* 2>/dev/null) ; do
    scp "$f" "$REMOTE:.$(basename "$f")"
done

_rsh '
    set -ex
    cd ./cluster-deployment-automation/
    if [ ! -d ocp-venv ] ; then
        git checkout "thom311/th/setup-script^{commit}"
        ./setup.sh
        git checkout -
    fi
'

# latest tag is gone: https://quay.io/repository/centos7/postgresql-12-centos7?tab=history
_rsh '
    set -ex
    podman pull quay.io/centos7/postgresql-12-centos7:centos7
    podman pull quay.io/centos7/postgresql-12-centos7:69623db6c74ac2437a2f11c0733e38c4b8dbb6b1
    podman tag quay.io/centos7/postgresql-12-centos7:69623db6c74ac2437a2f11c0733e38c4b8dbb6b1 quay.io/centos7/postgresql-12-centos7:latest
'

_rsh '
    set -ex
    grep -q /var/lib/containers /etc/fstab && exit 0

    mv /var/lib/containers/ /home/containers
    mkdir /var/lib/containers/
    echo "/home/containers /var/lib/containers none bind 0 0" >> /etc/fstab
    systemctl daemon-reload
    mount /var/lib/containers/
'

if [ -f .git/openshift-oauth-token ] ; then
    # https://oauth-openshift.apps.ci.l2s4.p1.openshiftapps.com/oauth/token/request
    _rsh "
        podman login -u \"$USER\" --password-stdin registry.ci.openshift.org || :
" < .git/openshift-oauth-token
fi
