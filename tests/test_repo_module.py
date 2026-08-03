"""The path-trust primitives live in a leaf module, so every route can reach the one gate.

`backends/api.py` has to apply the same tracked/inside/not-metadata rule the docs paths
pass, and it cannot import `cli` or `config` to get it: `config` imports `backends`, so
`api -> config -> backends -> api` closes a cycle. The rule therefore lives in
`rocket_review.repo`, which imports nothing first-party at all, and `cli` and `config`
take it from there rather than carrying a second copy — one rule, one implementation, is
the whole point of the module existing.
"""

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import rocket_review
from rocket_review import cli, config

MODULE = "rocket_review.repo"

#: The gate and everything the gate is made of. `case_folds`/`tracked_key` decide what the
#: tracked set is keyed by and `run_capture` is how it is read, so they belong beside it.
PRIMITIVES = (
    "resolve_doc_path",
    "resolve_doc_paths",
    "tracked",
    "tracked_files",
    "is_repository_metadata",
    "inside_dot_git",
    "find_git_root",
    "case_folds",
    "tracked_key",
    "run_capture",
    "capture",
    "clear_caches",
)


def repo_module():
    """Imported here rather than at collection time: a module that does not exist yet must
    fail the test asserting it exists, not error this file out of collection."""
    return importlib.import_module(MODULE)


def repo_source() -> str:
    return (Path(rocket_review.__file__).parent / "repo.py").read_text(encoding="utf-8")


def test_repo_module_exposes_the_path_trust_primitives():
    repo = repo_module()
    missing = [name for name in PRIMITIVES if not callable(getattr(repo, name, None))]
    assert not missing, f"{MODULE} does not expose {missing}"


def test_repo_module_imports_nothing_first_party():
    """Statically — including imports inside a function, which a fresh import would miss."""
    imported = set()
    for node in ast.walk(ast.parse(repo_source())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import cannot leave the package, so level alone settles it.
            imported.add("rocket_review" if node.level else (node.module or ""))
    first_party = sorted(
        name for name in imported
        if name == "rocket_review" or name.startswith("rocket_review.")
    )
    assert not first_party, f"{MODULE} must import only stdlib; it imports {first_party}"


def test_importing_the_repo_module_pulls_in_no_other_first_party_module():
    """And dynamically, in a fresh interpreter, which is what the cycle actually cares about."""
    probe = (
        "import sys, rocket_review.repo; "
        "print(sorted(m for m in sys.modules if m.startswith('rocket_review')))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['rocket_review', 'rocket_review.repo']"


def test_cli_and_config_take_the_gate_from_the_leaf_module():
    """The same objects, not equivalent copies: a second implementation is the failure mode
    this whole module exists to prevent."""
    repo = repo_module()
    assert cli.resolve_doc_path is repo.resolve_doc_path
    assert cli.tracked_files is repo.tracked_files
    assert cli.case_folds is repo.case_folds
    assert cli.run_capture is repo.run_capture
    assert config.inside_dot_git is repo.inside_dot_git
    assert config.find_git_root is repo.find_git_root


def test_the_api_backend_still_imports():
    """The cycle, asserted from the side that would close it."""
    result = subprocess.run(
        [sys.executable, "-c", "import rocket_review.backends.api"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
