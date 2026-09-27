#!/usr/bin/env bash
# Bring up the GCP test cluster end to end:
#
#   1. preflight: tools, gcloud Application Default Credentials, Pulumi login,
#      infra repo on the firecracker branch
#   2. pulumi preview, confirm, pulumi up
#   3. regenerate the kubeconfig (the endpoint changes when the cluster is recreated)
#   4. ensure the ghcr-pull secret in fc-system, prompting for a GitHub token
#      (classic PAT, read:packages) if it is missing or rejected by GHCR
#   5. poll until the nodes, every Argo CD Application, firecracker-host,
#      fc-api and fc-agent are ready
#
# Nothing secret is stored in this repo: the token comes from a hidden prompt
# or GHCR_TOKEN, is checked against GHCR, and goes straight into the secret.
#
# Usage: scripts/cluster-up.sh [--yes] [--rotate-secret] [--timeout SECONDS]
#   --yes            skip the confirmation after the preview
#   --rotate-secret  replace ghcr-pull even if the current one works
#   --timeout        how long to wait for readiness after pulumi up (default 1200)
#
# Env overrides:
#   STACK               Pulumi stack (default: nimeshamin/test-cluster/gcp)
#   KUBECONFIG_FILE     kubeconfig to (re)generate (default: ~/.kube/test-cluster-gcp.yaml)
#   REFRESH_KUBECONFIG  script that writes it (default: ~/.kube/refresh-test-cluster-gcp.sh)
#   GHCR_USER           GitHub user owning the packages (default: nimeshamin)
#   GHCR_TOKEN          token to use instead of prompting
set -euo pipefail

cd "$(dirname "$0")/.."

STACK="${STACK:-nimeshamin/test-cluster/gcp}"
KUBECONFIG_FILE="${KUBECONFIG_FILE:-$HOME/.kube/test-cluster-gcp.yaml}"
REFRESH_KUBECONFIG="${REFRESH_KUBECONFIG:-$HOME/.kube/refresh-test-cluster-gcp.sh}"
GHCR_USER="${GHCR_USER:-nimeshamin}"
GHCR_IMAGE="nimeshamin/test-control-plane/fc-api"
FC_NAMESPACE=fc-system
SECRET=ghcr-pull

ASSUME_YES=0
ROTATE=0
TIMEOUT=1200
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --rotate-secret) ROTATE=1 ;;
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

# ---------------------------------------------------------------------------
step "Preflight"
for tool in pulumi kubectl gcloud curl python3; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done
gcloud auth application-default print-access-token >/dev/null 2>&1 \
  || die "gcloud Application Default Credentials are missing or expired: run 'gcloud auth application-default login'"
pulumi whoami >/dev/null 2>&1 || die "not logged in to Pulumi: run 'pulumi login'"
[[ -x "$REFRESH_KUBECONFIG" ]] || die "$REFRESH_KUBECONFIG not found (it writes $KUBECONFIG_FILE from the stack state)"
branch=$(git branch --show-current)
if [[ "$branch" != firecracker ]]; then
  info "WARNING: infra repo is on '$branch', not 'firecracker'; Pulumi.gcp.yaml on this branch will be applied."
fi
info "stack $STACK, branch $branch"

# ---------------------------------------------------------------------------
step "Pulumi preview"
preview_err=$(mktemp)
preview=$(pulumi preview --stack "$STACK" --non-interactive --json 2>"$preview_err") \
  || { cat "$preview_err"; rm -f "$preview_err"; die "pulumi preview failed"; }
rm -f "$preview_err"
# Resource changes other than the Kubernetes provider, whose embedded access
# token is minted fresh on every run and so always shows as an update.
changes=$(echo "$preview" | python3 -c '
import json, sys
steps = json.load(sys.stdin).get("steps", [])
for s in steps:
    op, urn = s.get("op"), s.get("urn", "")
    if op in ("same", "read") or "::pulumi:providers:kubernetes::" in urn:
        continue
    kind, name = urn.split("::")[-2:]
    print(f"{op:<8} {kind} {name}")
')
if [[ -n "$changes" ]]; then
  awk '{print "    " $0}' <<<"$changes"
  if [[ "$ASSUME_YES" != 1 ]]; then
    read -r -p "    Apply these changes? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || die "aborted"
  fi
  step "Pulumi up (creating the cluster from scratch takes ~15 minutes)"
else
  info "no infrastructure changes"
  step "Pulumi up (refreshes the Kubernetes provider credentials only)"
fi
up_log=$(mktemp)
set +e
pulumi up --stack "$STACK" --yes --non-interactive --skip-preview 2>&1 \
  | tee "$up_log" \
  | awk '/ (created|updated|deleted|replaced) \(|^Resources:|^ +[+~-] [0-9]+ |unchanged$|^Duration:|error/ {print "    " $0; fflush()}'
rc=${PIPESTATUS[0]}
set -e
if [[ "$rc" != 0 ]]; then
  tail -n 30 "$up_log" | sed 's/^/    /'
  die "pulumi up failed (full log: $up_log)"
fi
rm -f "$up_log"

# ---------------------------------------------------------------------------
step "Kubeconfig"
"$REFRESH_KUBECONFIG" | sed 's/^/    /'
k get --raw /readyz >/dev/null || die "cannot reach the cluster with $KUBECONFIG_FILE"

# ---------------------------------------------------------------------------
# Prints "ok", "denied" or "error" for a token's pull access to the fc-api image.
ghcr_check() {
  local token="$1" code body bearer
  body=$(mktemp)
  code=$(curl -s -o "$body" -w '%{http_code}' -u "$GHCR_USER:$token" \
    "https://ghcr.io/token?service=ghcr.io&scope=repository:$GHCR_IMAGE:pull") || { rm -f "$body"; echo error; return; }
  if [[ "$code" != 200 ]]; then rm -f "$body"; echo denied; return; fi
  bearer=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("token", ""))' "$body")
  rm -f "$body"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $bearer" \
    "https://ghcr.io/v2/$GHCR_IMAGE/tags/list") || { echo error; return; }
  [[ "$code" == 200 ]] && echo ok || echo denied
}

# Prints the token stored in the existing secret, or nothing.
existing_token() {
  k -n "$FC_NAMESPACE" get secret "$SECRET" -o jsonpath='{.data.\.dockerconfigjson}' 2>/dev/null \
    | base64 -d 2>/dev/null \
    | python3 -c 'import base64,json,sys
auths = json.load(sys.stdin).get("auths", {})
auth = auths.get("ghcr.io", {}).get("auth", "")
print(base64.b64decode(auth).decode().partition(":")[2])' 2>/dev/null || true
}

step "GHCR pull secret ($FC_NAMESPACE/$SECRET)"
k create namespace "$FC_NAMESPACE" --dry-run=client -o yaml | k apply -f - >/dev/null
need_secret=1
if [[ "$ROTATE" != 1 ]]; then
  current=$(existing_token)
  if [[ -n "$current" ]]; then
    case "$(ghcr_check "$current")" in
      ok) info "existing secret can pull from GHCR; keeping it"; need_secret=0 ;;
      denied) info "existing secret is rejected by GHCR (expired, revoked, fine-grained, or missing read:packages)" ;;
      *) info "could not reach GHCR to check the existing secret; keeping it"; need_secret=0 ;;
    esac
  else
    info "secret not found"
  fi
  unset current
fi

if [[ "$need_secret" == 1 ]]; then
  token="${GHCR_TOKEN:-}"
  for attempt in 1 2 3; do
    if [[ -z "$token" ]]; then
      [[ -t 0 ]] || die "no GHCR_TOKEN and no terminal to prompt on"
      info "Paste a GitHub classic personal access token with only the read:packages scope."
      info "(Fine-grained github_pat_ tokens are not accepted by GHCR.) Input is hidden."
      read -rs -p "    token: " token
      echo
    fi
    if [[ "$token" == github_pat_* ]]; then
      info "that is a fine-grained token; GHCR needs a classic (ghp_) token"
    else
      result=$(ghcr_check "$token")
      [[ "$result" == ok ]] && break
      info "GHCR rejected the token ($result)"
    fi
    token=""
    [[ -n "${GHCR_TOKEN:-}" || "$attempt" == 3 ]] && die "no working GHCR token"
  done
  k -n "$FC_NAMESPACE" create secret docker-registry "$SECRET" \
    --docker-server=ghcr.io --docker-username="$GHCR_USER" --docker-password="$token" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  unset token GHCR_TOKEN
  info "secret written"
  # Pods already in image-pull backoff would otherwise wait out the backoff.
  stuck=$(k -n "$FC_NAMESPACE" get pods --no-headers 2>/dev/null | awk '/ImagePull|ErrImage/ {print $1}')
  if [[ -n "$stuck" ]]; then
    info "restarting pods stuck pulling images: ${stuck//$'\n'/ }"
    echo "$stuck" | xargs kubectl --kubeconfig "$KUBECONFIG_FILE" -n "$FC_NAMESPACE" delete pod --wait=false >/dev/null
  fi
fi

# ---------------------------------------------------------------------------
# Each check prints one status line and returns 0 when ready.
check_nodes() {
  local total ready
  total=$(k get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
  ready=$(k get nodes --no-headers 2>/dev/null | awk '$2 == "Ready"' | wc -l | tr -d ' ')
  echo "nodes: $ready/$total Ready"
  [[ "$total" -gt 0 && "$ready" == "$total" ]]
}
check_apps() {
  local apps bad
  apps=$(k -n argocd get applications -o jsonpath='{range .items[*]}{.metadata.name} {.status.sync.status} {.status.health.status}{"\n"}{end}' 2>/dev/null)
  [[ -n "$apps" ]] || { echo "argo cd: no applications yet"; return 1; }
  bad=$(echo "$apps" | awk '$2 != "Synced" || $3 != "Healthy" {print $1 "=" $2 "/" $3}')
  echo "argo cd: $(echo "$apps" | wc -l | tr -d ' ') apps${bad:+, waiting on: ${bad//$'\n'/ }}"
  [[ -z "$bad" ]]
}
check_ready() {  # namespace selector label
  local out
  out=$(k -n "$1" get pods -l "$2" -o jsonpath='{range .items[*]}{.status.containerStatuses[0].ready}{" "}{end}' 2>/dev/null)
  echo "$3: ${out:-no pods}"
  [[ -n "$out" && "$out" != *false* ]]
}

step "Waiting for the cluster to become ready (timeout ${TIMEOUT}s)"
deadline=$(( $(date +%s) + TIMEOUT ))
last=""
while :; do
  status=""
  all=1
  for c in check_nodes check_apps \
    "check_ready firecracker app.kubernetes.io/name=firecracker-host firecracker-host" \
    "check_ready $FC_NAMESPACE app.kubernetes.io/name=fc-api fc-api" \
    "check_ready $FC_NAMESPACE app.kubernetes.io/name=fc-agent fc-agent"; do
    line=$($c) || all=0
    status+="$line"$'\n'
  done
  if pull=$(k -n "$FC_NAMESPACE" get pods --no-headers 2>/dev/null | awk '/ImagePull|ErrImage/ {print $1}') && [[ -n "$pull" ]]; then
    status+="image pulls failing in $FC_NAMESPACE: ${pull//$'\n'/ } (rerun with --rotate-secret if the token is the problem)"$'\n'
  fi
  if [[ "$status" != "$last" ]]; then
    printf '%s' "$status" | sed 's/^/    /'
    echo "    --"
    last="$status"
  fi
  [[ "$all" == 1 ]] && break
  (( $(date +%s) < deadline )) || die "timed out waiting for readiness"
  sleep 15
done

step "Cluster is up"
info "export KUBECONFIG=$KUBECONFIG_FILE"
info "Firecracker host checks:  ../test_cluster_k8s_base/scripts/firecracker-check.sh && ../test_cluster_k8s_base/scripts/firecracker-smoke.sh"
info "Control plane e2e:        ../test_control_plane/scripts/e2e.sh"
info "Tear down:                pulumi destroy --stack $STACK"
