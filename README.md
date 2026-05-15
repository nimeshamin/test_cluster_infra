# test-cluster

Pulumi Python infrastructure for creating a Kubernetes cluster on minikube, GKE, or EKS and bootstrapping Argo CD into it. Argo CD is configured to read from two GitOps repositories:

- `platform-base`: `test_cluster_k8s_base`
- `application-services`: `test_cluster_k8s_app`

## Version defaults

The defaults are intentionally provider-aware:

- local/minikube: Kubernetes `v1.34.0`
- AWS EKS: Kubernetes `1.35`
- GKE: Stable release channel by default; set `test-cluster:kubernetesVersion` to pin a specific GKE patch
- Argo CD chart: `9.5.14`
- Grafana chart: `10.5.15`
- Prometheus chart: `29.6.0`
- Alloy chart: `1.8.1`
- Tempo chart: `1.24.4`
- Loki chart: `7.0.0`
- Pyroscope chart: `2.0.1`

As of this local setup, minikube `v1.37.0` reports `v1.34.0` as its newest supported Kubernetes version. EKS is pinned to `1.35`, and GKE should be managed through the Stable channel unless you need a deterministic patch pin.

## First run

```bash
cd /Users/nimesh/Source/test_cluster
uv sync

pulumi stack init nimeshamin/local
pulumi preview --stack nimeshamin/local
pulumi up --stack nimeshamin/local
```

For cloud targets:

```bash
pulumi stack init nimeshamin/gcp
pulumi config set gcp:project <project-id>
pulumi config set test-cluster:gcpMasterAuthorizedCidrBlocks '[{"name":"home","cidrBlock":"YOUR_IP/32"}]' --path
pulumi up --stack nimeshamin/gcp

pulumi stack init nimeshamin/aws
pulumi config set aws:region us-west-2
pulumi config set test-cluster:awsEndpointPublicAccessCidrs '["YOUR_IP/32"]' --path
pulumi up --stack nimeshamin/aws
```

The sample stack files use `0.0.0.0/0` for initial usability. Replace it with your current admin IP before creating cloud clusters.

The local stack requests `7168MB` from Docker Desktop by default. Increase `test-cluster:minikubeMemory` only after raising Docker Desktop's memory limit.

## Argo CD access

No ingress controller is installed. Use port-forwarding:

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

The root Argo CD Applications allow empty paths, so the placeholder repos can be pushed incrementally without blocking the cluster bootstrap.

## GPU support

All three stacks provision GPU capacity by default so the KFP PPO trainer can request `nvidia.com/gpu: 1`.

| target | resource added                                                    | toggle off                                            |
|--------|-------------------------------------------------------------------|-------------------------------------------------------|
| local  | minikube with `--driver=docker --container-runtime=docker --gpus=all`, NVIDIA device plugin DaemonSet | `pulumi config set test-cluster:minikubeGpu false`    |
| gcp    | second GKE NodePool `gpu` (`g2-standard-4` + `nvidia-l4`, autoscale 0→1, taint `nvidia.com/gpu=present:NoSchedule`, GKE-managed driver install) | `pulumi config set test-cluster:gcpGpuNodePoolEnabled false` |
| aws    | second EKS NodeGroup `gpu` (`g4dn.xlarge`, AMI `AL2023_x86_64_NVIDIA`, taint `nvidia.com/gpu=present:NoSchedule`) + NVIDIA device plugin DaemonSet | `pulumi config set test-cluster:awsGpuNodeGroupEnabled false` |

Local prerequisites on WSL2:

- Docker Desktop with WSL2 backend.
- NVIDIA Container Toolkit installed on the host.
- Sanity check: `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` shows the GPU.

KFP v2 sets the matching toleration automatically when a step calls
`set_accelerator_type("nvidia.com/gpu")`, so GPU steps schedule onto the
tainted GPU pool without extra wiring.
