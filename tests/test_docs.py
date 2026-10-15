"""Checks that the docs still describe the code they cite.

Server-free and model-free: it reads Markdown and source text, nothing else, so
it runs in a few seconds on any checkout. ``pytest tests/test_docs.py -q``.

What it holds the docs to (the citation rule in CONTRIBUTING.md):

* a repo path a doc names in backticks (anything with a ``/``: ``src/ndif/...``,
  ``services/api/app.py``, ``.../cluster/node.py``) exists;
* a symbol cited next to its file (``path`` + ``Symbol`` or ``Class.method``) is
  defined in that file — a def, a class, or an assignment;
* every relative link and ``#anchor`` resolves, using GitHub's heading slugs;
* the front-matter ``related:``/``sources:`` lists name files that exist;
* citations by line number (``file.py:123``) do not grow. Line numbers drift
  with every edit, so the rule is path + symbol; the remaining ones are a
  ratchet, ``LINE_CITATION_BASELINE``, which only ever goes down.

Fenced code blocks are skipped throughout: they hold commands and output, not
citations.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Line-number citations still in the docs. Lower this as pages are converted to
# path + symbol; at 0 the test enforces the rule outright. Never raise it.
LINE_CITATION_BASELINE = 1595

LINE_CITATION = re.compile(r"\.(?:py|sh|yml|yaml|toml|json|sql|md):\d")

# Paths that name files in other repositories (nnsight's package and docs), not
# this one.
EXTERNAL_PREFIXES = ("nnsight/", "intervention/", "transformers/", "torch/")

# Paths docs name that are not repo files: what the dashboard writes into its data
# directory at runtime, and the placeholder in a "create a new view" recipe.
NOT_REPO_FILES = frozenset({"cache/values.json", "logs/.state.json", "views/MyView.vue"})

FILE_EXTENSIONS = (
    "py", "sh", "yml", "yaml", "toml", "json", "sql", "md", "txt", "vue", "ts",
    "js", "cfg", "ini", "conf",
)
PATH_TOKEN = re.compile(
    r"^(?:\.\.\./|\./)?[\w.\-/]*[\w\-]\.(?:" + "|".join(FILE_EXTENSIONS) + r")$"
    r"|^(?:\.\.\./|\./)?(?:[\w.\-]+/)*(?:Dockerfile|justfile)$"
)
SYMBOL = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(\))?"
PY_PATH = r"(?:\.\.\./|\./)?[\w.\-/]*\.py(?::\d+(?:-\d+)?)?"
# `path.py` `Symbol`, `path.py`'s `Symbol`, (`path.py`, `Symbol`)
PATH_THEN_SYMBOL = re.compile(
    rf"`({PY_PATH})`(?:'s)?[ \t]*[,:]?[ \t]*`({SYMBOL})`"
)
# `Symbol` (`path.py`), `Symbol`, `path.py`, `Symbol` in `path.py`
SYMBOL_THEN_PATH = re.compile(
    rf"`({SYMBOL})`[ \t]*(?:\(|,|in|at|—|-)[ \t]*`({PY_PATH})`"
)
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
FENCE = re.compile(r"^\s*(```|~~~)")


def doc_files() -> list[Path]:
    files = sorted((ROOT / "docs").rglob("*.md"))
    for name in ("CLAUDE.md", "README.md", "CONTRIBUTING.md", "AGENTS.md"):
        if (ROOT / name).exists():
            files.append(ROOT / name)
    return files


@functools.lru_cache(maxsize=None)
def repo_files() -> tuple[str, ...]:
    """Every file in the checkout git would track (committed or new)."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return tuple(p for p in out.splitlines() if (ROOT / p).exists())


@functools.lru_cache(maxsize=None)
def resolve(token: str) -> tuple[str, ...]:
    """The repo files a cited path can mean.

    Docs cite repo-relative paths (``src/ndif/...``), package-relative ones
    (``services/api/app.py``, relative to ``src/ndif``), elided ones
    (``.../cluster/node.py``) and, next to a symbol, bare file names.
    """
    path = token.removeprefix("./")
    if path.startswith(".../"):
        tail = "/" + path[4:]
        return tuple(f for f in repo_files() if f.endswith(tail))
    for candidate in (path, f"src/ndif/{path}", f"src/{path}"):
        if (ROOT / candidate).is_file():
            return (candidate,)
    tail = "/" + path
    return tuple(f for f in repo_files() if f.endswith(tail))


def prose_lines(path: Path):
    """(line number, text) for every line outside fenced code blocks."""
    fenced = False
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def code_spans(line: str):
    for match in CODE_SPAN.finditer(line):
        yield match.group(2).strip()


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


# --------------------------------------------------------------------------- (a)


def test_no_new_line_number_citations():
    found = [
        f"{rel(path)}:{number}"
        for path in doc_files()
        for number, line in prose_lines(path)
        for _ in LINE_CITATION.finditer(line)
    ]
    assert len(found) <= LINE_CITATION_BASELINE, (
        f"{len(found)} line-number citations, baseline {LINE_CITATION_BASELINE}. "
        "Cite path + symbol instead (`src/ndif/x.py` `Class.method`). "
        f"First few: {found[:10]}"
    )


# --------------------------------------------------------------------------- (b)


def cited_paths(path: Path):
    for number, line in prose_lines(path):
        for span in code_spans(line):
            token = re.sub(r":\d+(?:-\d+)?(?:,\s*:?\d+(?:-\d+)?)*$", "", span)
            token = token.rstrip(",.")
            if not PATH_TOKEN.match(token):
                continue
            if "/" not in token or token.startswith(("/", "~")):
                continue  # a bare name (`models.yaml`) or a path on some host
            if token.startswith(EXTERNAL_PREFIXES) or token in NOT_REPO_FILES:
                continue
            yield number, token


def test_cited_paths_exist():
    missing = []
    for path in doc_files():
        for number, token in cited_paths(path):
            if not resolve(token):
                missing.append(f"{rel(path)}:{number} `{token}`")
    assert not missing, "cited files that do not exist:\n" + "\n".join(missing)


FRONT_MATTER_LIST = re.compile(r"^(related|sources):\s*\[(.*)\]\s*$")


def test_front_matter_paths_exist():
    missing = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        lines = path.read_text().splitlines()
        if not lines or lines[0] != "---":
            continue
        for line in lines[1:]:
            if line == "---":
                break
            match = FRONT_MATTER_LIST.match(line)
            if not match:
                continue
            for entry in filter(None, (e.strip() for e in match.group(2).split(","))):
                if not (ROOT / entry).exists():
                    missing.append(f"{rel(path)} {match.group(1)}: {entry}")
    assert not missing, "front matter names missing files:\n" + "\n".join(missing)


# --------------------------------------------------------------------------- (c)


@functools.lru_cache(maxsize=None)
def defines(file: str, name: str) -> bool:
    text = (ROOT / file).read_text(errors="replace")
    name = re.escape(name)
    return bool(
        re.search(rf"^\s*(?:async\s+)?(?:def|class)\s+{name}\b", text, re.M)
        or re.search(rf"^\s*(?:self\.|cls\.)?{name}\s*[:=]", text, re.M)
        # a `CONFIG`-style dict key
        or re.search(rf"^\s*[\"']{name}[\"']\s*:", text, re.M)
        # an environment variable the file reads by name
        or (name.isupper() and re.search(rf"[\"']{name}[\"']", text))
        # re-exported: `from x import name`, including a parenthesized list
        or re.search(rf"^\s*(?:from\s+[\w.]+\s+)?import\s+[^\n]*\b{name}\b", text, re.M)
        or re.search(rf"^from\s+[\w.]+\s+import\s+\([^)]*\b{name}\b", text, re.M)
    )


def symbol_citations(path: Path):
    """(line, path, symbol) for each path + symbol citation.

    Only the path + symbol form: a citation still carrying a line number names
    a call site as often as a definition, and is counted by the ratchet above
    until it is converted.
    """
    for number, line in prose_lines(path):
        pairs = [(m.group(1), m.group(2)) for m in PATH_THEN_SYMBOL.finditer(line)]
        pairs += [(m.group(2), m.group(1)) for m in SYMBOL_THEN_PATH.finditer(line)]
        for cited, symbol in pairs:
            if ":" in cited or PATH_TOKEN.match(symbol.removesuffix("()")):
                continue  # a line-number citation, or two paths side by side
            yield number, cited, symbol


def test_cited_symbols_are_defined():
    wrong = []
    for path in doc_files():
        for number, cited, symbol in symbol_citations(path):
            if cited.startswith(EXTERNAL_PREFIXES):
                continue
            candidates = resolve(cited)
            if not candidates:
                continue  # test_cited_paths_exist reports it
            parts = symbol.removesuffix("()").split(".")
            # `Class.method` needs both; a dotted module prefix
            # (`queue.replica.Replica`) is skipped by checking the last two.
            needed = parts[-2:] if len(parts) > 1 else parts
            if not any(all(defines(c, p) for p in needed) for c in candidates):
                wrong.append(f"{rel(path)}:{number} `{cited}` `{symbol}`")
    assert not wrong, "symbols not defined in the cited file:\n" + "\n".join(wrong)


# --------------------------------------------------------------------------- (d)


def slugify(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links -> text
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


@functools.lru_cache(maxsize=None)
def anchors(path: Path) -> frozenset[str]:
    seen: dict[str, int] = {}
    out = set()
    for _, line in prose_lines(path):
        out.update(HTML_ANCHOR.findall(line))
        match = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = slugify(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        out.add(slug if count == 0 else f"{slug}-{count}")
    return frozenset(out)


def test_links_and_anchors_resolve():
    broken = []
    for path in doc_files():
        for number, line in prose_lines(path):
            for target in LINK.findall(line):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                    continue  # http:, https:, mailto:
                file_part, _, anchor = target.partition("#")
                dest = (path.parent / file_part).resolve() if file_part else path
                if not dest.exists():
                    broken.append(f"{rel(path)}:{number} {target} (no such file)")
                    continue
                if anchor and dest.suffix == ".md" and anchor not in anchors(dest):
                    broken.append(f"{rel(path)}:{number} {target} (no such anchor)")
    assert not broken, "broken links:\n" + "\n".join(broken)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
