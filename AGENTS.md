# AGENTS.md

## Repository Scope

This repository is for infrastructure orchestration only. Use it for Pulumi code that creates or connects to Kubernetes clusters and installs the minimum bootstrap layer needed for GitOps.

## Boundaries

- Only install Argo CD from this repository.
- Do not install Istio, observability tools, application services, or one-off Kubernetes workloads here.
- Do not add Helm releases for platform services here beyond Argo CD.
- Do not place Kubernetes manifests or application charts in this repository unless they are strictly required to bootstrap Argo CD itself.

## Expected Use

- Manage local minikube, GCP, and AWS cluster infrastructure.
- Configure Kubernetes providers and cloud provider resources.
- Install and configure Argo CD so it can read from the GitOps repositories.
- Wire Argo CD to the base and app repositories using Applications or App-of-Apps bootstrap resources.

## Package Management

Use `uv` for Python dependency management. Do not introduce another Python package manager.

## Validation

Before handing off changes, run the relevant Pulumi and Python checks for the target being changed, such as:

```bash
uv run python -m compileall -q .
pulumi preview --stack nimeshamin/test-cluster/local
```
