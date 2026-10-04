"""Trajectory search: normalized terms and UI synonyms, trigrams for morphology, structured
matches (site, app, labels, surface), filters, outcome preference, determinism, and an injected
embedding model."""

from __future__ import annotations

from pathlib import Path

from highhx.trajectories import Trajectory, TrajectoryStep, TrajectoryStore
from highhx.trajectories.search import TrajectoryIndex, stem, terms


def task(
    goal: str,
    *,
    surface: str = "browser",
    status: str = "completed",
    url: str = "",
    app: str = "",
    labels: tuple[str, ...] = (),
    started: float = 0.0,
) -> Trajectory:
    t = Trajectory(goal, surface, status=status, started=started)
    for i, label in enumerate(labels or ("",), 1):
        t.add(
            TrajectoryStep(
                i,
                f"{goal} step {i}",
                {"action_type": "browser.click", "target": {"label": label}},
                {"outcome": "success"},
                {"url": url, "app": app},
            )
        )
    return t


CORPUS = [
    task("export the invoices as csv", url="https://billing.acme.test/invoices", labels=("Export",), started=1),
    task("change the wallpaper", surface="desktop", app="System Settings", labels=("Wallpaper",), started=2),
    task("sign in to the dashboard", url="https://app.acme.test/login", labels=("Sign in",), started=3),
    task("delete the draft email", url="https://mail.test/drafts", labels=("Delete",), status="failed", started=4),
    task("remove the old drafts", url="https://mail.test/drafts", labels=("Remove",), started=5),
]


def ids(results: list) -> list[str]:
    return [r.trajectory.task for r in results]


def test_terms_are_normalized() -> None:
    assert stem("invoices") == "invoice" and stem("exporting") == "export" and stem("companies") == "company"
    assert stem("boxes") == "box" and stem("address") == "address"
    assert stem("settings") == stem("setting") == stem("set") and stem("exported") == stem("export")
    assert terms("Download the Invoices") == terms("export invoice")
    assert terms("log in please") == terms("Sign in")


def test_synonyms_and_morphology_find_what_words_alone_miss() -> None:
    index = TrajectoryIndex(CORPUS)
    assert ids(index.search("download the invoice"))[0] == "export the invoices as csv"
    assert ids(index.search("log in to the dashboard"))[0] == "sign in to the dashboard"
    assert ids(index.search("invoise export"))[0] == "export the invoices as csv"  # a typo


def test_completed_tasks_rank_above_failed_ones_and_structure_counts() -> None:
    index = TrajectoryIndex(CORPUS)
    found = index.search("delete the drafts")
    assert set(ids(found)[:2]) == {"remove the old drafts", "delete the draft email"}  # "remove" means "delete"
    twins = TrajectoryIndex(
        [task("archive the report", status="failed", started=9), task("archive the report", started=1)]
    )
    assert [r.trajectory.status for r in twins.search("archive the report")] == [
        "completed",
        "failed",
    ]  # equally similar: completed first
    by_app = index.search("wallpaper in system settings")
    assert ids(by_app)[0] == "change the wallpaper" and by_app[0].parts["structure"] > 0
    by_site = index.search("billing export")
    assert ids(by_site)[0] == "export the invoices as csv" and by_site[0].parts["structure"] > 0


def test_filters_and_no_spurious_matches() -> None:
    index = TrajectoryIndex(CORPUS)
    assert ids(index.search("drafts", host="mail.test")) == ["remove the old drafts", "delete the draft email"]
    assert ids(index.search("drafts", status="failed")) == ["delete the draft email"]
    assert index.search("wallpaper", surface="browser") == []
    assert index.search("quarterly tax report for the accountant") == []
    assert index.search("") == []


def test_results_are_deterministic() -> None:
    first = [r.to_dict() for r in TrajectoryIndex(CORPUS).search("drafts", limit=5)]
    for _ in range(3):
        assert [r.to_dict() for r in TrajectoryIndex(list(reversed(CORPUS))).search("drafts", limit=5)] == first


def test_an_embedding_model_can_replace_the_term_vectors() -> None:
    class OneHot:
        name, dimensions = "fake", 2

        def embed(self, texts):  # type: ignore[no-untyped-def]
            return [[1.0, 0.0] if "wallpaper" in t.lower() or "background" in t.lower() else [0.0, 1.0] for t in texts]

    found = TrajectoryIndex(CORPUS, embedding=OneHot()).search("desktop background")
    assert ids(found)[0] == "change the wallpaper" and found[0].parts["semantic"] == 1.0


def test_the_store_searches_saved_trajectories(tmp_path: Path) -> None:
    store = TrajectoryStore(tmp_path)
    for t in CORPUS:
        store.save(t)
    hits = store.search("download invoices")
    assert hits and hits[0].trajectory.task == "export the invoices as csv"
    assert [h.trajectory.task for h in store.search("drafts", host="mail.test", status="completed")] == [
        "remove the old drafts"
    ]
