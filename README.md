# test-cluster

Pulumi Python infrastructure for creating a Kubernetes cluster on kind, GKE, or EKS and bootstrapping Argo CD into it. The cluster is left empty after bootstrap — Argo CD is then pointed at two GitOps repositories that own everything that runs on top:

- `platform-base`: [`test_cluster_k8s_base`](https://github.com/nimeshamin/test_cluster_k8s_base) — Istio, the observability stack, Kubeflow Pipelines, MLflow, namespaces.
- `application-services`: [`test_cluster_k8s_app`](https://github.com/nimeshamin/test_cluster_k8s_app) — the PPO runtime (Argo WorkflowTemplate + trigger RBAC).

## What this repo provisions

- The Kubernetes cluster itself (kind / GKE / EKS).
- A GPU pool on every target (toggle off per-stack — see [GPU support](#gpu-support)).
- The NVIDIA k8s device plugin DaemonSet on local and AWS (GKE handles this via its own driver-install DaemonSet).
- A Helm install of Argo CD into the cluster.
- The two root `Application` resources Argo CD uses to bootstrap the GitOps repos above.

Anything else running in the cluster — observability stack, Istio, KFP, MLflow, the PPO runtime — is owned by the GitOps repos. Chart versions for those apps live in their respective READMEs.

## Version defaults

- local/kind: Kubernetes `v1.34.0` (uses `kindest/node:v1.34.0`)
- AWS EKS: Kubernetes `1.35`
- GKE: Stable release channel by default; set `test-cluster:kubernetesVersion` to pin a specific patch
- Argo CD chart: `9.5.14`
- NVIDIA k8s-device-plugin chart (local + AWS targets only): `0.19.1`

## First run

```bash
cd test_cluster_infra
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

## Argo CD access

No ingress controller is installed. Use port-forwarding:

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

The root Argo CD Applications allow empty paths, so the placeholder repos can be pushed incrementally without blocking the cluster bootstrap.

## GPU support

All three stacks provision GPU capacity by default. Workloads requesting `nvidia.com/gpu: 1` schedule onto the GPU pool; the corresponding `nvidia.com/gpu=present:NoSchedule` toleration is set automatically by KFP v2 when a step calls `set_accelerator_type("nvidia.com/gpu")`.

| target | resource added                                                    | toggle off                                            |
|--------|-------------------------------------------------------------------|-------------------------------------------------------|
| local  | kind cluster with 2 nodes (control-plane handles CPU work; a dedicated worker labeled `nvidia.com/gpu=present` and tainted `nvidia.com/gpu=present:NoSchedule` runs GPU pods). Containerd inside the GPU node is reconfigured to use `nvidia-container-runtime`, plus the NVIDIA k8s device plugin DaemonSet. | `pulumi config set test-cluster:kindGpu false` |
| gcp    | second GKE NodePool `gpu` (`g2-standard-4` + `nvidia-l4`, autoscale 0→1, taint `nvidia.com/gpu=present:NoSchedule`, GKE-managed driver install) | `pulumi config set test-cluster:gcpGpuNodePoolEnabled false` |
| aws    | second EKS NodeGroup `gpu` (`g4dn.xlarge`, AMI `AL2023_x86_64_NVIDIA`, taint `nvidia.com/gpu=present:NoSchedule`) + NVIDIA device plugin DaemonSet | `pulumi config set test-cluster:awsGpuNodeGroupEnabled false` |

### Local prerequisites on WSL2

- Docker Desktop with WSL2 backend, **Kubernetes feature disabled** (we run kind, not DD's K8s).
- Docker's `default-runtime` set to `nvidia` (`docker info | grep -i 'Default Runtime'` should report `nvidia`). Configure via Docker Desktop → Settings → Docker Engine → daemon.json.
- Sanity check: `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` shows the GPU.
