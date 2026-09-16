"""Unit tests for Kubernetes naming and quantity parsing.

These are pure functions guarding the boundary with the cluster: a bad namespace
name is an API error at creation time, and a mis-parsed quantity silently
mis-sizes everybody's quota.
"""

from __future__ import annotations

import pytest

from app.k8s.client import gpu_model_from_labels, parse_cpu_milli, parse_memory_mb
from app.k8s.naming import (
    MANAGED_LABEL,
    MANAGED_LABEL_VALUE,
    is_managed_namespace,
    namespace_for,
    slug,
)


def test_namespace_follows_the_documented_scheme() -> None:
    assert namespace_for("lab-a", "alice") == "wl-lab-a-alice"


def test_slug_handles_non_ascii_and_separators() -> None:
    # Chinese names collapse entirely; the ASCII `name` field is what carries
    # identity, which is why the models require one.
    assert slug("Lab A") == "lab-a"
    assert slug("lab___a") == "lab-a"
    assert slug("--lab-a--") == "lab-a"
    assert slug("人工智能实验室") == ""


@pytest.mark.parametrize(
    ("group", "workspace"),
    [
        ("g" * 60, "w" * 60),
        ("a" * 200, "b"),
        ("lab", "w" * 100),
    ],
)
def test_long_names_stay_within_the_dns_limit(group: str, workspace: str) -> None:
    name = namespace_for(group, workspace)

    assert len(name) <= 63
    assert name.startswith("wl-")
    assert name[-1].isalnum()
    assert all(character.islower() or character.isdigit() or character == "-" for character in name)


def test_truncation_keeps_distinct_names_distinct() -> None:
    """Two long labs must not be truncated onto the same namespace."""
    first = namespace_for("a" * 60 + "x", "workspace")
    second = namespace_for("a" * 60 + "y", "workspace")

    assert first != second


def test_managed_guard_recognises_platform_namespaces() -> None:
    assert is_managed_namespace("wl-lab-a-alice")
    assert not is_managed_namespace("kube-system")
    assert not is_managed_namespace("default")
    # A name that merely contains the prefix must not pass.
    assert not is_managed_namespace("not-wl-lab")


def test_managed_label_is_the_documented_one() -> None:
    assert MANAGED_LABEL == "wl-managed"
    assert MANAGED_LABEL_VALUE == "true"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8", 8000),
        ("3850m", 3850),
        ("1", 1000),
        ("500m", 500),
        ("0", 0),
    ],
)
def test_cpu_quantity_is_parsed_into_millicores(value: str, expected: int) -> None:
    assert parse_cpu_milli(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("32Gi", 32768),
        ("1024Mi", 1024),
        ("1Gi", 1024),
        ("1T", 953674),
        ("1073741824", 1024),
    ],
)
def test_memory_quantity_is_parsed_into_mebibytes(value: str, expected: int) -> None:
    assert parse_memory_mb(value) == expected


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-40GB"}, "a100"),
        ({"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}, "h100"),
        ({"nvidia.com/gpu.product": "Tesla-V100-SXM2-32GB"}, "v100"),
        ({"wl-gpu-model": "A800"}, "a800"),
        ({"nvidia.com/gpu.product": "NVIDIA-L40S"}, "l40s"),
        ({}, None),
    ],
)
def test_gpu_model_is_recognised_from_node_labels(
    labels: dict[str, str], expected: str | None
) -> None:
    assert gpu_model_from_labels(labels) == expected


def test_platform_label_wins_over_the_vendor_string() -> None:
    """An operator's explicit override must not be second-guessed."""
    labels = {
        "wl-gpu-model": "h800",
        "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-40GB",
    }
    assert gpu_model_from_labels(labels) == "h800"
