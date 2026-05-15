from __future__ import annotations

import ipaddress
from typing import Sequence

import yaml

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as k8s

from infra.config import ClusterConfig
from infra.models import KubernetesCluster


def _cidrsubnet(base: str, newbits: int, netnum: int) -> str:
    network = ipaddress.ip_network(base)
    return str(list(network.subnets(new_prefix=network.prefixlen + newbits))[netnum])


def _eks_kubeconfig(cluster_name: str, endpoint: str, cert: str, region: str) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": cluster_name,
                    "cluster": {
                        "server": endpoint,
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
            "users": [
                {
                    "name": cluster_name,
                    "user": {
                        "exec": {
                            "apiVersion": "client.authentication.k8s.io/v1beta1",
                            "command": "aws",
                            "args": ["eks", "get-token", "--cluster-name", cluster_name, "--region", region],
                        }
                    },
                }
            ],
        }
    )


def _eks_oidc_issuer(identities: object) -> str:
    first_identity = identities[0]
    oidcs = first_identity["oidcs"] if isinstance(first_identity, dict) else first_identity.oidcs
    first_oidc = oidcs[0]
    return first_oidc["issuer"] if isinstance(first_oidc, dict) else first_oidc.issuer


def _tag(name: str, cluster_name: str, visibility: str) -> dict[str, str]:
    tags = {
        "Name": name,
        f"kubernetes.io/cluster/{cluster_name}": "shared",
    }
    if visibility == "public":
        tags["kubernetes.io/role/elb"] = "1"
    else:
        tags["kubernetes.io/role/internal-elb"] = "1"
    return tags


def _create_subnets(vpc: aws.ec2.Vpc, cluster_name: str, azs: Sequence[str]) -> tuple[list[aws.ec2.Subnet], list[aws.ec2.Subnet]]:
    public_subnets: list[aws.ec2.Subnet] = []
    private_subnets: list[aws.ec2.Subnet] = []

    for index, az in enumerate(azs):
        public_subnets.append(
            aws.ec2.Subnet(
                f"public-{index}",
                vpc_id=vpc.id,
                cidr_block=_cidrsubnet("10.60.0.0/16", 4, index),
                availability_zone=az,
                map_public_ip_on_launch=True,
                tags=_tag(f"{cluster_name}-public-{index}", cluster_name, "public"),
            )
        )
        private_subnets.append(
            aws.ec2.Subnet(
                f"private-{index}",
                vpc_id=vpc.id,
                cidr_block=_cidrsubnet("10.60.0.0/16", 4, index + 8),
                availability_zone=az,
                map_public_ip_on_launch=False,
                tags=_tag(f"{cluster_name}-private-{index}", cluster_name, "private"),
            )
        )

    return public_subnets, private_subnets


def create_eks_cluster(cfg: ClusterConfig) -> KubernetesCluster:
    region = aws.config.region or "us-west-2"
    azs = aws.get_availability_zones(state="available").names[:2]

    vpc = aws.ec2.Vpc(
        "eks-vpc",
        cidr_block="10.60.0.0/16",
        enable_dns_hostnames=True,
        enable_dns_support=True,
        tags={"Name": cfg.cluster_name},
    )

    internet_gateway = aws.ec2.InternetGateway(
        "eks-igw",
        vpc_id=vpc.id,
        tags={"Name": f"{cfg.cluster_name}-igw"},
    )

    public_subnets, private_subnets = _create_subnets(vpc, cfg.cluster_name, azs)

    public_route_table = aws.ec2.RouteTable(
        "public-route-table",
        vpc_id=vpc.id,
        routes=[{"cidr_block": "0.0.0.0/0", "gateway_id": internet_gateway.id}],
        tags={"Name": f"{cfg.cluster_name}-public"},
    )

    for index, subnet in enumerate(public_subnets):
        aws.ec2.RouteTableAssociation(
            f"public-route-association-{index}",
            subnet_id=subnet.id,
            route_table_id=public_route_table.id,
        )

    nat_eips: list[aws.ec2.Eip] = []
    nat_gateways: list[aws.ec2.NatGateway] = []
    private_route_tables: list[aws.ec2.RouteTable] = []

    for index, subnet in enumerate(public_subnets):
        eip = aws.ec2.Eip(
            f"nat-eip-{index}",
            domain="vpc",
            tags={"Name": f"{cfg.cluster_name}-nat-{index}"},
        )
        nat = aws.ec2.NatGateway(
            f"nat-gateway-{index}",
            subnet_id=subnet.id,
            allocation_id=eip.id,
            tags={"Name": f"{cfg.cluster_name}-nat-{index}"},
            opts=pulumi.ResourceOptions(depends_on=[internet_gateway]),
        )
        route_table = aws.ec2.RouteTable(
            f"private-route-table-{index}",
            vpc_id=vpc.id,
            routes=[{"cidr_block": "0.0.0.0/0", "nat_gateway_id": nat.id}],
            tags={"Name": f"{cfg.cluster_name}-private-{index}"},
        )
        aws.ec2.RouteTableAssociation(
            f"private-route-association-{index}",
            subnet_id=private_subnets[index].id,
            route_table_id=route_table.id,
        )
        nat_eips.append(eip)
        nat_gateways.append(nat)
        private_route_tables.append(route_table)

    cluster_role = aws.iam.Role(
        "eks-cluster-role",
        assume_role_policy=aws.iam.get_policy_document(
            statements=[
                {
                    "actions": ["sts:AssumeRole"],
                    "principals": [{"type": "Service", "identifiers": ["eks.amazonaws.com"]}],
                }
            ]
        ).json,
        tags={"Name": f"{cfg.cluster_name}-cluster"},
    )

    aws.iam.RolePolicyAttachment(
        "eks-cluster-policy",
        role=cluster_role.name,
        policy_arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
    )

    node_role = aws.iam.Role(
        "eks-node-role",
        assume_role_policy=aws.iam.get_policy_document(
            statements=[
                {
                    "actions": ["sts:AssumeRole"],
                    "principals": [{"type": "Service", "identifiers": ["ec2.amazonaws.com"]}],
                }
            ]
        ).json,
        tags={"Name": f"{cfg.cluster_name}-nodes"},
    )

    node_policy_attachments = [
        aws.iam.RolePolicyAttachment(
            "eks-node-worker-policy",
            role=node_role.name,
            policy_arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
        ),
        aws.iam.RolePolicyAttachment(
            "eks-node-cni-policy",
            role=node_role.name,
            policy_arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
        ),
        aws.iam.RolePolicyAttachment(
            "eks-node-ecr-policy",
            role=node_role.name,
            policy_arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
        ),
    ]

    cluster = aws.eks.Cluster(
        "eks-cluster",
        name=cfg.cluster_name,
        role_arn=cluster_role.arn,
        version=cfg.kubernetes_version,
        vpc_config={
            "subnet_ids": [subnet.id for subnet in private_subnets],
            "endpoint_private_access": True,
            "endpoint_public_access": True,
            "public_access_cidrs": cfg.aws_endpoint_public_access_cidrs,
        },
        access_config={
            "authentication_mode": "API_AND_CONFIG_MAP",
            "bootstrap_cluster_creator_admin_permissions": True,
        },
        enabled_cluster_log_types=[
            "api",
            "audit",
            "authenticator",
            "controllerManager",
            "scheduler",
        ],
        tags={"Name": cfg.cluster_name},
        opts=pulumi.ResourceOptions(depends_on=[cluster_role]),
    )

    oidc_issuer = cluster.identities.apply(_eks_oidc_issuer)
    aws.iam.OpenIdConnectProvider(
        "eks-oidc-provider",
        url=oidc_issuer,
        client_id_lists=["sts.amazonaws.com"],
        thumbprint_lists=["9e99a48a9960b14926bb7f3b02e22da0ecd"],
    )

    node_group = aws.eks.NodeGroup(
        "eks-node-group",
        cluster_name=cluster.name,
        node_group_name="primary",
        node_role_arn=node_role.arn,
        subnet_ids=[subnet.id for subnet in private_subnets],
        version=cfg.kubernetes_version,
        ami_type="AL2023_x86_64_STANDARD",
        capacity_type="ON_DEMAND",
        disk_size=80,
        instance_types=[cfg.aws_node_instance_type],
        scaling_config={
            "desired_size": cfg.aws_node_min_count,
            "min_size": cfg.aws_node_min_count,
            "max_size": cfg.aws_node_max_count,
        },
        update_config={"max_unavailable": 1},
        tags={"Name": f"{cfg.cluster_name}-primary"},
        opts=pulumi.ResourceOptions(depends_on=node_policy_attachments + private_route_tables),
    )

    eks_depends_on: list[pulumi.Resource] = [cluster, node_group]

    if cfg.aws_gpu_node_group_enabled:
        gpu_node_group = aws.eks.NodeGroup(
            "eks-gpu-node-group",
            cluster_name=cluster.name,
            node_group_name="gpu",
            node_role_arn=node_role.arn,
            subnet_ids=[subnet.id for subnet in private_subnets],
            version=cfg.kubernetes_version,
            ami_type="AL2023_x86_64_NVIDIA",
            capacity_type="ON_DEMAND",
            disk_size=100,
            instance_types=[cfg.aws_gpu_instance_type],
            scaling_config={
                "desired_size": cfg.aws_gpu_node_min_count,
                "min_size": cfg.aws_gpu_node_min_count,
                "max_size": cfg.aws_gpu_node_max_count,
            },
            update_config={"max_unavailable": 1},
            taints=[
                {"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"},
            ],
            tags={"Name": f"{cfg.cluster_name}-gpu"},
            opts=pulumi.ResourceOptions(depends_on=node_policy_attachments + private_route_tables),
        )
        eks_depends_on.append(gpu_node_group)

    kubeconfig = pulumi.Output.all(cluster.name, cluster.endpoint, cluster.certificate_authority.data).apply(
        lambda args: _eks_kubeconfig(args[0], args[1], args[2], region)
    )

    provider = k8s.Provider(
        "eks-provider",
        kubeconfig=kubeconfig,
        opts=pulumi.ResourceOptions(depends_on=eks_depends_on),
    )

    if cfg.aws_gpu_node_group_enabled:
        device_plugin = k8s.yaml.v2.ConfigFile(
            "nvidia-device-plugin",
            file=cfg.nvidia_device_plugin_manifest_url,
            opts=pulumi.ResourceOptions(provider=provider, depends_on=eks_depends_on),
        )
        eks_depends_on.append(device_plugin)

    return KubernetesCluster(name=cluster.name, provider=provider, depends_on=eks_depends_on)
