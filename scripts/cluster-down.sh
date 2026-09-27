#!/usr/bin/env bash
# Tear down the GCP test cluster without leaving billed resources behind.
#
# A bare `pulumi destroy` has two problems here:
#   - GKE does not delete persistent disks created for PVCs when the cluster
#     is deleted, so Prometheus/Loki/Tempo/Pyroscope/fc-api disks would be
#     orphaned and keep billing.
#   - The root Argo CD Applications carry cascade-delete finalizers; if Argo CD
#     is removed before they are processed, the destroy hangs on them.
#
# So, while the cluster is still reachable:
#   1. record every PV's GCE disk
#   2. delete the root Applications and wait for Argo CD to cascade-delete
#      every app and its resources
#   3. delete the PVCs that remain (StatefulSet volume claims are not owned by
#      Argo CD) and wait for their PVs, and so their disks, to be released
# then:
#   4. pulumi destroy
#   5. check through the Compute API that every recorded disk is gone. Only
#      disks this cluster created are checked; nothing else in the project is
#      touched.
#
# Firecracker VMs and their disks, the fc-api state and the ghcr-pull secret
# are lost. scripts/cluster-up.sh recreates everything (and prompts for a
# GHCR token again).
#
# Usage: scripts/cluster-down.sh [--yes] [--timeout SECONDS]
#   --yes       skip the typed confirmation
#   --timeout   how long to wait for each Kubernetes cleanup phase (default 600)
#
# Env overrides:
#   STACK               Pulumi stack (default: nimeshamin/test-cluster/gcp)
#   KUBECONFIG_FILE     kubeconfig (default: ~/.kube/test-cluster-gcp.yaml)
#   REFRESH_KUBECONFIG  script that writes it (default: ~/.kube/refresh-test-cluster-gcp.sh)
set -euo pipefail

cd "$(dirname "$0")/.."

STACK="${STACK:-nimeshamin/test-cluster/gcp}"
KUBECONFIG_FILE="${KUBECONFIG_FILE:-$HOME/.kube/test-cluster-gcp.yaml}"
REFRESH_KUBECONFIG="${REFRESH_KUBECONFIG:-$HOME/.kube/refresh-test-cluster-gcp.sh}"

ASSUME_YES=0
TIMEOUT=600
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --timeout) TIMEOUT="$2"; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
k() { KUBECONFIG="$KUBECONFIG_FILE" kubectl "$@"; }

# wait_until DESCRIPTION COMMAND...: polls every 10s until COMMAND prints
# nothing, printing what is left whenever it changes. Returns 1 on timeout.
wait_until() {
  local desc="$1" deadline last="" left
  shift
  deadline=$(( $(date +%s) + TIMEOUT ))
  while :; do
    left=$("$@" 2>/dev/null || true)
    [[ -z "$left" ]] && { info "$desc: done"; return 0; }
    if [[ "$left" != "$last" ]]; then
      info "$desc: waiting on ${left//$'\n'/ }"
      last="$left"
    fi
    (( $(date +%s) < deadline )) || { info "$desc: timed out"; return 1; }
    sleep 10
  done
}

# ---------------------------------------------------------------------------
step "Preflight"
for tool in pulumi kubectl gcloud curl python3; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done
gcloud auth application-default print-access-token >/dev/null 2>&1 \
  || die "gcloud Application Default Credentials are missing or expired: run 'gcloud auth application-default login'"
pulumi whoami >/dev/null 2>&1 || die "not logged in to Pulumi: run 'pulumi login'"

state=$(pulumi stack export --stack "$STACK")
summary=$(echo "$state" | python3 -c '
import collections, json, sys
res = json.load(sys.stdin)["deployment"].get("resources") or []
res = [r for r in res if not r["type"].startswith("pulumi:")]
cluster = next((r["outputs"].get("name", "") for r in res if r["type"] == "gcp:container/cluster:Cluster"), "")
print(cluster)
for t, n in sorted(collections.Counter(r["type"] for r in res).items()):
    print(f"{n:>3}  {t}")
')
cluster=$(head -n1 <<<"$summary")
resources=$(tail -n +2 <<<"$summary")
if [[ -z "$resources" ]]; then
  info "stack $STACK has no resources; nothing to destroy"
  exit 0
fi
info "stack $STACK${cluster:+, cluster $cluster}:"
awk '{print "    " $0}' <<<"$resources"

# ---------------------------------------------------------------------------
step "Confirm"
info "This destroys the cluster, every Firecracker VM and disk, the fc-api state,"
info "all Prometheus/Loki/Tempo/Pyroscope data and the ghcr-pull secret."
if [[ "$ASSUME_YES" != 1 ]]; then
  [[ -t 0 ]] || die "no terminal to confirm on; pass --yes"
  expect="${cluster:-destroy}"
  read -r -p "    Type '$expect' to continue: " answer
  [[ "$answer" == "$expect" ]] || die "aborted"
fi

# ---------------------------------------------------------------------------
disks=""
reachable=0
if [[ -n "$cluster" && -x "$REFRESH_KUBECONFIG" ]] \
  && "$REFRESH_KUBECONFIG" >/dev/null 2>&1 \
  && k get --raw /readyz >/dev/null 2>&1; then
  reachable=1
fi

if [[ "$reachable" == 1 ]]; then
  step "Recording persistent disks"
  disks=$(k get pv -o jsonpath='{range .items[*]}{.spec.csi.volumeHandle}{"\n"}{end}' | sed '/^$/d')
  if [[ -n "$disks" ]]; then
    awk '{print "    " $0}' <<<"$disks"
  else
    info "none"
  fi

  step "Deleting Argo CD applications (cascade)"
  roots=$(k -n argocd get applications -o name 2>/dev/null | grep -E '/(gcp|local|aws)-' || true)
  if [[ -n "$roots" ]]; then
    # --wait=false: the finalizers are processed by Argo CD, polled below.
    echo "$roots" | xargs kubectl --kubeconfig "$KUBECONFIG_FILE" -n argocd delete --wait=false >/dev/null
  fi
  apps_left() { k -n argocd get applications -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'; }
  if ! wait_until "applications" apps_left; then
    info "WARNING: some applications did not finish deleting; continuing anyway"
  fi

  step "Deleting remaining PVCs and waiting for their disks to be released"
  k delete pvc --all-namespaces --all --wait=false >/dev/null 2>&1 || true
  pvs_left() { k get pv -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'; }
  if ! wait_until "persistent volumes" pvs_left; then
    info "WARNING: some volumes were not released; their disks are checked after the destroy"
  fi
else
  step "Kubernetes cleanup"
  info "cluster is not reachable; skipping (orphaned disks cannot be discovered without it)"
fi

# ---------------------------------------------------------------------------
step "Pulumi destroy"
destroy_log=$(mktemp)
set +e
pulumi destroy --stack "$STACK" --yes --non-interactive --skip-preview 2>&1 \
  | tee "$destroy_log" \
  | awk '/ (deleted|deleting failed) \(|^Resources:|^ +- [0-9]+ |^Duration:|error/ {print "    " $0; fflush()}'
rc=${PIPESTATUS[0]}
set -e
if [[ "$rc" != 0 ]]; then
  tail -n 30 "$destroy_log" | sed 's/^/    /'
  die "pulumi destroy failed (full log: $destroy_log); rerun this script to retry"
fi
rm -f "$destroy_log"

# ---------------------------------------------------------------------------
if [[ -n "$disks" ]]; then
  step "Checking for orphaned disks"
  token=$(gcloud auth application-default print-access-token)
  orphans=""
  while read -r handle; do
    [[ -z "$handle" ]] && continue
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" \
      "https://compute.googleapis.com/compute/v1/$handle")
    case "$code" in
      404) info "gone     $handle" ;;
      200) info "EXISTS   $handle"; orphans+="$handle"$'\n' ;;
      *) info "unknown  $handle (HTTP $code)" ;;
    esac
  done <<<"$disks"
  if [[ -n "$orphans" ]]; then
    info ""
    info "These disks outlived the cluster and are still billed. Delete them with:"
    while read -r handle; do
      [[ -z "$handle" ]] && continue
      # projects/P/zones/Z/disks/D
      IFS=/ read -r _ project _ zone _ disk <<<"$handle"
      info "  gcloud compute disks delete $disk --zone $zone --project $project --quiet"
    done <<<"$orphans"
    exit 1
  fi
fi

step "Cluster is down"
info "$KUBECONFIG_FILE is now stale; scripts/cluster-up.sh regenerates it."
