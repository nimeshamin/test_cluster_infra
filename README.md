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

## Firecracker node pool (GCP, `firecracker` branch)

On this branch the `gcp` stack turns the GPU pool off, points both GitOps repos at their `firecracker` branches (`baseRepoRevision` / `appRepoRevision`), and adds a third GKE NodePool `firecracker` for running Firecracker microVMs:

- `n2-standard-4` (nested virtualization needs an Intel machine family; E2 is not supported), `UBUNTU_CONTAINERD` image, `advanced_machine_features.enable_nested_virtualization = true` so the node exposes `/dev/kvm`.
- Pinned to a single zone (`gcpFirecrackerZone`, default `<gcpLocation>-a`) so the regional pool is one node, not one per zone.
- The `primary` pool is also pinned to `us-central1-a` via `gcpNodeLocations` (2–4 nodes instead of 2–4 per zone); unset, GKE spreads it across every zone in the region.
- Labelled `firecracker=true` and tainted `firecracker=true:NoSchedule`; only the `firecracker-host` DaemonSet from `test_cluster_k8s_base` tolerates it.
- SMT disabled (`threads_per_core: 1`) so different tenants' vCPUs never share a physical core; an `n2-standard-4` then exposes 2 vCPUs.
- One raw local NVMe SSD (`gcpFirecrackerLocalSsdCount`, 375 GB, `local_nvme_ssd_block_config`) that firecracker-host formats as the XFS (reflink) image store; with 0 it falls back to a loop-mounted file on the boot disk. Changing this replaces the node pool, and local SSD contents do not survive node recreation.

| Key | Default | |
|---|---|---|
| `gcpFirecrackerNodePoolEnabled` | `false` (set `true` in `Pulumi.gcp.yaml` on this branch) | |
| `gcpFirecrackerMachineType` | `n2-standard-4` | |
| `gcpFirecrackerZone` | `<gcpLocation>-a` | |
| `gcpFirecrackerNodeMinCount` / `gcpFirecrackerNodeMaxCount` | `1` / `1` | |
| `gcpFirecrackerLocalSsdCount` | `0` (set `1` in `Pulumi.gcp.yaml` on this branch) | raw local NVMe SSDs for the image store |

### Bring-up script

`scripts/cluster-up.sh` runs the whole GCP bring-up and is safe to rerun on a live cluster:

1. Preflight: tools, `gcloud auth application-default login`, `pulumi login`, branch check.
2. `pulumi preview`; asks for confirmation if any infrastructure would change (the Kubernetes provider's token refresh is ignored), then `pulumi up`.
3. Regenerates `~/.kube/test-cluster-gcp.yaml` via `~/.kube/refresh-test-cluster-gcp.sh` (local, not in this repo).
4. Ensures the `fc-system/ghcr-pull` image pull secret. An existing secret is kept if it can still pull from GHCR; otherwise it prompts (hidden input) for a **classic** GitHub token with `read:packages`, checks it against GHCR, and writes the secret. Fine-grained `github_pat_` tokens are rejected because GHCR does not accept them. `GHCR_TOKEN` skips the prompt; `--rotate-secret` forces a new token.
5. Polls until all nodes, every Argo CD Application, `firecracker-host`, `fc-api` and `fc-agent` are ready (`--timeout`, default 1200s).

```bash
scripts/cluster-up.sh            # confirm before infrastructure changes
scripts/cluster-up.sh --yes      # unattended
```

### Tear-down script

`scripts/cluster-down.sh` destroys the stack without leaving billed disks behind. While the cluster is still reachable it records every PV's GCE disk, deletes the root Argo CD Applications and waits for Argo CD to cascade-delete everything (a bare `pulumi destroy` can hang on their finalizers), then deletes leftover PVCs (StatefulSet claims are not owned by Argo CD) and waits for the disks to be released. After `pulumi destroy` it checks through the Compute API that each recorded disk is gone and prints `gcloud compute disks delete` commands for any that are not. Only disks this cluster created are checked.

```bash
scripts/cluster-down.sh          # type the cluster name to confirm
scripts/cluster-down.sh --yes    # unattended
```

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
