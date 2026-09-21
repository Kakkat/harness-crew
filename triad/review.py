"""Optional Jev first-pass review, run by the harness as an advisory check. Standard library only.

Each function (or 80-line chunk of other text) is asked a checklist of narrow yes/no questions in one
request. A score is flagged when it is high and stands out from how that question scores across the
code: flags say where a reviewer should look, never what is wrong.
"""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import statistics

from .gate import ask_many

CHECKLIST = {
    "injection": "Does this code run a string as code (eval, exec, compile) or build a shell or SQL command from input?",
    "swallowed": "Is an exception caught and then ignored, or turned into a normal-looking result?",
    "leak": "On some path, including errors, is a file, socket, lock or process left open?",
    "unbounded": "Can this code block or recurse without limit (no timeout, unbounded recursion or loop) for some input?",
    "destructive": "Does this code delete, overwrite or kill something without checking that it owns it?",
    "boundary": "Is there an off-by-one error, wrong comparison or wrong boundary for some realistic input?",
    "exception_type": "Can some input make this code raise an exception type other than the one it documents or promises?",
    "mismatch": "Does this code do something different from what its name or docstring says?",
}
SKIP = {"__pycache__", "node_modules", "venv", "build", "dist"}


def pieces(paths):
    """Python split by function and method; other text in 80-line chunks. Hidden and dependency folders skipped."""
    seen = set()
    for root in map(Path, paths):
        files = (sorted(p for p in root.rglob("*") if p.is_file() and not any(
            part.startswith(".") or part in SKIP for part in p.relative_to(root).parts[:-1])) if root.is_dir() else [root])
        for path in files:
            if path.resolve() in seen or path.suffix in {".pyc", ".lock", ".sqlite"}:
                continue
            seen.add(path.resolve())
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if path.suffix == ".py":
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    tree = None
                if tree:
                    scopes = [(tree, "")] + [(n, n.name + ".") for n in tree.body if isinstance(n, ast.ClassDef)]
                    for scope, prefix in scopes:
                        for node in scope.body:
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno > node.lineno + 1:
                                yield {"where": f"{path}:{node.lineno}", "name": prefix + node.name,
                                       "code": ast.get_source_segment(source, node)}
                    continue
            lines = source.splitlines()
            for start in range(0, len(lines), 80):
                yield {"where": f"{path}:{start + 1}", "name": f"lines {start + 1}-{start + 80}",
                       "code": "\n".join(lines[start:start + 80])}


def review(paths, key, context=None, model=None, minimum=0.7, checklist=CHECKLIST):
    """Return (scored pieces, flags sorted by probability)."""
    units = list(pieces(paths))

    def judge(unit):
        state = {"code": unit["code"], "location": unit["where"], "function": unit["name"]}
        if context:
            state["project"] = context
        return unit | {"p": ask_many(checklist, state, key, model)[0]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        scored = list(pool.map(judge, units))
    flags = []
    for name in checklist:
        baseline = statistics.median(u["p"][name] for u in scored) if scored else 0
        flags += [(u["p"][name], name, u) for u in scored
                  if u["p"][name] >= 0.9 or (u["p"][name] >= minimum and u["p"][name] - baseline >= 0.2)]
    return scored, sorted(flags, key=lambda flag: -flag[0])


def digest(scored, flags, checklist=CHECKLIST):
    lines = [f"Jev review: {len(scored)} pieces x {len(checklist)} questions; "
             f"{len(flags)} flag(s). Flags say where to look, not what is wrong."]
    lines += [f"  {p:4.0%}  {name:14} {unit['where']}  {unit['name']}" for p, name, unit in flags[:20]]
    return "\n".join(lines)
