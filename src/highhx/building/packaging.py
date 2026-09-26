"""Packaging helpers: pick the packaging command per ecosystem."""

from __future__ import annotations

from highhx.project.detector import ProjectProfile


def default_package_command(profile: ProjectProfile) -> str | None:
    """Ecosystem default when neither commands.package nor commands.build is configured."""
    pm = {d.details.get("ecosystem"): d.name for d in profile.package_managers}
    if profile.primary == "python":
        return {"uv": "uv build", "poetry": "poetry build", "pdm": "pdm build"}.get(
            pm.get("python", ""), "python -m build"
        )
    if profile.primary in ("node", "react", "nextjs"):
        return f"{pm.get('node', 'npm')} pack"
    if profile.primary == "java":
        return profile.commands.get("package")
    if profile.primary == "rust":
        return "cargo package"
    if profile.primary == "docker" or profile.has("dockerfile"):
        return f"docker build -t {profile.name.lower()}:{profile.version or 'latest'} ."
    return None
