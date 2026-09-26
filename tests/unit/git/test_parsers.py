from highhx.git.branches import FIELD as BF
from highhx.git.branches import parse_branches
from highhx.git.commits import parse_conventional, validate_message
from highhx.git.diff import parse_numstat
from highhx.git.history import FIELD, RECORD, parse_log
from highhx.git.status import parse_status_v2
from highhx.git.tags import Tag, latest_version_tag


def test_status_v2_parsing() -> None:
    text = "\0".join(
        [
            "# branch.oid abc123",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -1",
            "1 M. N... 100644 100644 100644 aaa bbb src/app.py",
            "1 .M N... 100644 100644 100644 aaa bbb README.md",
            "2 R. N... 100644 100644 100644 aaa bbb R100 new.py\told.py",
            "u UU N... 100644 100644 100644 100644 a b c conflict.txt",
            "? notes.txt",
        ]
    )
    status = parse_status_v2(text)
    assert (status.branch, status.upstream, status.ahead, status.behind) == ("main", "origin/main", 2, 1)
    assert [f.path for f in status.staged] == ["src/app.py", "new.py"]
    assert status.staged[1].original == "old.py"
    assert [f.path for f in status.unstaged] == ["README.md"]
    assert status.untracked == ["notes.txt"] and status.conflicted == ["conflict.txt"]
    assert not status.clean and status.change_count == 5


def test_log_parsing_and_conventional_commits() -> None:
    record = (
        FIELD.join(
            ["a" * 40, "aaaaaaa", "Ann", "ann@x", "2026-01-01T00:00:00+00:00", "feat(api)!: new endpoint", "body"]
        )
        + RECORD
    )
    record2 = FIELD.join(["b" * 40, "bbbbbbb", "Bob", "b@x", "2026-01-02T00:00:00+00:00", "Fix typo", ""]) + RECORD
    commits = parse_log(record + "\n" + record2)
    assert [c.short for c in commits] == ["aaaaaaa", "bbbbbbb"]
    cc = commits[0].conventional
    assert cc is not None and (cc.type, cc.scope, cc.breaking) == ("feat", "api", True)
    assert commits[1].conventional is None
    assert parse_conventional("fix: x", "BREAKING CHANGE: y").breaking  # type: ignore[union-attr]


def test_commit_message_validation() -> None:
    assert validate_message("") == ["commit message is empty"]
    assert validate_message("feat: ok", conventional=True) == []
    assert validate_message("did stuff", conventional=True)
    assert validate_message("wip: x", conventional=True)


def test_numstat_branches_tags() -> None:
    changes = parse_numstat("3\t1\tsrc/a.py\n-\t-\tlogo.png\n")
    assert changes[0].added == 3 and changes[1].binary
    branches = parse_branches(BF.join(["main", "origin/main", "abc", "2026-01-01", "*", "[ahead 1]"]))
    assert branches[0].current and branches[0].track == "[ahead 1]"
    tags = [Tag("v1.2.0", "", ""), Tag("v1.10.0", "", ""), Tag("v1.3.0-rc.1", "", ""), Tag("other", "", "")]
    assert latest_version_tag(tags).name == "v1.10.0"  # type: ignore[union-attr]
