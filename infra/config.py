from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pulumi


SUPPORTED_TARGETS = {"local", "gcp", "aws"}


@dataclass(frozen=True)
class GitOpsRepo:
    name: str
    url: str
    path: str
    revision: str


@dataclass(frozen=True)
class ClusterConfig:
    target: str
    cluster_name: str
    kubernetes_version: str | None
    argocd_namespace: str
    argocd_chart_version: str
    gitops_repos: tuple[GitOpsRepo, ...]
    local_kind_node_image: str
    local_kind_gpu: bool
    local_kind_device_plugin_chart_version: str
    gcp_location: str
    gcp_node_machine_type: str
    gcp_node_min_count: int
    gcp_node_max_count: int
    gcp_master_authorized_cidr_blocks: list[dict[str, str]]
    gcp_gpu_node_pool_enabled: bool
    gcp_gpu_machine_type: str
    gcp_gpu_accelerator_type: str
    gcp_gpu_accelerator_count: int
    gcp_gpu_node_min_count: int
    gcp_gpu_node_max_count: int
    aws_node_instance_type: str
    aws_node_min_count: int
    aws_node_max_count: int
    aws_endpoint_public_access_cidrs: list[str]
    aws_gpu_node_group_enabled: bool
    aws_gpu_instance_type: str
    aws_gpu_node_min_count: int
    aws_gpu_node_max_count: int
    nvidia_device_plugin_manifest_url: str


def _stack_target(config: pulumi.Config) -> str:
    configured = config.get("target")
    if configured:
        return configured

    stack = pulumi.get_stack()
    if stack in SUPPORTED_TARGETS:
        return stack

    return "local"


def _default_kubernetes_version(target: str) -> str | None:
    if target == "local":
        return "v1.34.0"
    if target == "aws":
        return "1.35"

    # GKE's Stable channel is the default source of truth. Set
    # test-cluster:kubernetesVersion when you need a deterministic pin.
    return None


def _repo_path(path_template: str, target: str) -> str:
    return path_template.format(target=target)


def _get_object_list(config: pulumi.Config, key: str, default: list[Any]) -> list[Any]:
    value = config.get_object(key)
    if value is None:
        return default
    if not isinstance(value, list):
        raise ValueError(f"Config key {key!r} must be a list.")
    return value


def load_config() -> ClusterConfig:
    config = pulumi.Config()
    target = _stack_target(config)
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"Unsupported target {target!r}. Expected one of: {sorted(SUPPORTED_TARGETS)}.")

    default_name = f"test-cluster-{target}"
    base_repo_url = config.get("baseRepoUrl") or "https://github.com/nimeshamin/test_cluster_k8s_base.git"
    app_repo_url = config.get("appRepoUrl") or "https://github.com/nimeshamin/test_cluster_k8s_app.git"

    repos = (
        GitOpsRepo(
            name="platform-base",
            url=base_repo_url,
            path=_repo_path(config.get("baseRepoPath") or "environments/{target}", target),
            revision=config.get("baseRepoRevision") or "main",
        ),
        GitOpsRepo(
            name="application-services",
            url=app_repo_url,
            path=_repo_path(config.get("appRepoPath") or "environments/{target}", target),
            revision=config.get("appRepoRevision") or "main",
        ),
    )

    return ClusterConfig(
        target=target,
        cluster_name=config.get("clusterName") or default_name,
        kubernetes_version=config.get("kubernetesVersion") or _default_kubernetes_version(target),
        argocd_namespace=config.get("argocdNamespace") or "argocd",
        argocd_chart_version=config.get("argocdChartVersion") or "9.5.14",
        gitops_repos=repos,
        local_kind_node_image=(
            config.get("kindNodeImage")
            or f"kindest/node:{config.get('kubernetesVersion') or _default_kubernetes_version(target) or 'v1.34.0'}"
        ),
        local_kind_gpu=config.get_bool("kindGpu") if config.get("kindGpu") is not None else True,
        local_kind_device_plugin_chart_version=(
            config.get("kindDevicePluginChartVersion") or "0.19.1"
        ),
        gcp_location=config.get("gcpLocation") or "us-central1",
        gcp_node_machine_type=config.get("gcpNodeMachineType") or "e2-standard-4",
        gcp_node_min_count=config.get_int("gcpNodeMinCount") or 2,
        gcp_node_max_count=config.get_int("gcpNodeMaxCount") or 4,
        gcp_master_authorized_cidr_blocks=_get_object_list(
            config,
            "gcpMasterAuthorizedCidrBlocks",
            [{"name": "local-admin", "cidrBlock": "0.0.0.0/0"}],
        ),
        gcp_gpu_node_pool_enabled=(
            config.get_bool("gcpGpuNodePoolEnabled") if config.get("gcpGpuNodePoolEnabled") is not None else True
        ),
        gcp_gpu_machine_type=config.get("gcpGpuMachineType") or "g2-standard-4",
        gcp_gpu_accelerator_type=config.get("gcpGpuAcceleratorType") or "nvidia-l4",
        gcp_gpu_accelerator_count=config.get_int("gcpGpuAcceleratorCount") or 1,
        gcp_gpu_node_min_count=config.get_int("gcpGpuNodeMinCount") or 0,
        gcp_gpu_node_max_count=config.get_int("gcpGpuNodeMaxCount") or 1,
        aws_node_instance_type=config.get("awsNodeInstanceType") or "t3.xlarge",
        aws_node_min_count=config.get_int("awsNodeMinCount") or 2,
        aws_node_max_count=config.get_int("awsNodeMaxCount") or 4,
        aws_endpoint_public_access_cidrs=_get_object_list(
            config,
            "awsEndpointPublicAccessCidrs",
            ["0.0.0.0/0"],
        ),
        aws_gpu_node_group_enabled=(
            config.get_bool("awsGpuNodeGroupEnabled") if config.get("awsGpuNodeGroupEnabled") is not None else True
        ),
        aws_gpu_instance_type=config.get("awsGpuInstanceType") or "g4dn.xlarge",
        aws_gpu_node_min_count=config.get_int("awsGpuNodeMinCount") or 0,
        aws_gpu_node_max_count=config.get_int("awsGpuNodeMaxCount") or 1,
        nvidia_device_plugin_manifest_url=(
            config.get("nvidiaDevicePluginManifestUrl")
            or "https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.19.1/deployments/static/nvidia-device-plugin.yml"
        ),
    )
