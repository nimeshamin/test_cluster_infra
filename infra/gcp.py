from __future__ import annotations

import yaml

import pulumi
import pulumi_gcp as gcp
import pulumi_kubernetes as k8s

from infra.config import ClusterConfig
from infra.models import KubernetesCluster


def _gke_kubeconfig(cluster_name: str, endpoint: str, cert: str, token: str) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": cluster_name,
                    "cluster": {
                        "server": f"https://{endpoint}",
                        "certificate-authority-data": cert,
                    },
                }
            ],
            "contexts": [
                {
                    "name": cluster_name,
                    "context": {"cluster": cluster_name, "user": cluster_name},
                }
            ],
            "current-context": cluster_name,
            "users": [{"name": cluster_name, "user": {"token": token}}],
        }
    )


def _gke_cluster_ca_certificate(master_auth: object) -> str:
    if isinstance(master_auth, dict):
        return master_auth.get("cluster_ca_certificate") or master_auth["clusterCaCertificate"]
    return master_auth.cluster_ca_certificate


def create_gke_cluster(cfg: ClusterConfig) -> KubernetesCluster:
    network = gcp.compute.Network(
        "gke-network",
        name=cfg.cluster_name,
        auto_create_subnetworks=False,
    )

    subnet = gcp.compute.Subnetwork(
        "gke-subnet",
        name=f"{cfg.cluster_name}-primary",
        region=cfg.gcp_location,
        network=network.id,
        ip_cidr_range="10.70.0.0/20",
        secondary_ip_ranges=[
            {"range_name": "pods", "ip_cidr_range": "10.72.0.0/14"},
            {"range_name": "services", "ip_cidr_range": "10.76.0.0/20"},
        ],
        private_ip_google_access=True,
    )

    router = gcp.compute.Router(
        "gke-router",
        name=f"{cfg.cluster_name}-router",
        region=cfg.gcp_location,
        network=network.id,
    )

    gcp.compute.RouterNat(
        "gke-nat",
        name=f"{cfg.cluster_name}-nat",
        region=cfg.gcp_location,
        router=router.name,
        nat_ip_allocate_option="AUTO_ONLY",
        source_subnetwork_ip_ranges_to_nat="ALL_SUBNETWORKS_ALL_IP_RANGES",
    )

    node_service_account = gcp.serviceaccount.Account(
        "gke-node-service-account",
        account_id=f"{cfg.cluster_name[:20]}-nodes",
        display_name=f"{cfg.cluster_name} GKE nodes",
    )

    project = gcp.organizations.get_project()

    gcp.projects.IAMMember(
        "gke-node-log-writer",
        project=project.project_id,
        role="roles/logging.logWriter",
        member=node_service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    )

    gcp.projects.IAMMember(
        "gke-node-metric-writer",
        project=project.project_id,
        role="roles/monitoring.metricWriter",
        member=node_service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    )

    master_authorized_networks = [
        {
            "display_name": item["name"],
            "cidr_block": item["cidrBlock"],
        }
        for item in cfg.gcp_master_authorized_cidr_blocks
    ]

    cluster_args = {
        "name": cfg.cluster_name,
        "location": cfg.gcp_location,
        "network": network.id,
        "subnetwork": subnet.id,
        "remove_default_node_pool": True,
        "initial_node_count": 1,
        "deletion_protection": False,
        "networking_mode": "VPC_NATIVE",
        "datapath_provider": "ADVANCED_DATAPATH",
        "enable_shielded_nodes": True,
        "release_channel": {"channel": "STABLE"},
        "ip_allocation_policy": {
            "cluster_secondary_range_name": "pods",
            "services_secondary_range_name": "services",
        },
        "private_cluster_config": {
            "enable_private_nodes": True,
            "enable_private_endpoint": False,
            "master_ipv4_cidr_block": "172.16.0.0/28",
        },
        "workload_identity_config": {
            "workload_pool": f"{project.project_id}.svc.id.goog",
        },
        "addons_config": {
            "http_load_balancing": {"disabled": True},
            "horizontal_pod_autoscaling": {"disabled": False},
            "gce_persistent_disk_csi_driver_config": {"enabled": True},
        },
        "master_authorized_networks_config": {
            "cidr_blocks": master_authorized_networks,
        },
    }
    if cfg.kubernetes_version:
        cluster_args["min_master_version"] = cfg.kubernetes_version

    cluster = gcp.container.Cluster(
        "gke-cluster",
        **cluster_args,
        opts=pulumi.ResourceOptions(depends_on=[subnet]),
    )

    node_pool_args = {
        "name": "primary",
        "cluster": cluster.name,
        "location": cfg.gcp_location,
        "node_count": cfg.gcp_node_min_count,
        "autoscaling": {
            "min_node_count": cfg.gcp_node_min_count,
            "max_node_count": cfg.gcp_node_max_count,
        },
        "management": {
            "auto_repair": True,
            "auto_upgrade": True,
        },
        "upgrade_settings": {
            "max_surge": 1,
            "max_unavailable": 0,
        },
        "node_config": {
            "machine_type": cfg.gcp_node_machine_type,
            "disk_size_gb": 80,
            "disk_type": "pd-balanced",
            "image_type": "COS_CONTAINERD",
            "service_account": node_service_account.email,
            "oauth_scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            "metadata": {"disable-legacy-endpoints": "true"},
            "shielded_instance_config": {
                "enable_secure_boot": True,
                "enable_integrity_monitoring": True,
            },
            "workload_metadata_config": {"mode": "GKE_METADATA"},
        },
    }
    if cfg.kubernetes_version:
        node_pool_args["version"] = cfg.kubernetes_version

    node_pool = gcp.container.NodePool("gke-node-pool", **node_pool_args)

    gke_depends_on: list[pulumi.Resource] = [cluster, node_pool]

    if cfg.gcp_gpu_node_pool_enabled:
        gpu_node_pool_args = {
            "name": "gpu",
            "cluster": cluster.name,
            "location": cfg.gcp_location,
            "node_count": cfg.gcp_gpu_node_min_count,
            "autoscaling": {
                "min_node_count": cfg.gcp_gpu_node_min_count,
                "max_node_count": cfg.gcp_gpu_node_max_count,
            },
            "management": {"auto_repair": True, "auto_upgrade": True},
            "upgrade_settings": {"max_surge": 1, "max_unavailable": 0},
            "node_config": {
                "machine_type": cfg.gcp_gpu_machine_type,
                "disk_size_gb": 100,
                "disk_type": "pd-balanced",
                "image_type": "COS_CONTAINERD",
                "service_account": node_service_account.email,
                "oauth_scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                "metadata": {"disable-legacy-endpoints": "true"},
                "shielded_instance_config": {
                    "enable_secure_boot": True,
                    "enable_integrity_monitoring": True,
                },
                "workload_metadata_config": {"mode": "GKE_METADATA"},
                "guest_accelerators": [
                    {
                        "type": cfg.gcp_gpu_accelerator_type,
                        "count": cfg.gcp_gpu_accelerator_count,
                        "gpu_driver_installation_config": {"gpu_driver_version": "DEFAULT"},
                    }
                ],
                "taints": [
                    {"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"},
                ],
            },
        }
        if cfg.kubernetes_version:
            gpu_node_pool_args["version"] = cfg.kubernetes_version

        gpu_node_pool = gcp.container.NodePool("gke-gpu-node-pool", **gpu_node_pool_args)
        gke_depends_on.append(gpu_node_pool)

    client_config = gcp.organizations.get_client_config()
    cert = cluster.master_auth.apply(_gke_cluster_ca_certificate)
    kubeconfig = pulumi.Output.all(cluster.name, cluster.endpoint, cert, client_config.access_token).apply(
        lambda args: _gke_kubeconfig(args[0], args[1], args[2], args[3])
    )

    provider = k8s.Provider(
        "gke-provider",
        kubeconfig=kubeconfig,
        opts=pulumi.ResourceOptions(depends_on=gke_depends_on),
    )

    return KubernetesCluster(name=cluster.name, provider=provider, depends_on=gke_depends_on)
