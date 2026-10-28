"""What a script may hold. A Job runs it in one transaction it opens and closes itself, through
psql, so a script that commits on its own or talks to psql would escape that.

`problems` walks the script as PostgreSQL's lexer would: quoted strings, quoted names,
dollar-quoted bodies and comments are skipped, so a function body that says COMMIT is fine and a
top-level COMMIT is not. Outside those, a statement may not open, close or mark a transaction
(BEGIN, START, COMMIT, END, ROLLBACK, ABORT, SAVEPOINT, RELEASE, PREPARE TRANSACTION), and a
backslash is a psql command (\\! runs a shell, \\connect another database, \\o writes a file), which
nothing here runs. COPY from STDIN or to STDOUT is refused too: its data would be read from the
script itself.
"""

from __future__ import annotations

import re

MAX_BYTES = 100_000
_TRANSACTION = {"begin", "start", "commit", "end", "rollback", "abort", "savepoint", "release"}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _atomic(words: list[str]) -> bool:
    """Whether a CREATE FUNCTION or PROCEDURE is inside an unfinished BEGIN ATOMIC body."""
    if words[0] != "create":
        return False
    depth = 0
    for before, word in zip(["", *words], words, strict=False):
        if before == "begin" and word == "atomic":
            depth += 1
        elif depth and word == "case":
            depth += 1
        elif depth and word == "end":
            depth -= 1
    return depth > 0


def _statements(script: str) -> tuple[list[tuple[int, list[str]]], list[int]]:
    """Each top-level statement as (the line it starts on, its words in lower case), and the lines
    of any backslash outside quotes and comments."""
    statements: list[tuple[int, list[str]]] = []
    backslashes: list[int] = []
    words: list[str] = []
    start = 0
    line = 1
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif script.startswith("--", i):
            end = script.find("\n", i)
            i = n if end < 0 else end
        elif script.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if script.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif script.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    line += script[i] == "\n"
                    i += 1
        elif c == "'":
            # E'...' takes backslash escapes; a standard string only doubles its quote.
            escapes = i > 0 and script[i - 1] in "eE" and (i < 2 or not (script[i - 2].isalnum() or script[i - 2] == "_"))
            i += 1
            while i < n:
                if escapes and script[i] == "\\":
                    line += script[i + 1 : i + 2] == "\n"
                    i += 2
                elif script[i] == "'":
                    if script.startswith("''", i):
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    line += script[i] == "\n"
                    i += 1
        elif c == '"':
            end = script.find('"', i + 1)
            while end >= 0 and script.startswith('""', end):
                end = script.find('"', end + 2)
            end = n if end < 0 else end + 1
            line += script.count("\n", i, end)
            i = end
        elif c == "$" and (m := _DOLLAR.match(script, i)) and not (i > 0 and (script[i - 1].isalnum() or script[i - 1] == "_")):
            tag = m.group(0)
            end = script.find(tag, m.end())
            end = n if end < 0 else end + len(tag)
            line += script.count("\n", i, end)
            words.append("$body$")
            i = end
        elif c == ";":
            # A BEGIN ATOMIC body (CREATE FUNCTION ... BEGIN ATOMIC ... END) holds statements of
            # its own; its semicolons end nothing until its END, as in psql's own lexer.
            if words and not _atomic(words):
                statements.append((start, words))
                words = []
            i += 1
        elif c == "\\":
            backslashes.append(line)
            i += 1
        elif m := _WORD.match(script, i):
            if not words:
                start = line
            words.append(m.group(0).lower())
            i = m.end()
        else:
            if not words:
                start = line
                words.append("")
            i += 1
    if words:
        statements.append((start, words))
    return statements, backslashes


def problems(script: str) -> list[str]:
    """Why this script can't run here, one line each with where; empty when it can."""
    if not script.strip():
        return ["The script is empty."]
    if len(script.encode()) > MAX_BYTES:
        return [f"The script is over {MAX_BYTES // 1000} kB; split it."]
    if "\x00" in script:
        return ["The script holds a NUL character."]
    found = []
    statements, backslashes = _statements(script)
    for at, words in statements:
        first = words[0]
        if first in _TRANSACTION or (first == "prepare" and words[1:2] == ["transaction"]):
            found.append(f"Line {at}: {first.upper()}. The script already runs in one transaction; leave out BEGIN, COMMIT and the like.")
        elif first == "copy" and ("stdin" in words or "stdout" in words):
            found.append(f"Line {at}: COPY from STDIN or to STDOUT does not run here; use INSERT, or COPY on the server.")
    found += [f"Line {at}: a backslash outside quotes is a psql command, which does not run here." for at in sorted(set(backslashes))]
    return found
