#!/usr/bin/env bash
#
# Build and push the kube-wl-ai instance image.
#
#   ./build.sh                    # build only
#   PUSH=1 ./build.sh             # build, then push to Harbor
#   HARBOR=10.28.0.194:30002 ./build.sh
#
# Pushing to library/ needs an account with push rights on that project. The
# platform's own robot (robot$kube-wl-ai) is the one kubelet pulls with, so
# building and pushing is deliberately a human action rather than something the
# API does: an image everyone can select should not be replaceable by a request.

set -euo pipefail

HARBOR="${HARBOR:-10.28.0.194:30002}"
PROJECT="${PROJECT:-library}"
NAME="${NAME:-ubuntu-dev}"
TAG="${TAG:-24.04}"

IMAGE="${HARBOR}/${PROJECT}/${NAME}:${TAG}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> building ${IMAGE}"
docker build -t "${IMAGE}" "${HERE}"

if [ "${PUSH:-0}" != "1" ]; then
    echo "==> built ${IMAGE} (not pushed; set PUSH=1 to push)"
    exit 0
fi

echo "==> pushing ${IMAGE}"
docker push "${IMAGE}"

cat <<EOF
==> pushed ${IMAGE}

Make it selectable in the platform:

  * images are discovered from Harbor by a periodic sync (workload
    sync-images, IMAGE_SYNC_INTERVAL_SECONDS, every 24h by default), so the
    new tag appears on its own — or run it now:

        cd backend && .venv/bin/python -c \\
            'from app.workers.tasks import sync_images; sync_images()'

  * verify it is listed:

        curl -s \$BASE/api/v1/images -H "Authorization: Bearer \$TOKEN" | jq .

Then the platform's ssh_command() will offer "ssh -p <nodeport> <username>@<host>",
and the jupyter IDE will actually start — which is the whole point of this image.
EOF
