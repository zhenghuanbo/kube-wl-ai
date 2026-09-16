"""Kubernetes naming rules.

Namespace names are derived, not stored free-form: the backend must be able to
recognise the objects it owns, and an operator must be able to look at a
namespace and know which lab it belongs to.

All names are DNS-1123 labels — lower case, alphanumeric and ``-``, at most 63
characters, starting and ending with an alphanumeric. Getting this wrong produces
an API error at creation time, which is exactly the kind of thing that should be
caught before a request is made.
"""

from __future__ import annotations

import hashlib
import re

#: Every object this platform creates carries this label. The backend's K8s
#: RBAC is scoped to it, so a bug in the application cannot reach namespaces
#: that belong to someone else.
MANAGED_LABEL = "wl-managed"
MANAGED_LABEL_VALUE = "true"
MANAGED_LABEL_SELECTOR = f"{MANAGED_LABEL}={MANAGED_LABEL_VALUE}"

#: Labels attached to every namespace, so ownership is greppable from kubectl.
GROUP_LABEL = "wl-group"
WORKSPACE_LABEL = "wl-workspace"

NAMESPACE_PREFIX = "wl"
MAX_LABEL_LENGTH = 63

_UNSAFE = re.compile(r"[^a-z0-9]+")
_LEADING_TRAILING = re.compile(r"^[-]+|[-]+$")


def slug(value: str) -> str:
    """Reduce an arbitrary name to a DNS-1123-safe fragment.

    Chinese characters, spaces and underscores all collapse to ``-``, so a
    group named ``人工智能实验室`` needs an explicit ASCII ``name`` (which the
    group model already requires) rather than silently becoming empty.
    """
    lowered = value.strip().lower()
    collapsed = _UNSAFE.sub("-", lowered)
    return _LEADING_TRAILING.sub("", collapsed)


def namespace_for(group_name: str, workspace_name: str) -> str:
    """Build the namespace for a workspace: ``wl-{group}-{workspace}``.

    Long names are truncated and suffixed with a digest of the *full* input
    rather than simply cut, so two long labs can never collide onto the same
    namespace.
    """
    raw = f"{NAMESPACE_PREFIX}-{slug(group_name)}-{slug(workspace_name)}"
    raw = _LEADING_TRAILING.sub("", raw)
    if len(raw) <= MAX_LABEL_LENGTH:
        return raw

    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    budget = MAX_LABEL_LENGTH - len(digest) - 1
    return f"{raw[:budget].rstrip('-')}-{digest}"


def is_managed_namespace(name: str) -> bool:
    """Whether a namespace belongs to this platform.

    Used as a guard before any destructive call — the application must never
    delete a namespace it did not create.
    """
    return name.startswith(f"{NAMESPACE_PREFIX}-")
