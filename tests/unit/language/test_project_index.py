"""The project file index: metadata only, inside the project, never secrets, bounded, lazy."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from highhx.project import index as index_module
from highhx.project.index import FileQuery, ProjectFileIndex, kinds_of

DAY = 86_400.0
NOW = time.time()


def touch(root: Path, rel: str, *, age_days: float = 0.0, text: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    touch(tmp_path, "docs/architecture.md", age_days=1)
    touch(tmp_path, "docs/Architecture Overview.pdf", age_days=3)
    touch(tmp_path, "reports/q3-report.pdf", age_days=0.1)
    touch(tmp_path, "assets/logo.png", age_days=10)
    touch(tmp_path, "src/app.py")
    touch(tmp_path, ".env", text="TOKEN=secret")
    touch(tmp_path, "config/credentials.json")
    touch(tmp_path, "node_modules/pkg/readme.pdf")
    touch(tmp_path, ".git/objects/x.pdf")
    touch(tmp_path, ".highhx/state/run.pdf")
    return tmp_path


def paths(found: list) -> list[str]:
    return [f.path for f in found]


def test_it_sees_project_files_but_never_secrets_dependencies_or_state(project: Path) -> None:
    seen = set(paths(ProjectFileIndex(project).files()))
    assert {"docs/architecture.md", "reports/q3-report.pdf", "assets/logo.png", "src/app.py"} <= seen
    assert not seen & {".env", "config/credentials.json", "node_modules/pkg/readme.pdf", ".git/objects/x.pdf"}
    assert ".highhx/state/run.pdf" not in seen


def test_links_are_never_followed_out_of_the_project(project: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    touch(outside, "private.pdf")
    (project / "linked").symlink_to(outside, target_is_directory=True)
    (project / "file-link.pdf").symlink_to(outside / "private.pdf")
    seen = paths(ProjectFileIndex(project).files())
    assert not any("private" in p or p == "file-link.pdf" for p in seen)


def test_kinds_keywords_and_order(project: Path) -> None:
    index = ProjectFileIndex(project)
    assert paths(index.find(FileQuery(kinds=("pdf",)))) == ["reports/q3-report.pdf", "docs/Architecture Overview.pdf"]
    assert paths(index.find(FileQuery(kinds=("pdf",), sort="oldest")))[0] == "docs/Architecture Overview.pdf"
    # keywords ignore case, "-", "_" and spaces; a PDF is also a document
    assert paths(index.find(FileQuery(kinds=("document",), keywords=("architecture overview",)))) == [
        "docs/Architecture Overview.pdf"
    ]
    assert paths(index.find(FileQuery(keywords=("architecture",)))) == [
        "docs/architecture.md",
        "docs/Architecture Overview.pdf",
    ]
    assert paths(index.find(FileQuery(kinds=("image",), under="assets"))) == ["assets/logo.png"]
    assert index.find(FileQuery(kinds=("video",))) == []


def test_modification_windows(project: Path) -> None:
    index = ProjectFileIndex(project)
    recent = index.find(FileQuery(kinds=("pdf", "document"), modified_after=NOW - 2 * DAY))
    assert paths(recent) == ["reports/q3-report.pdf", "docs/architecture.md"]
    older = index.find(FileQuery(kinds=("pdf",), modified_before=NOW - 2 * DAY))
    assert paths(older) == ["docs/Architecture Overview.pdf"]


def test_lazy_cached_and_refreshable(project: Path) -> None:
    index = ProjectFileIndex(project, ttl=3600)
    assert index._files is None  # nothing is scanned until the first question
    first = len(index.files())
    touch(project, "docs/new.md")
    assert len(index.files()) == first  # cached
    index.refresh()
    assert len(index.files()) == first + 1


def test_a_huge_tree_is_indexed_partially(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index_module, "MAX_FILES", 2)
    index = ProjectFileIndex(project)
    assert len(index.files()) == 2 and index.partial


def test_entity_fields(project: Path) -> None:
    entity = next(f for f in ProjectFileIndex(project).files() if f.path == "docs/architecture.md")
    data = entity.to_dict()
    assert data["name"] == "architecture.md" and data["kind"] == "document" and data["size"] == 1
    assert entity.parent == "docs" and entity.extension == ".md"
    assert kinds_of("a/b.PDF") == ("pdf", "document")
