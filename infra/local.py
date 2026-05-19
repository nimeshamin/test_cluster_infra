from __future__ import annotations

import hashlib

import pulumi
import pulumi_kubernetes as k8s
from pulumi_command import local
from pulumi_kubernetes import helm

from infra.config import ClusterConfig
from infra.models import KubernetesCluster


# Two-node kind cluster:
#   - control-plane runs CPU workloads (default kubeadm taint is cleared)
#   - worker is labeled `nvidia.com/gpu=present` and tainted with the matching
#     NoSchedule taint so only GPU-requesting pods land there.
# GPU access into pods on the worker is wired up by a post-create script that
# installs nvidia-container-toolkit inside the node container and reconfigures
# the in-node containerd to use the nvidia OCI runtime.
KIND_CONFIG_TEMPLATE = """\
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: {cluster_name}
nodes:
  - role: control-plane
    image: {node_image}
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          taints: []
  - role: worker
    image: {node_image}
    labels:
      # NVIDIA's k8s-device-plugin helm chart pins a required nodeAffinity
      # on `nvidia.com/gpu.present=true` (dot, not slash) — without this
      # label the DaemonSet's desired count stays 0.
      nvidia.com/gpu.present: "true"
    extraMounts:
      # The host's nvidia OCI runtime only injects WSL CUDA shim libs into
      # containers that explicitly request a GPU (--gpus all / NVIDIA_*
      # env). kind doesn't pass either, so without this bind-mount the
      # worker node has /dev/dxg but no libnvidia-ml.so → nvidia-ctk's
      # auto-CDI mode fails to init NVML inside pods.
      - hostPath: /usr/lib/wsl
        containerPath: /usr/lib/wsl
        readOnly: true
    kubeadmConfigPatches:
      - |
        kind: JoinConfiguration
        nodeRegistration:
          taints:
            - key: nvidia.com/gpu
              value: present
              effect: NoSchedule
"""


# Installed inside the GPU worker node. Adds NVIDIA's apt repo, installs
# nvidia-container-toolkit, then runs `nvidia-ctk runtime configure` which
# rewrites /etc/containerd/config.toml so nvidia is the default OCI runtime.
# The nvidia runtime is a runc wrapper — it only injects GPU mounts when the
# container declares it wants them (via NVIDIA_VISIBLE_DEVICES, which the
# device plugin sets), so non-GPU pods on this node behave like under runc.
GPU_NODE_SETUP_SCRIPT = """\
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates
install -d -m 0755 /etc/apt/keyrings
# apt 2.4+ accepts the armored key directly via signed-by, so we skip
# `gpg --dearmor` entirely (gpg fails in this exec context with
# `cannot open /dev/tty`).
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\
  -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.asc
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \\
  sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.asc] https://#g' \\
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq
apt-get install -y -qq nvidia-container-toolkit
# Tell the dynamic linker about /usr/lib/wsl/lib so nvidia-container-runtime
# (and nvidia-smi from the same dir) can find libnvidia-ml.so.1 / libcuda.so.1.
echo /usr/lib/wsl/lib > /etc/ld.so.conf.d/ld.wsl.conf
ldconfig
nvidia-ctk runtime configure --runtime=containerd --set-as-default
systemctl restart containerd
"""


def create_kind_cluster(cfg: ClusterConfig) -> KubernetesCluster:
    cluster_name = cfg.cluster_name
    node_image = cfg.local_kind_node_image
    config_yaml = KIND_CONFIG_TEMPLATE.format(cluster_name=cluster_name, node_image=node_image)

    # `kind create cluster --config=-` reads cluster config from stdin. We
    # pre-delete any leftover cluster of the same name so re-running pulumi up
    # after a triggers-only change is idempotent.
    create_command = (
        f"kind delete cluster --name={cluster_name} 2>/dev/null; "
        "cat <<'KINDEOF' | kind create cluster --config=-\n"
        f"{config_yaml}"
        "KINDEOF"
    )

    # Hash the rendered config so edits to KIND_CONFIG_TEMPLATE force a
    # cluster recreate — kind itself can't reconfigure labels/taints in place.
    config_hash = hashlib.sha256(config_yaml.encode()).hexdigest()[:12]

    cluster = local.Command(
        "kind-create",
        create=create_command,
        update=create_command,
        delete=f"kind delete cluster --name={cluster_name} || true",
        triggers=[cluster_name, node_image, str(cfg.local_kind_gpu), config_hash],
    )

    setup_depends: list[pulumi.Resource] = [cluster]

    if cfg.local_kind_gpu:
        # Kind names worker containers "<cluster>-worker", "<cluster>-worker2",
        # etc. We have one worker, so the name is deterministic. Pipe the
        # script via a quoted heredoc so single quotes inside (e.g. in sed
        # expressions) survive into the container shell unmangled.
        gpu_setup_command = (
            f"docker exec -i {cluster_name}-worker bash -s <<'NVSETUP'\n"
            f"{GPU_NODE_SETUP_SCRIPT}"
            "NVSETUP\n"
        )
        gpu_setup = local.Command(
            "kind-gpu-setup",
            create=gpu_setup_command,
            update=gpu_setup_command,
            # config_hash flips whenever the cluster is replaced, so this
            # script always re-runs against the fresh node container.
            triggers=[config_hash],
            opts=pulumi.ResourceOptions(depends_on=[cluster]),
        )
        setup_depends.append(gpu_setup)

    kubeconfig = local.Command(
        "kind-kubeconfig",
        create=f"kind get kubeconfig --name={cluster_name}",
        update=f"kind get kubeconfig --name={cluster_name}",
        triggers=[config_hash],
        opts=pulumi.ResourceOptions(depends_on=setup_depends),
    )

    provider = k8s.Provider(
        "kind-provider",
        kubeconfig=kubeconfig.stdout,
        opts=pulumi.ResourceOptions(depends_on=[kubeconfig]),
    )

    cluster_depends: list[pulumi.Resource] = [cluster, kubeconfig]

    if cfg.local_kind_gpu:
        # Scoped to the GPU node by nodeSelector + matching toleration. The
        # default upstream manifest deploys to every node and would crashloop
        # on CPU-only nodes that lack the nvidia runtime.
        device_plugin = helm.v3.Release(
            "nvidia-device-plugin",
            chart="nvidia-device-plugin",
            version=cfg.local_kind_device_plugin_chart_version,
            namespace="kube-system",
            repository_opts=helm.v3.RepositoryOptsArgs(
                repo="https://nvidia.github.io/k8s-device-plugin"
            ),
            create_namespace=False,
            values={
                "nodeSelector": {"nvidia.com/gpu.present": "true"},
                "tolerations": [
                    {
                        "key": "nvidia.com/gpu",
                        "operator": "Equal",
                        "value": "present",
                        "effect": "NoSchedule",
                    },
                ],
            },
            opts=pulumi.ResourceOptions(
                provider=provider,
                depends_on=[kubeconfig],
                delete_before_replace=True,
            ),
        )
        cluster_depends.append(device_plugin)

    return KubernetesCluster(name=cluster_name, provider=provider, depends_on=cluster_depends)
