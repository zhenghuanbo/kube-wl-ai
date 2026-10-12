# syntax=docker/dockerfile:1
#
# kube-wl-ai instance image — Ubuntu 24.04 + conda + the three IDEs the platform
# can launch, with China-side package mirrors baked in.
#
# This is not a general-purpose base image: it implements the *contract* the
# platform expects of an instance image. Read ./README.md before changing it —
# every requirement there is enforced by something under backend/app/k8s/.
#
#   docker build -t 10.28.0.194:30002/library/ubuntu-dev:24.04 \
#       deploy/images/ubuntu-dev
#
# ./build.sh wraps that and the push.

FROM ubuntu:24.04

# --- mirrors and pins --------------------------------------------------------
# Every external fetch goes through a mirror by default. This image is built on a
# campus network where the upstreams range from slow to unreachable, and a build
# that times out is worse than one pinned to a mirror that does not move. All of
# them are build args, so a builder outside China can point them at the original
# upstreams without editing this file.
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ubuntu
# Used for the very first `apt-get update`, and it must be plain HTTP. The base
# image ships no CA bundle at all (`ca-certificates` is not installed and
# /etc/ssl/certs is empty), so an https mirror cannot be verified until this step
# has installed one — apt fails with "The certificate issuer is unknown" and
# every package then reports "no installation candidate". tuna serves the same
# tree over http, and apt still verifies package signatures against the keyring
# that *is* present, so the bootstrap is not a trust downgrade for packages.
ARG APT_BOOTSTRAP_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/ubuntu
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_HOST=pypi.tuna.tsinghua.edu.cn
ARG CONDA_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/anaconda
ARG MINICONDA_INSTALLER=Miniconda3-py312_25.5.1-0-Linux-x86_64.sh
ARG CODE_SERVER_BASE=https://github.com/coder/code-server/releases/download
ARG CODE_SERVER_VERSION=4.141.0
ARG DEB_ARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# --- apt ---------------------------------------------------------------------
# The base image's deb822 source list is replaced rather than patched: a `sed`
# over archive.ubuntu.com misses security.ubuntu.com, and the two live in one
# file only by coincidence of how Canonical ships it.
#
# Written twice on purpose. The first pass exists only to get ca-certificates in,
# over a mirror that needs no CA bundle; the second switches to https and is the
# one every real package comes from.
RUN set -eux; \
    write_sources() { \
        printf '%s\n' \
            'Types: deb' \
            "URIs: $1" \
            'Suites: noble noble-updates noble-backports' \
            'Components: main restricted universe multiverse' \
            'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
            '' \
            'Types: deb' \
            "URIs: $1" \
            'Suites: noble-security' \
            'Components: main restricted universe multiverse' \
            'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
            > /etc/apt/sources.list.d/ubuntu.sources; \
    }; \
    rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list; \
    write_sources "${APT_BOOTSTRAP_MIRROR}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    write_sources "${APT_MIRROR}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        # Editors, shells and the file tooling people reach for first.
        vim nano less tmux screen tree \
        # "git log --graph" and every git-over-https clone.
        git git-lfs ca-certificates gnupg openssl \
        # --no-install-recommends leaves this out, and every pip/conda install
        # needs it to build a wheel that has no prebuilt one.
        build-essential pkg-config cmake \
        curl wget rsync unzip zip xz-utils bzip2 file \
        # net-tools is the package that provides `netstat`. Also ship `ss` and
        # `ip`, because netstat is the legacy tool and people should meet both.
        net-tools iproute2 iputils-ping dnsutils traceroute telnet \
        procps psmisc lsof htop jq \
        # The SSH server *is* the platform's only way in besides the web IDE.
        openssh-server openssh-client \
        # gosu drops the IDE from root to the instance's own account; sudo gives
        # that account the same reach interactively.
        gosu sudo \
        locales tzdata \
        # libgl1/libglib2.0-0 are what opencv, matplotlib's Qt backend and most
        # scientific wheels dlopen at import time. Without them the failure is a
        # bare "libGL.so.1: cannot open shared object file" deep in a notebook.
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
        ttyd; \
    # The three IDEs the platform can launch all want a UTF-8 locale, and a
    # Chinese-language Jupyter is a real expectation here.
    sed -i 's/^# *\(en_US.UTF-8 UTF-8\)/\1/; s/^# *\(zh_CN.UTF-8 UTF-8\)/\1/' /etc/locale.gen; \
    locale-gen; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

# --- free uid 1000 -----------------------------------------------------------
# The base image ships an `ubuntu` account at uid 1000, and the instance account
# is created at uid 1000 (WL_UID in rootfs/opt/wl/runtime.sh). Leaving it in
# place means `useradd --uid 1000` fails, useradd silently falls back to the next
# free uid, and the home volume stays chowned to 1000 — so every IDE dies with
# "Permission denied: /home/<user>/.local" even though sshd and the account both
# came up. runtime.sh no longer trusts its own requested uid either, but the
# predictable uid is what makes `docker exec` and host-side file ownership
# legible, so the collision is removed rather than tolerated.
RUN set -eux; \
    if id -u ubuntu >/dev/null 2>&1; then \
        userdel --remove ubuntu 2>/dev/null || userdel ubuntu; \
    fi; \
    if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu 2>/dev/null || true; fi; \
    if ! getent group 1000 >/dev/null 2>&1; then groupadd --gid 1000 wl; fi

# --- pip ---------------------------------------------------------------------
# System-wide, so it applies to every interpreter in the image regardless of
# which HOME the platform hands the container — HOME is /home/{username}, and a
# ~/.pip/pip.conf would therefore never be read from /root.
RUN set -eux; \
    printf '%s\n' \
        '[global]' \
        "index-url = ${PIP_INDEX}" \
        "trusted-host = ${PIP_HOST}" \
        'timeout = 120' \
        'disable-pip-version-check = true' \
        > /etc/pip.conf

# --- conda -------------------------------------------------------------------
# Into /opt/conda, the conventional location, so `CONDARC` and the PATH entry
# below have one obvious value rather than one derived at runtime.
ENV CONDA_DIR=/opt/conda \
    CONDARC=/opt/conda/.condarc

RUN set -eux; \
    curl -fsSL "${CONDA_MIRROR}/miniconda/${MINICONDA_INSTALLER}" -o /tmp/miniconda.sh; \
    bash /tmp/miniconda.sh -b -p "$CONDA_DIR"; \
    rm -f /tmp/miniconda.sh; \
    # The base environment is the one users land in; leaving conda's own
    # cache/package tarballs out of the layer saves ~400 MB.
    "$CONDA_DIR/bin/conda" clean -afy; \
    printf '%s\n' \
        'channels:' \
        '  - defaults' \
        'show_channel_urls: true' \
        'default_channels:' \
        "  - ${CONDA_MIRROR}/pkgs/main" \
        "  - ${CONDA_MIRROR}/pkgs/r" \
        'custom_channels:' \
        "  conda-forge: ${CONDA_MIRROR}/cloud" \
        "  pytorch: ${CONDA_MIRROR}/cloud" \
        "  nvidia: ${CONDA_MIRROR}/cloud" \
        'channel_priority: flexible' \
        'auto_activate_base: false' \
        > "$CONDARC"

# --- Python toolchain and JupyterLab -----------------------------------------
# JupyterLab is the platform's default IDE and the one whose ingress prefix it
# already passes correctly (--ServerApp.base_url), so this is the path that has
# to work end to end.
ARG PIP_PACKAGES="jupyterlab notebook ipykernel jupyterlab-language-pack-zh-CN numpy pandas matplotlib"
RUN set -eux; \
    "$CONDA_DIR/bin/python" -m pip install --no-cache-dir $PIP_PACKAGES; \
    # Removing the tarball cache a second time: pip and conda both leave one.
    "$CONDA_DIR/bin/conda" clean -afy

# --- code-server -------------------------------------------------------------
# Pinned to a version rather than "latest": a rebuild of this image should be
# reproducible, and code-server's own updater is disabled below because inside a
# container it can only ever write to a layer that is thrown away.
RUN set -eux; \
    mkdir -p /opt/code-server; \
    curl -fsSL \
        "${CODE_SERVER_BASE}/v${CODE_SERVER_VERSION}/code-server-${CODE_SERVER_VERSION}-linux-${DEB_ARCH}.tar.gz" \
        -o /tmp/code-server.tar.gz; \
    tar -xzf /tmp/code-server.tar.gz -C /opt/code-server --strip-components=1; \
    rm -f /tmp/code-server.tar.gz; \
    /opt/code-server/bin/code-server --version

# --- the instance runtime ----------------------------------------------------
# PATH order is load-bearing, and this is the reason `/opt/wl/bin` comes first:
# the jupyter / code-server / ttyd files placed there are wrappers, and they only
# work as wrappers if nothing earlier on PATH resolves to the real binary. They
# are not installed *over* the real ones — mutating another package's files is
# how you end up with a "which jupyter am I running" that nobody can answer.
ARG INSTANCE_PATH=/opt/wl/bin:/opt/conda/bin:/opt/code-server/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV PATH=${INSTANCE_PATH}

# The same value again, for SSH sessions — and this duplication is the whole
# point rather than an oversight. `ENV PATH` is the *container's* environment and
# sshd does not pass it on: an SSH session gets sshd's own compiled-in default
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games), so
# `conda`, `python` and `jupyter` are all "command not found" to a user who has
# just logged in — the single most confusing possible first impression.
#
# /etc/environment is what PAM reads (pam_env, via /etc/pam.d/sshd), and that is
# the only channel that reaches *non-interactive* SSH commands too: `ssh host
# 'which conda'` runs no profile and no bashrc. It is also why runtime.sh must
# keep UsePAM on; turning PAM off silently strips PATH from every SSH session.
RUN printf 'PATH="%s"\n' "${INSTANCE_PATH}" > /etc/environment

# Containers usually lack CAP_AUDIT_WRITE, and pam_loginuid.so being `required`
# then fails the whole login — for a key that is perfectly valid, reported as a
# bare "Permission denied". `optional` keeps PAM (and therefore /etc/environment,
# and therefore PATH) while removing the one module that depends on a capability
# this pod is not guaranteed to have.
RUN sed -i \
        's/^session\s\+required\s\+pam_loginuid\.so/session    optional     pam_loginuid.so/' \
        /etc/pam.d/sshd; \
    grep -q 'optional.*pam_loginuid\.so' /etc/pam.d/sshd

COPY rootfs/ /

RUN set -eux; \
    chmod 0755 /opt/wl/runtime.sh /opt/wl/entrypoint.sh /opt/wl/bin/*; \
    # A key-based-only shell: no password to leak and nothing to brute force.
    mkdir -p /run/sshd /etc/ssh/authorized_keys.d

# sshd, the three IDE ports, and nothing else. The platform maps 22 to a NodePort
# Service and exactly one IDE port to a ClusterIP Service behind the ingress.
EXPOSE 22 8888 8080 7681

# The ENTRYPOINT matters for `ide_type=none` only: the platform sets the IDE argv
# as the container *command*, and a Kubernetes command replaces ENTRYPOINT
# outright, so for jupyter/vscode/webssh it is the wrapper in /opt/wl/bin that
# runs instead and brings the runtime up itself. Both paths call the same
# runtime.sh, so either one leaves a working sshd behind.
ENTRYPOINT ["/opt/wl/entrypoint.sh"]
