"""Parsers for ``outdated`` output of each package manager."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class OutdatedPackage:
    name: str
    current: str | None
    latest: str | None
    wanted: str | None = None
    kind: str = ""

    @property
    def update_type(self) -> str:
        """major / minor / patch / unknown based on semantic version comparison."""
        cur, new = _nums(self.current), _nums(self.latest)
        if not cur or not new:
            return "unknown"
        if new[0] != cur[0]:
            return "major"
        if len(new) > 1 and len(cur) > 1 and new[1] != cur[1]:
            return "minor"
        return "patch"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"update_type": self.update_type}


def _nums(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    parts = re.findall(r"\d+", version.split("-", 1)[0])[:3]
    return tuple(int(p) for p in parts)


def parse_outdated(fmt: str, stdout: str) -> list[OutdatedPackage]:
    text = stdout.strip()
    if not text:
        return []
    if fmt == "pip-json":
        return [
            OutdatedPackage(d["name"], d.get("version"), d.get("latest_version"), kind=d.get("latest_filetype", ""))
            for d in json.loads(text)
        ]
    if fmt == "npm-json":
        data = json.loads(text)
        if isinstance(data, list):  # pnpm may emit a list
            return [
                OutdatedPackage(
                    d.get("packageName") or d.get("name"), d.get("current"), d.get("latest"), d.get("wanted")
                )
                for d in data
            ]
        packages = []
        for name, info in data.items():
            if isinstance(info, list):
                info = info[0] if info else {}
            packages.append(
                OutdatedPackage(
                    name, info.get("current"), info.get("latest"), info.get("wanted"), info.get("dependencyType", "")
                )
            )
        return packages
    if fmt == "yarn-json":
        packages = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "table":
                for row in obj.get("data", {}).get("body", []):
                    packages.append(OutdatedPackage(row[0], row[1], row[3], row[2], row[4] if len(row) > 4 else ""))
        return packages
    if fmt == "poetry-text":
        packages = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3 and re.match(r"\d", parts[1]):
                latest = parts[2] if re.match(r"\d", parts[2]) else parts[3] if len(parts) > 3 else None
                packages.append(OutdatedPackage(parts[0], parts[1], latest))
        return packages
    if fmt == "pub-json":
        data = json.loads(text)
        packages = []
        for pkg in data.get("packages", []):
            current = (pkg.get("current") or {}).get("version")
            latest = (pkg.get("latest") or {}).get("version")
            if current and latest and current != latest:
                packages.append(
                    OutdatedPackage(
                        pkg["package"],
                        current,
                        latest,
                        (pkg.get("upgradable") or {}).get("version"),
                        pkg.get("kind", ""),
                    )
                )
        return packages
    if fmt == "maven-text":
        packages = []
        for match in re.finditer(r"([\w.\-]+:[\w.\-]+)\s*\.+\s*(\S+)\s*->\s*(\S+)", text):
            packages.append(OutdatedPackage(match.group(1), match.group(2), match.group(3)))
        return packages
    if fmt == "go-json":
        packages = []
        decoder = json.JSONDecoder()
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            obj, index = decoder.raw_decode(text, index)
            update = obj.get("Update")
            if update and not obj.get("Main") and not obj.get("Indirect"):
                packages.append(OutdatedPackage(obj["Path"], obj.get("Version"), update.get("Version")))
        return packages
    raise ValueError(f"unknown outdated format {fmt}")
