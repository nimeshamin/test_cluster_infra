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
    local_minikube_driver: str
    local_minikube_nodes: int
    local_minikube_cpus: int
    local_minikube_memory_mb: int
    gcp_location: str
    gcp_node_machine_type: str
    gcp_node_min_count: int
    gcp_node_max_count: int
    gcp_master_authorized_cidr_blocks: list[dict[str, str]]
    aws_node_instance_type: str
    aws_node_min_count: int
    aws_node_max_count: int
    aws_endpoint_public_access_cidrs: list[str]


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
        local_minikube_driver=config.get("minikubeDriver") or "docker",
        local_minikube_nodes=config.get_int("minikubeNodes") or 2,
        local_minikube_cpus=config.get_int("minikubeCpus") or 4,
        local_minikube_memory_mb=config.get_int("minikubeMemory") or 7168,
        gcp_location=config.get("gcpLocation") or "us-central1",
        gcp_node_machine_type=config.get("gcpNodeMachineType") or "e2-standard-4",
        gcp_node_min_count=config.get_int("gcpNodeMinCount") or 2,
        gcp_node_max_count=config.get_int("gcpNodeMaxCount") or 4,
        gcp_master_authorized_cidr_blocks=_get_object_list(
            config,
            "gcpMasterAuthorizedCidrBlocks",
            [{"name": "local-admin", "cidrBlock": "0.0.0.0/0"}],
        ),
        aws_node_instance_type=config.get("awsNodeInstanceType") or "t3.xlarge",
        aws_node_min_count=config.get_int("awsNodeMinCount") or 2,
        aws_node_max_count=config.get_int("awsNodeMaxCount") or 4,
        aws_endpoint_public_access_cidrs=_get_object_list(
            config,
            "awsEndpointPublicAccessCidrs",
            ["0.0.0.0/0"],
        ),
    )
