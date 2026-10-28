"""What a script may hold: nothing that leaves the transaction the Job opens, or talks to psql."""

from __future__ import annotations

import pytest

from databases_ui.sql import MAX_BYTES, problems


@pytest.mark.parametrize(
    "script",
    [
        "insert into menu (code, label) values ('A', 'Results');\nupdate menu set label = 'B' where code = 'A';",
        # Without a last semicolon, as people paste them.
        "alter table orders add column note text",
        # Words that end a transaction, where they end nothing.
        "create function f() returns void language plpgsql as $$ begin commit; end $$;",
        "create function g() returns void language plpgsql as $body$ begin rollback; end $body$;",
        "insert into log values ('commit;'), ('begin; end;');",
        "insert into log values (E'it\\'s; commit;');",
        'select "commit" from "end";',
        "-- begin;\nselect 1; /* commit; /* nested; */ rollback; */ select 2;",
        "create function h() returns int language sql begin atomic select 1; select case when true then 2 end; end;",
        "prepare q as select 1; execute q;",
        "copy orders to '/tmp/orders.csv';",
        "select $1::int;",
        "insert into t values ('a\\b');",
    ],
)
def test_ordinary_scripts_pass(script: str) -> None:
    assert problems(script) == []


@pytest.mark.parametrize(
    ("script", "said"),
    [
        ("begin;\ninsert into t values (1);\ncommit;", ["Line 1: BEGIN.", "Line 3: COMMIT."]),
        ("insert into t values (1);\n\nROLLBACK;", ["Line 3: ROLLBACK."]),
        ("start transaction; select 1;", ["Line 1: START."]),
        ("select 1;\nend;", ["Line 2: END."]),
        ("savepoint a; release a;", ["Line 1: SAVEPOINT.", "Line 1: RELEASE."]),
        ("prepare transaction 'x';", ["Line 1: PREPARE."]),
        ("select 1;\n\\! id", ["Line 2: a backslash"]),
        ("select 1 \\gset", ["Line 1: a backslash"]),
        ("insert into t values ('x');\n\\connect other", ["Line 2: a backslash"]),
        ("copy t from stdin;\n1\n\\.", ["Line 1: COPY", "Line 3: a backslash"]),
        ("/* a\ncomment */\ncommit;", ["Line 3: COMMIT."]),
        ("insert into t values ('two\nlines');\ncommit;", ["Line 3: COMMIT."]),
    ],
)
def test_what_escapes_the_transaction_is_refused_with_its_line(script: str, said: list[str]) -> None:
    found = problems(script)
    assert len(found) == len(said), found
    for line, start in zip(found, said, strict=True):
        assert line.startswith(start), (line, start)


def test_empty_huge_or_binary_scripts_are_refused() -> None:
    assert problems("  \n ") == ["The script is empty."]
    assert problems("select 1;" + " " * MAX_BYTES)[0].startswith("The script is over")
    assert problems("select 1;\x00") == ["The script holds a NUL character."]
