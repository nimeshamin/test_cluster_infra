import pulumi

from infra.argocd import install_argocd
from infra.aws import create_eks_cluster
from infra.config import load_config
from infra.gcp import create_gke_cluster
from infra.local import create_minikube_cluster


cfg = load_config()

if cfg.target == "local":
    cluster = create_minikube_cluster(cfg)
elif cfg.target == "gcp":
    cluster = create_gke_cluster(cfg)
elif cfg.target == "aws":
    cluster = create_eks_cluster(cfg)
else:
    raise ValueError(f"Unsupported target {cfg.target!r}. Expected one of: local, gcp, aws.")

argocd = install_argocd(cfg, cluster.provider, depends_on=cluster.depends_on)

pulumi.export("target", cfg.target)
pulumi.export("clusterName", cluster.name)
pulumi.export("kubernetesVersion", cfg.kubernetes_version)
pulumi.export("argocdNamespace", cfg.argocd_namespace)
pulumi.export("argocdServer", "kubectl -n argocd port-forward svc/argocd-server 8080:80")
pulumi.export("argocdInitialAdminPassword", argocd.initial_admin_password_command)
