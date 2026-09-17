"""Build and version identity.

One definition, imported by the app factory, the health probes and the system
information endpoint.

This used to be a literal in both ``app/main.py`` and ``app/api/v1/health.py``,
which meant two places to bump and, inevitably, a build that reported one version
on ``/healthz`` and another in the OpenAPI document. A version that can disagree
with itself is worse than no version at all: it is the field an operator reads
first when working out what is deployed.
"""

from __future__ import annotations

from pathlib import Path

#: The application version. Bump on release; the Helm chart's ``appVersion`` is
#: expected to match.
VERSION = "0.8.0"

#: The name used in logs, the OpenAPI document and the system page.
APP_NAME = "kube-wl-ai"


def build_info() -> dict[str, str]:
    """Version plus the revision this process was built from, when known.

    The git revision is read at import time and fails soft: a container image
    built without ``.git`` is normal, and a deployment that cannot report its
    revision is still a working deployment. What it must not do is raise on
    startup.
    """
    info = {"version": VERSION, "name": APP_NAME}
    revision = _git_revision()
    if revision:
        info["revision"] = revision
    return info


def _git_revision() -> str | None:
    # Walk up from this file: the repository root is three levels above
    # ``app/core/``, and a source checkout may be run from anywhere.
    for parent in Path(__file__).resolve().parents:
        head = parent / ".git" / "HEAD"
        if head.is_file():
            try:
                raw = head.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if raw.startswith("ref: "):
                ref = parent / ".git" / raw.removeprefix("ref: ").strip()
                try:
                    return ref.read_text(encoding="utf-8").strip()[:12] or None
                except OSError:
                    return None
            return raw[:12] or None
    return None
