"""Container and orchestration detection."""

from __future__ import annotations

from pathlib import Path

from highhx.detection import Detection

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def find_compose_files(root: Path) -> list[Path]:
    return [root / name for name in COMPOSE_FILES if (root / name).is_file()]


def detect_containers(root: Path) -> list[Detection]:
    found: list[Detection] = []
    dockerfiles = sorted(p.name for p in root.glob("Dockerfile*") if p.is_file())
    if dockerfiles:
        found.append(Detection("dockerfile", "container", dockerfiles))
    compose = find_compose_files(root)
    if compose:
        found.append(
            Detection(
                "docker-compose", "container", [p.name for p in compose], details={"files": [p.name for p in compose]}
            )
        )
    if (root / ".devcontainer").is_dir() or (root / ".devcontainer.json").is_file():
        found.append(Detection("devcontainer", "container", [".devcontainer"]))
    k8s_evidence = [d for d in ("k8s", "kubernetes", "manifests", "deploy/k8s") if (root / d).is_dir()]
    if (root / "kustomization.yaml").is_file():
        k8s_evidence.append("kustomization.yaml")
    if k8s_evidence:
        found.append(Detection("kubernetes", "container", k8s_evidence))
    charts = [p.parent.name for p in root.glob("**/Chart.yaml") if "node_modules" not in p.parts][:5]
    if charts:
        found.append(Detection("helm", "container", [f"chart {c}" for c in charts]))
    tf = sorted(p.name for p in root.glob("*.tf"))
    if tf or (root / "terraform").is_dir():
        found.append(Detection("terraform", "container", tf[:3] or ["terraform/"]))
    return found
