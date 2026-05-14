from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pulumi
import pulumi_kubernetes as k8s


@dataclass(frozen=True)
class KubernetesCluster:
    name: pulumi.Input[str]
    provider: k8s.Provider
    depends_on: Sequence[pulumi.Resource]
