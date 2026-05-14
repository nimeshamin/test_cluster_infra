from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pulumi
import pulumi_kubernetes as k8s
from pulumi_kubernetes import apiextensions, core, helm

from infra.config import ClusterConfig, GitOpsRepo


ALLOWED_HELM_REPOS = [
    "https://istio-release.storage.googleapis.com/charts",
    "https://grafana.github.io/helm-charts",
    "https://prometheus-community.github.io/helm-charts",
]


@dataclass(frozen=True)
class ArgoCdInstall:
    release: helm.v3.Release
    initial_admin_password_command: str


def _repo_secret_name(repo: GitOpsRepo) -> str:
    return f"argocd-repo-{repo.name}".replace("_", "-")


def _application_name(repo: GitOpsRepo, target: str) -> str:
    return f"{target}-{repo.name}".replace("_", "-")


def install_argocd(
    cfg: ClusterConfig,
    provider: k8s.Provider,
    depends_on: Sequence[pulumi.Resource],
) -> ArgoCdInstall:
    options = pulumi.ResourceOptions(provider=provider, depends_on=list(depends_on))

    namespace = core.v1.Namespace(
        "argocd-namespace",
        metadata={"name": cfg.argocd_namespace},
        opts=options,
    )

    release = helm.v3.Release(
        "argocd",
        name="argocd",
        chart="argo-cd",
        version=cfg.argocd_chart_version,
        namespace=cfg.argocd_namespace,
        repository_opts=helm.v3.RepositoryOptsArgs(repo="https://argoproj.github.io/argo-helm"),
        create_namespace=False,
        values={
            "fullnameOverride": "argocd",
            "global": {"logging": {"format": "json"}},
            "crds": {"install": True, "keep": True},
            "configs": {
                "params": {
                    "server.insecure": True,
                    "server.disable.auth": False,
                },
                "cm": {
                    "timeout.reconciliation": "60s",
                    "application.resourceTrackingMethod": "annotation",
                    "kustomize.buildOptions": "--load-restrictor LoadRestrictionsNone",
                },
                "rbac": {"policy.default": "role:readonly"},
            },
            "server": {
                "service": {"type": "ClusterIP"},
                "ingress": {"enabled": False},
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"memory": "512Mi"},
                },
            },
            "controller": {
                "resources": {
                    "requests": {"cpu": "250m", "memory": "512Mi"},
                    "limits": {"memory": "1Gi"},
                }
            },
            "repoServer": {
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"memory": "512Mi"},
                }
            },
            "applicationSet": {"enabled": True},
            "notifications": {"enabled": False},
            "dex": {"enabled": False},
        },
        opts=pulumi.ResourceOptions(
            provider=provider,
            depends_on=[namespace],
            delete_before_replace=True,
        ),
    )

    repo_urls = [repo.url for repo in cfg.gitops_repos]
    allowed_source_repos = repo_urls + ALLOWED_HELM_REPOS

    for repo in cfg.gitops_repos:
        core.v1.Secret(
            _repo_secret_name(repo),
            metadata={
                "name": _repo_secret_name(repo),
                "namespace": cfg.argocd_namespace,
                "labels": {"argocd.argoproj.io/secret-type": "repository"},
            },
            string_data={
                "name": repo.name,
                "type": "git",
                "url": repo.url,
            },
            type="Opaque",
            opts=pulumi.ResourceOptions(
                provider=provider,
                depends_on=[release],
                delete_before_replace=True,
            ),
        )

    bootstrap_project = apiextensions.CustomResource(
        "argocd-bootstrap-project",
        api_version="argoproj.io/v1alpha1",
        kind="AppProject",
        metadata={"name": "bootstrap", "namespace": cfg.argocd_namespace},
        spec={
            "description": "Root project for platform and application GitOps repositories.",
            "sourceRepos": allowed_source_repos,
            "destinations": [{"server": "https://kubernetes.default.svc", "namespace": "*"}],
            "clusterResourceWhitelist": [{"group": "*", "kind": "*"}],
            "namespaceResourceWhitelist": [{"group": "*", "kind": "*"}],
        },
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[release]),
    )

    for repo in cfg.gitops_repos:
        apiextensions.CustomResource(
            f"argocd-application-{repo.name}",
            api_version="argoproj.io/v1alpha1",
            kind="Application",
            metadata={
                "name": _application_name(repo, cfg.target),
                "namespace": cfg.argocd_namespace,
                "finalizers": ["resources-finalizer.argocd.argoproj.io"],
            },
            spec={
                "project": "bootstrap",
                "source": {
                    "repoURL": repo.url,
                    "targetRevision": repo.revision,
                    "path": repo.path,
                },
                "destination": {
                    "server": "https://kubernetes.default.svc",
                    "namespace": cfg.argocd_namespace,
                },
                "syncPolicy": {
                    "automated": {"prune": True, "selfHeal": True, "allowEmpty": True},
                    "syncOptions": [
                        "CreateNamespace=true",
                        "PrunePropagationPolicy=foreground",
                        "ServerSideApply=true",
                    ],
                    "retry": {
                        "limit": 5,
                        "backoff": {
                            "duration": "10s",
                            "factor": 2,
                            "maxDuration": "3m",
                        },
                    },
                },
            },
            opts=pulumi.ResourceOptions(provider=provider, depends_on=[release, bootstrap_project]),
        )

    return ArgoCdInstall(
        release=release,
        initial_admin_password_command=(
            f"kubectl -n {cfg.argocd_namespace} get secret argocd-initial-admin-secret "
            "-o jsonpath='{.data.password}' | base64 -d"
        ),
    )
