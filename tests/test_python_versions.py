"""The supported Pythons are written in four places, and they must agree.

The classifiers tell users which versions rr supports. The CI test and build matrices and
the release test matrix are what proves it. A version added to the classifiers and missed
in one workflow ships a claim that no run tested.
"""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = "Programming Language :: Python :: "


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def jobs(workflow: str) -> dict:
    path = ROOT / ".github" / "workflows" / workflow
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def classified_versions() -> list[str]:
    named = [c.removeprefix(CLASSIFIER) for c in project()["classifiers"]]
    return sorted((v for v in named if v.startswith("3.")), key=lambda v: int(v[2:]))


def matrix(job: dict) -> list[str]:
    return job["strategy"]["matrix"]["python"]


def test_every_python_matrix_names_the_classified_versions():
    want = classified_versions()
    ci, publish = jobs("ci.yml"), jobs("publish.yml")
    assert matrix(ci["test"]) == want
    assert matrix(ci["build"]) == want
    assert matrix(publish["test"]) == want


def test_the_oldest_classified_version_is_the_requires_python_floor():
    assert project()["requires-python"] == f">={classified_versions()[0]}"


def test_each_matrix_leg_runs_on_its_own_python():
    """A matrix whose value reaches no interpreter request runs every leg on one Python."""
    ci, publish = jobs("ci.yml"), jobs("publish.yml")
    for name in ("test", "build"):
        assert ci[name]["env"]["UV_PYTHON"] == "${{ matrix.python }}", name
    setup = [s for s in publish["test"]["steps"] if "actions/setup-python" in s.get("uses", "")]
    assert [s["with"]["python-version"] for s in setup] == ["${{ matrix.python }}"]
