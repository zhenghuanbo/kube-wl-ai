#!/bin/sh
#
# ENTRYPOINT for a kube-wl-ai instance container.
#
# Reachable only when the platform launches no IDE: for jupyter / vscode /
# webssh it sets the IDE argv as the container *command*, and a Kubernetes
# command replaces ENTRYPOINT entirely, so /opt/wl/bin/<ide> runs instead of
# this. Both routes call runtime.sh, so either one leaves sshd running.
#
# The case this exists for is `ide_type=none`, which is a legitimate instance: an
# SSH-only box for someone who wants a shell and nothing else.
set -eu

/opt/wl/runtime.sh

if [ "$#" -gt 0 ]; then
    # A command was supplied after all (an operator debugging with
    # `kubectl exec`-style overrides, or a future platform that switches command
    # to args). Run it as the instance's own account, like the wrappers do.
    exec gosu "$(cat /run/wl/user)" "$@"
fi

# No command: sshd is up and there is nothing else to serve. PID 1 has to stay
# alive or the pod restarts in a loop, and `sleep infinity` is the honest way to
# say "this container is a login target".
exec sleep infinity
