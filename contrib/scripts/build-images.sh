#!/bin/bash

set -e

die() {
    printf "%s\n" "$*" >&2
    exit 1
}

# Arguments:
OCP_RELEASE="$1"
NAME="$2"
REGISTRY="$3"
KEEP_GITDIR="${KEEP_GITDIR:-0}"
DOWNSTREAM="${DOWNSTREAM:-0}"
REBUILD="${REBUILD:-1}"
PUSH="${PUSH:-1}"

# Sanitize:
if [ "$DOWNSTREAM" != 1 ] ; then
    DOWNSTREAM=0
fi

# Error checking
if [ "$KEEP_GITDIR" = 1 -a -z "$OCP_RELEASE" ] ; then
    die "When setting KEEP_GITDIR=1, you must prepare the git directory. That requires also setting a specific \$OCP_RELEASE"
fi

if [ $# -gt 3 -o -z "$REGISTRY" ] ; then
    cat <<EOF
$0 OCP_RELEASE NAME REGISTRY

Build container images of openshift projects and push them to a registry.

  NAME and OCP_RELEASE can both empty, to build all supported names/ocp-versions

  Environment variables:
  - KEEP_GITDIR=1: if set and the git directory at /tmp/cda-build-images-*/ is already checked out,
       build it without cleanup. By default, the directory will be reset to the corresponding
       \$OCP_RELEASE branch.
  - DOWNSTREAM=1: normally, we use upstream images (Dockerfile). Set DOWNSTREAM=1 to build
       downstream images (Dockerfile.rhel7). Note that this requires a private build dependency
       and such builds shall not be published.
  - REBUILD=0: if the image already exits, don't rebuild it.
  - PUSH=0: by default, the built images get pushed. Skip the push step.
  - PODMAN_PUSH_ARGS=: pass additional arguments to podman-push, for example \"--tls-verify=false\" or \"--cert-dir ...\".

The script writes /tmp/cda-build-images.env. Check there and export the names.

How to and examples:

    A.1) run local container registry:

      $ CDA_LOG_LEVEL=debug \\
        python -c 'if 1:
                import reglocal, host

                reg = reglocal.ensure_running(host.LocalHost(), delete_all=False)
                print(f"registry: {reg[1]}:{reg[2]}")
            '

    A.2) or, run local container registry and trust the CA certificate in openshift:

      $ KUBECONFIG=/root/kubeconfig.nicmodecluster \\
        CDA_LOG_LEVEL=debug \\
        python -c 'if 1:
                import reglocal, host, k8sClient, os

                lsh = host.LocalHost()
                client = k8sClient.K8sClient(os.getenv("KUBECONFIG"))
                reg = reglocal.start_image_registry(lsh, client)
                cert_dir = reglocal.get_certificate_path(lsh)
                print("")
                print(f"REGISTRY=\\"{reg}\\"")
                print(f"PODMAN_PUSH_ARGS=\\"--cert-dir={cert_dir}\\"")
            '

    B.1) build upstream for all OCP_RELEASES and projects and push to quay.io:

      $ ./contrib/scripts/build-images.sh "" "" "quay.io/\$USER"

    B.2) or, build downstream and push to local registry from step A.x):

      $ OCP_RELEASE=... \\
        DOWNSTREAM=1 \\
        PODMAN_PUSH_ARGS="--cert-dir=\$HOME/.local-container-registry/certs" \\
        ./contrib/scripts/build-images.sh "\$OCP_RELEASE" "" "\$(hostname -f):5000"

    C) use the images by setting the environment variables from
      /tmp/cda-build-images.env:

      # Check the output
      $ sed -n "s/.*\$OCP_RELEASE$/export \\0/p" /tmp/cda-build-images.env

      # Source it
      $ \$( sed -n "s/.*\$OCP_RELEASE$/export \\0/p" /tmp/cda-build-images.env )

    D) either run CDA deploy with "sriov_network_operator" postconfig (with or
      without "sriov_network_operator_local:True") or run \`make undeploy && make
      deploy-setup\` in the sriov-network-operator. The environment variables from C)
      will be honored.

EOF
    if [ "$#" -eq 0 ] ; then
        exit 0
    fi
    die "Invalid arguments"
fi

if [ "$DOWNSTREAM" = 1 ] ; then
    echo "Must login in registry.ci.openshift.org. Get token from https://oauth-openshift.apps.ci.l2s4.p1.openshiftapps.com/oauth/token/request"
    podman login registry.ci.openshift.org
fi

NAMES=(
    sriov-cni
    ib-sriov-cni
    sriov-network-device-plugin
    sriov-dp-admission-controller
    sriov-network-config-daemon
    sriov-network-webhook
    sriov-network-operator
)
OCP_RELEASES=(
    4.14
    4.15
    4.16
    4.17
    4.18
)

get_info() {
    local OCP_RELEASE="$1"
    local NAME="$2"

    case "$NAME" in
        sriov-cni | \
        ib-sriov-cni | \
        sriov-network-device-plugin | \
        sriov-dp-admission-controller)
            case "$OCP_RELEASE" in
                4.14|4.15|4.16|4.17|4.18)
                    echo "https://github.com/openshift/$NAME.git"
                    echo "release-$OCP_RELEASE"
                    case "$NAME" in
                        sriov-cni)
                            echo "Dockerfile"
                            echo "Dockerfile.rhel"
                            echo "SRIOV_CNI_IMAGE"
                            ;;
                        ib-sriov-cni)
                            echo "Dockerfile"
                            echo "Dockerfile.rhel7"
                            echo "SRIOV_INFINIBAND_CNI_IMAGE"
                            ;;
                        sriov-network-device-plugin)
                            echo "images/Dockerfile"
                            echo "Dockerfile.rhel7"
                            echo "SRIOV_DEVICE_PLUGIN_IMAGE"
                            ;;
                        sriov-dp-admission-controller)
                            echo "Dockerfile"
                            echo "Dockerfile.rhel7"
                            echo "NETWORK_RESOURCES_INJECTOR_IMAGE"
                            ;;
                        *)
                            return 1
                            ;;
                    esac
                    return 0
                    ;;
            esac
            ;;
        sriov-network-config-daemon | \
        sriov-network-webhook | \
        sriov-network-operator )
            case "$OCP_RELEASE" in
                4.14|4.15|4.16|4.17)
                    echo "https://github.com/openshift/sriov-network-operator.git"
                    echo "release-$OCP_RELEASE"
                    case "$NAME" in
                        sriov-network-config-daemon)
                            echo "Dockerfile.sriov-network-config-daemon"
                            echo "Dockerfile.sriov-network-config-daemon.rhel7"
                            echo "SRIOV_NETWORK_CONFIG_DAEMON_IMAGE"
                            ;;
                        sriov-network-webhook)
                            echo "Dockerfile.webhook"
                            echo "Dockerfile.webhook.rhel7"
                            echo "SRIOV_NETWORK_WEBHOOK_IMAGE"
                            ;;
                        sriov-network-operator)
                            echo "Dockerfile"
                            echo "Dockerfile.rhel7"
                            echo "SRIOV_NETWORK_OPERATOR_IMAGE"
                            ;;
                        *)
                            return 1
                            ;;
                    esac
                    return 0
                    ;;
                4.18)
                    echo "https://github.com/openshift/sriov-network-operator.git"
                    echo "release-$OCP_RELEASE"
                    case "$NAME" in
                        sriov-network-config-daemon)
                            echo "Dockerfile.sriov-network-config-daemon"
                            echo "Dockerfile.sriov-network-config-daemon.ocp"
                            echo "SRIOV_NETWORK_CONFIG_DAEMON_IMAGE"
                            ;;
                        sriov-network-webhook)
                            echo "Dockerfile.webhook"
                            echo "Dockerfile.webhook.ocp"
                            echo "SRIOV_NETWORK_WEBHOOK_IMAGE"
                            ;;
                        sriov-network-operator)
                            echo "Dockerfile"
                            echo "Dockerfile.ocp"
                            echo "SRIOV_NETWORK_OPERATOR_IMAGE"
                            ;;
                        *)
                            return 1
                            ;;
                    esac
                    return 0
                    ;;
            esac
            ;;
    esac
    return 1
}

get_git_dir() {
    echo "/tmp/cda-build-images-$1"
}

get_info_check_or_die() {
    local OCP_RELEASE="$1"
    local NAME="$2"
    get_info "$OCP_RELEASE" "$NAME" 1>/dev/null || die  "Release \"$OCP_RELEASE\" for \"$NAME\" is not registered"
}

get_full_tag() {
    local OCP_RELEASE="$1"
    local NAME="$2"
    local REGISTRY="$3"
    echo "$REGISTRY/$NAME:$OCP_RELEASE"
}

git_patch_sources() {
    case "$(git rev-parse HEAD)" in
        c5092008eaeb6576e46b29985cbb1d467996ae5b | \
        5cdb6619ab5d4f21ea7353f4f01b708b25a96f7c )
            # https://github.com/openshift/sriov-dp-admission-controller/pull/84
            sed '15 s/^FROM golang:1.18.3-alpine as builder$/FROM golang:1.21-alpine as builder/' Dockerfile -i
            ;;
        4ecd7fbf8c1c8f9b1d31afefa4591680ad1b19e2 | \
        71dd406ca6497f85f03d33c121dc478e88926882 )
            # https://github.com/openshift/ib-sriov-cni/pull/54
            sed '8s/^RUN apk add --no-cache --virtual build-dependencies build-base=~0.5 linux-headers=~6.3$/RUN apk add --no-cache --virtual build-dependencies build-base=~0.5/' Dockerfile -i
            ;;
        e1d8f9573fc3346c0b2f631af13fbc583b41c95e)
            # https://github.com/openshift/sriov-dp-admission-controller/pull/85
            sed '15 s/^FROM golang:1.21-alpine as builder$/FROM golang:1.22-alpine as builder/' Dockerfile -i
            ;;
    esac
}

git_init() {
    local OCP_RELEASE="$1"
    local NAME="$2"
    local URL="$(get_info "$OCP_RELEASE" "$NAME" | sed -n 1p)"
    local GBRANCH="$(get_info "$OCP_RELEASE" "$NAME" | sed -n 2p)"
    local GDIR="$(get_git_dir "$NAME")"
    local NEW_GDIR

    if [ -d "$GDIR" ] ; then
        NEW_GDIR=0
    else
        NEW_GDIR=1
        mkdir -p "$GDIR"
    fi
    cd "$GDIR"
    if [ "$NEW_GDIR" = 1 ] ; then
        git clone "$URL" .
    else
        git fetch origin
    fi
    if [ "$KEEP_GITDIR" != 1 ] ; then
        git checkout -f -B "$OCP_RELEASE" -t "origin/$GBRANCH"
        git clean -fdx
        git reset --hard "HEAD"
        git_patch_sources
    fi
}

build() {
    local OCP_RELEASE="$1"
    local NAME="$2"
    local REGISTRY="$3"

    echo "Build release $OCP_RELEASE of $NAME"

    get_info_check_or_die "$OCP_RELEASE" "$NAME"

    local GDIR="$(get_git_dir "$NAME")"
    local FULL_TAG="$(get_full_tag "$OCP_RELEASE" "$NAME" "$REGISTRY")"
    local CONTAINERFILE

    if [ "$DOWNSTREAM" = 1 ] ; then
        CONTAINERFILE="$(get_info "$OCP_RELEASE" "$NAME" | sed -n '4p')"
    else
        CONTAINERFILE="$(get_info "$OCP_RELEASE" "$NAME" | sed -n '3p')"
    fi

    if [ -z "$CONTAINERFILE" ] ; then
        return 0
    fi

    if [ "$REBUILD" != 0 -o -n "$(podman images -q "$FULL_TAG")" ] ; then
        git_init "$OCP_RELEASE" "$NAME"
        podman build -t "$FULL_TAG" -f "$GDIR/$CONTAINERFILE" .
    fi

    if [ "$PUSH" != 0 ] ; then
        podman push "$FULL_TAG" $PODMAN_PUSH_ARGS
    fi

    local ENV_VAR="$(get_info "$OCP_RELEASE" "$NAME" | sed -n '5p')"

    echo "$ENV_VAR=$FULL_TAG" >> /tmp/cda-build-images.env
}

for _NAME in "${NAMES[@]}" ; do
    for _OCP_RELEASE in "${OCP_RELEASES[@]}" ; do
        [ -n "$OCP_RELEASE" -a "$OCP_RELEASE" != "$_OCP_RELEASE" ] && continue
        [ -n "$NAME" -a "$NAME" != "$_NAME" ] && continue
        build "$_OCP_RELEASE" "$_NAME" "$REGISTRY"
    done
done
