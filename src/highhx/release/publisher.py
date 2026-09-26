"""Publishing packages to registries (always CRITICAL risk)."""

from __future__ import annotations

from pathlib import Path

from highhx.project.detector import ProjectProfile
from highhx.utils.processes import which


def publish_command(root: Path, profile: ProjectProfile, configured: str | None) -> tuple[str | None, list[str]]:
    """Return (command, prerequisites problems)."""
    if configured:
        return configured, []
    problems: list[str] = []
    pm = {d.details.get("ecosystem"): d.name for d in profile.package_managers}
    if profile.primary == "python":
        dist = root / "dist"
        if not dist.is_dir() or not any(dist.iterdir()):
            problems.append("dist/ is empty — run `highhx build` or `highhx package` first")
        if pm.get("python") == "uv":
            return "uv publish", problems
        if pm.get("python") == "poetry":
            return "poetry publish", problems
        if which("twine") is None:
            problems.append("twine is not installed (pip install twine)")
        return "twine upload dist/*", problems
    if profile.primary in ("node", "react", "nextjs"):
        return f"{pm.get('node', 'npm')} publish", problems
    if profile.primary in ("flutter", "dart"):
        return "dart pub publish", problems
    if profile.primary == "java":
        return ("mvn deploy" if pm.get("java") == "maven" else "gradle publish"), problems
    if profile.primary == "rust":
        return "cargo publish", problems
    return None, ["no publish command known for this project; set release.publish_command"]
