"""Unit tests for turning an observed pod into an instance status.

This mapping is the only thing standing between a broken container and the user
finding out about it. It got this wrong once in a way that looked like the
opposite problem: a crash-looping instance was reported as 运行中, so the page
offered an IDE link that could not work and nothing suggested why.

The trap is that **a pod's phase stays `Running` while its container crash-loops**
— the phase describes the pod, and the pod is fine. Only `ready` and the
container's own reason distinguish the two.
"""

from __future__ import annotations

import pytest

from app.k8s.gateway import PodInfo
from app.models.enums import InstanceStatus
from app.services.instances import FATAL_POD_REASONS, status_from_pod


def pod(
    *,
    phase: str = "Running",
    ready: bool = True,
    reason: str | None = None,
    message: str | None = None,
) -> PodInfo:
    return PodInfo(
        name="wl-lab-test-1-abc12",
        phase=phase,
        ready=ready,
        node_name="gpu-01",
        reason=reason,
        message=message,
    )


def test_a_ready_running_pod_is_running() -> None:
    status, message = status_from_pod(pod())

    assert status is InstanceStatus.RUNNING
    # A healthy instance must not carry a stale explanation from a previous sweep.
    assert message is None


@pytest.mark.parametrize(
    "reason",
    ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "ContainerCannotRun"],
)
def test_a_crash_looping_container_is_an_error_even_though_the_pod_is_running(
    reason: str,
) -> None:
    """The bug this file exists for: phase `Running`, container never started."""
    status, message = status_from_pod(
        pod(ready=False, reason=reason, message=f"{reason}: something went wrong")
    )

    assert status is InstanceStatus.ERROR
    assert reason in (message or "")


def test_every_fatal_reason_is_actually_reachable() -> None:
    """Guards against a name in the set that no container state ever reports."""
    for reason in FATAL_POD_REASONS:
        status, _ = status_from_pod(pod(ready=False, reason=reason, message=reason))
        assert status is InstanceStatus.ERROR, reason


def test_running_but_unready_without_a_failing_container_is_still_starting() -> None:
    """A readiness probe that has not passed yet is not a failure."""
    status, message = status_from_pod(pod(phase="Running", ready=False))

    assert status is InstanceStatus.CREATING
    assert message  # the user is told it is not up yet


def test_an_unknown_failure_reason_does_not_claim_a_failure() -> None:
    """An unanticipated reason must not be reported as an error we cannot explain.

    Reporting `error` here would be the platform inventing a verdict; leaving it
    as "still starting" is recoverable and true for a slow image pull.
    """
    status, _ = status_from_pod(pod(ready=False, reason="SomeNewK8sReason", message="…"))

    assert status is InstanceStatus.CREATING


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("Pending", InstanceStatus.CREATING),
        ("Failed", InstanceStatus.FAILED),
        ("Succeeded", InstanceStatus.STOPPED),
        ("Unknown", InstanceStatus.ERROR),
    ],
)
def test_a_pod_that_is_not_running_follows_its_phase(phase: str, expected: InstanceStatus) -> None:
    status, _ = status_from_pod(pod(phase=phase, ready=False))

    assert status is expected


def test_an_unrecognised_phase_is_treated_as_still_starting() -> None:
    """`Creating` is the safe default: it invites another look instead of a verdict."""
    status, _ = status_from_pod(pod(phase="SomethingNew", ready=False))

    assert status is InstanceStatus.CREATING
