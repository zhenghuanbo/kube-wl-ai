#!/usr/bin/env bash
#
# Runtime preparation for a kube-wl-ai instance container.
#
# Called from two directions, always before an IDE or a login shell is allowed to
# run: the image ENTRYPOINT (reachable only for ide_type=none) and the wrappers
# in /opt/wl/bin (reachable when the platform sets the IDE argv as the container
# command). It must therefore be safe to run more than once — every step below is
# a no-op on the second call.
#
# What it has to achieve, and why each part is not optional:
#
#   * an account named after the owner, because ssh_command() tells the user to
#     run `ssh -p {port} {username}@{host}`.
#   * that account owning HOME, because HOME is the owner's storage volume and
#     kubelet creates hostPath directories as root:root.
#   * sshd listening on 22 with the platform's key file wired in.
#
# Nothing here is inferred from the image build: the username is not known until
# a pod starts, so all of it happens at pod start.

set -euo pipefail

WL_STATE_DIR=/run/wl
WL_USER_FILE="${WL_STATE_DIR}/user"
WL_SSHD_CONFIG=/etc/ssh/sshd_config

log() { printf '[wl-runtime] %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 0. Only root can do any of this
# ---------------------------------------------------------------------------
# Everything below needs root: useradd, chown, writing host keys and
# sshd_config, starting sshd. Reaching here as anyone else means the work has
# already been done by whoever started the container, so this is a no-op rather
# than a failure.
#
# Not hypothetical: `docker run <image> jupyter lab` keeps the ENTRYPOINT (Docker
# replaces CMD, not ENTRYPOINT), so the ENTRYPOINT prepares as root and then
# gosu's to the instance account — which resolves `jupyter` to the wrapper, which
# calls this script again, now unprivileged. The same happens to a user who types
# `jupyter lab` in their own shell. Without this guard both die on a permission
# error before the IDE ever starts.
if [ "$(id -u)" != "0" ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Who is this container for?
# ---------------------------------------------------------------------------
# The platform passes no username. It passes HOME=/home/{username}
# (build_instance_objects sets both HOME and the mount path to the same value),
# so the home directory is the only place the name is written down. Deriving it
# is not a trick to avoid asking: it is what keeps ssh_command() honest, since
# that string and this derivation have to agree or SSH cannot log in.
WL_HOME="${HOME:-/home/dev}"
WL_USER="$(basename "$WL_HOME")"
case "$WL_USER" in
    "" | "/" | "." | "..") WL_USER=dev ;;
esac
WL_UID="${WL_UID:-1000}"
# uid 0 would collide with root, and a home "owned by the user" would then mean
# nothing. The platform passes no uid today; this is here so that adding one
# later cannot quietly produce a root-owned home.
if [ "$WL_UID" = "0" ]; then WL_UID=1000; fi
WL_GID="${WL_GID:-$WL_UID}"

mkdir -p "$WL_STATE_DIR"
printf '%s' "$WL_USER" > "$WL_USER_FILE"

if ! id -u "$WL_USER" >/dev/null 2>&1; then
    # A gid conflict is not fatal: --gid accepts an existing group's number, and
    # the account is still usable. Failing the whole pod start over it would not.
    groupadd --gid "$WL_GID" "$WL_USER" 2>/dev/null || true
    useradd --uid "$WL_UID" --gid "$WL_GID" --home-dir "$WL_HOME" \
        --shell /bin/bash --no-create-home "$WL_USER" 2>/dev/null ||
        useradd --home-dir "$WL_HOME" --shell /bin/bash --no-create-home "$WL_USER"
fi

# Read the uid/gid back instead of trusting WL_UID. useradd picks its own uid when
# the requested one is taken and reports success either way, so the requested
# value is a wish, not a fact. Chowning the home to the wish leaves it owned by
# whoever actually holds that uid, and every IDE then dies with
# "Permission denied: /home/<user>/.local" — while sshd and the account both look
# perfectly healthy. (The image also removes the base image's uid-1000 `ubuntu`
# account so this is normally moot; this is the belt to that pair of braces.)
WL_UID="$(id -u "$WL_USER")"
WL_GID="$(id -g "$WL_USER")"
log "account ${WL_USER} is uid=${WL_UID} gid=${WL_GID} home=${WL_HOME}"

# useradd locks the account by writing "!" into /etc/shadow, and sshd refuses a
# locked account *even for public-key authentication*. The symptom is a bare
# "Permission denied (publickey)" that sends you hunting for a key or permission
# problem, and there is none — the account is simply disabled.
#
# "*" unlocks it and is not a valid password hash, so it can never be matched
# even if PasswordAuthentication were switched on later. Clearing the field works
# too and is what most images do, but an empty field is exactly what an empty
# password looks like, so it becomes a real hole the moment that flag flips.
#
# Only a locked account is touched: a password the user set themselves survives
# a pod restart.
SHADOW_PW="$(getent shadow "$WL_USER" | cut -d: -f2)"
case "$SHADOW_PW" in
    '!'*)
        usermod -p '*' "$WL_USER"
        log "unlocked account ${WL_USER} (useradd leaves it locked; sshd refuses locked accounts)"
        ;;
esac

usermod -aG sudo "$WL_USER" 2>/dev/null || true
printf '%s\n' "${WL_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-${WL_USER}"
chmod 0440 "/etc/sudoers.d/90-${WL_USER}"

# ---------------------------------------------------------------------------
# 2. Give the account its home
# ---------------------------------------------------------------------------
# Chown only when the top directory is already wrong. Recursing unconditionally
# would rescan a home that may hold millions of files on every single pod start,
# and the one thing a developer notices is a container that takes minutes to
# become Ready.
if [ ! -d "$WL_HOME" ]; then
    mkdir -p "$WL_HOME"
fi
if [ "$(stat -c '%u:%g' "$WL_HOME")" != "${WL_UID}:${WL_GID}" ]; then
    log "taking ownership of ${WL_HOME}"
    chown -R "${WL_UID}:${WL_GID}" "$WL_HOME"
fi

# ---------------------------------------------------------------------------
# 3. sshd host keys
# ---------------------------------------------------------------------------
# Kept inside the home volume, not /etc/ssh. /etc/ssh is not a volume, so keys
# minted there are lost when the pod is recreated — and then every user meets
# "REMOTE HOST IDENTIFICATION HAS CHANGED" after every restart, which is exactly
# how people are trained to click past the one warning that exists to signal a
# real interception.
#
# Per *owner*, not per instance: two instances belonging to the same person
# sharing a host identity reveals nothing that the owner does not already know,
# and the alternative (a warning per restart) has a real cost. Different owners
# never share these, because they never share a home volume.
WL_HOSTKEY_DIR="${WL_HOME}/.ssh/wl-hostkeys"
if ! install -d -m 0700 -o "$WL_UID" -g "$WL_GID" "$WL_HOSTKEY_DIR" 2>/dev/null; then
    # A read-only home volume must not stop SSH from working.
    log "cannot write host keys under ${WL_HOME}; using /etc/ssh"
    WL_HOSTKEY_DIR=/etc/ssh
fi
HOSTKEY_LINES=""
for type in rsa ecdsa ed25519; do
    key="${WL_HOSTKEY_DIR}/ssh_host_${type}_key"
    if [ ! -f "$key" ]; then
        ssh-keygen -q -t "$type" -N '' -f "$key" >/dev/null
        log "generated ${type} host key"
    fi
    chmod 0600 "$key"
    chmod 0644 "${key}.pub" 2>/dev/null || true
    HOSTKEY_LINES="${HOSTKEY_LINES}HostKey ${key}
"
done

# ---------------------------------------------------------------------------
# 4. sshd configuration
# ---------------------------------------------------------------------------
# Rewritten on every start rather than shipped in the image, because it has to
# name the host key paths decided above. There is no user-editable state in it:
# an instance that needs different sshd settings needs a different image.
mkdir -p /run/sshd
cat > "$WL_SSHD_CONFIG" <<EOF
# Generated by /opt/wl/runtime.sh on every container start. Edits are lost.

Port 22
ListenAddress 0.0.0.0
${HOSTKEY_LINES}
# The platform mounts the owner's public keys as a directory holding a single
# file named "authorized_keys" (SecretSpec data={"authorized_keys": ...} in
# build_instance_objects). It is deliberately not per-user: the container serves
# exactly one person, so one key file is the whole truth. The second path lets
# the account still manage ~/.ssh/authorized_keys by hand.
AuthorizedKeysFile /etc/ssh/authorized_keys.d/authorized_keys .ssh/authorized_keys

# Keys only. The platform ships no password and rotates none, so a password path
# would be a credential that nothing can ever change.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
PermitRootLogin prohibit-password

# UsePAM is on, and it is load-bearing rather than a default left alone. sshd does
# not pass the container's PATH to a session, and pam_env reading /etc/environment
# is the only channel that reaches *non-interactive* SSH commands too (`ssh host
# 'which conda'` runs no profile and no bashrc). With PAM off, a freshly logged-in
# user finds neither conda nor python nor jupyter — they are on PID 1's PATH and
# nowhere else.
#
# What PAM normally costs in a container is pam_loginuid.so being `required` and
# failing the login when CAP_AUDIT_WRITE is absent, which surfaces as a bare
# "Permission denied" for a valid key. The Dockerfile demotes that one module to
# `optional` instead of switching PAM off wholesale.
UsePAM yes

AllowTcpForwarding yes
X11Forwarding yes
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
PidFile /run/sshd.pid
EOF
chmod 0600 "$WL_SSHD_CONFIG"

# ---------------------------------------------------------------------------
# 5. Start sshd
# ---------------------------------------------------------------------------
# Not `-D`: it has to keep running in the background while the IDE owns PID 1.
# pgrep makes this idempotent, which is what lets the wrappers call this script
# without checking whether the ENTRYPOINT already did.
if ! pgrep -x sshd >/dev/null 2>&1; then
    /usr/sbin/sshd -f "$WL_SSHD_CONFIG"
    log "sshd listening on 22; login user is ${WL_USER}"
fi
