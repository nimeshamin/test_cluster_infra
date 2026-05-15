from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi_command import local

from infra.config import ClusterConfig
from infra.models import KubernetesCluster


def create_minikube_cluster(cfg: ClusterConfig) -> KubernetesCluster:
    version_flag = f" --kubernetes-version={cfg.kubernetes_version}" if cfg.kubernetes_version else ""
    # minikube --gpus all requires the docker driver with the docker container runtime.
    if cfg.local_minikube_gpu:
        container_runtime = "docker"
        gpu_flag = " --gpus=all"
    else:
        container_runtime = "containerd"
        gpu_flag = ""

    start_command = (
        f"minikube start -p {cfg.cluster_name}"
        f"{version_flag}"
        f" --driver={cfg.local_minikube_driver}"
        f" --container-runtime={container_runtime}"
        f"{gpu_flag}"
        f" --nodes={cfg.local_minikube_nodes}"
        f" --cpus={cfg.local_minikube_cpus}"
        f" --memory={cfg.local_minikube_memory_mb}"
        " --addons=default-storageclass,storage-provisioner,metrics-server"
    )

    cluster = local.Command(
        "minikube-start",
        create=start_command,
        update=start_command,
        delete=f"minikube delete -p {cfg.cluster_name}",
        triggers=[
            cfg.cluster_name,
            cfg.kubernetes_version or "",
            cfg.local_minikube_driver,
            container_runtime,
            str(cfg.local_minikube_gpu),
            str(cfg.local_minikube_nodes),
            str(cfg.local_minikube_cpus),
            str(cfg.local_minikube_memory_mb),
        ],
    )

    kubeconfig = local.Command(
        "minikube-kubeconfig",
        create=f"kubectl config view --raw --minify --context={cfg.cluster_name}",
        update=f"kubectl config view --raw --minify --context={cfg.cluster_name}",
        triggers=[cluster.stdout],
        opts=pulumi.ResourceOptions(depends_on=[cluster]),
    )

    provider = k8s.Provider(
        "minikube-provider",
        kubeconfig=kubeconfig.stdout,
        opts=pulumi.ResourceOptions(depends_on=[kubeconfig]),
    )

    cluster_deps: list[pulumi.Resource] = [cluster, kubeconfig]

    if cfg.local_minikube_gpu:
        device_plugin = k8s.yaml.v2.ConfigFile(
            "nvidia-device-plugin",
            file=cfg.nvidia_device_plugin_manifest_url,
            opts=pulumi.ResourceOptions(provider=provider, depends_on=[kubeconfig]),
        )
        cluster_deps.append(device_plugin)

    return KubernetesCluster(name=cfg.cluster_name, provider=provider, depends_on=cluster_deps)
