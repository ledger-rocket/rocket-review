import subprocess
import types
from pathlib import Path

import pytest

from rocket_review import cli, config
from rocket_review.cli import main

PROJECT = Path("/repo/.rocket-review.toml")
USER = Path("/home/u/.config/rocket-review/config.toml")


def layer(path, **values):
    return config.Layer(path=path, values=values, repo_supplied=path == PROJECT)


def write(directory: Path, body: str, name: str = config.PROJECT_CONFIG_NAME) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def write_user_config(body: str) -> Path:
    """Write the user config where discovery will look for it (XDG is isolated per test)."""
    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def make_repo(tmp_path: Path, body: str | None = None, subdir: str = "") -> Path:
    """A real git repo with an optional project config; returns the directory to run from.

    Real rather than a bare .git directory: what a project config may name is bounded by
    what the repository tracks, and only git can answer that.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value],
                       check=True, capture_output=True)
    if body is not None:
        write(repo, body)
    if not subdir:
        return repo
    nested = repo / subdir
    nested.mkdir(parents=True)
    return nested


def loaded(path: Path, *, repo_supplied: bool = False) -> dict:
    return config.load_file(path, repo_supplied=repo_supplied).values


def error_from(path: Path, *, repo_supplied: bool = False) -> str:
    with pytest.raises(config.ConfigError) as e:
        config.load_file(path, repo_supplied=repo_supplied)
    return str(e.value)


def track(repo: Path, *names: str) -> None:
    """Commit files so the repository carries them: HEAD is what decides what rr may read."""
    subprocess.run(["git", "-C", str(repo), "add", "-f", "--", *names],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "carry"],
                   check=True, capture_output=True)


# --- discovery -----------------------------------------------------------------------


def test_project_config_found_by_walking_up_to_the_repo_root(tmp_path):
    run_from = make_repo(tmp_path, "timeout = 60", subdir="src/deep")
    assert config.find_project_config(run_from) == tmp_path / "repo" / ".rocket-review.toml"


def test_nearest_project_config_wins(tmp_path):
    run_from = make_repo(tmp_path, "timeout = 60", subdir="src")
    nearer = write(run_from, "timeout = 30")
    assert config.find_project_config(run_from) == nearer


def test_discovery_stops_at_the_git_root(tmp_path):
    # A config above the repo belongs to no project of this one's; adopting it would let a
    # file in $HOME quietly configure every repo beneath it.
    write(tmp_path, "timeout = 60")
    run_from = make_repo(tmp_path, subdir="src")
    assert config.find_project_config(run_from) is None


def test_a_git_file_marks_the_root_too(tmp_path):
    # Worktrees and submodules have a .git file rather than a directory.
    write(tmp_path, "timeout = 60")
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /elsewhere\n")
    assert config.find_project_config(worktree / "src") is None


def test_outside_a_repo_only_the_current_directory_is_read(tmp_path):
    loose = tmp_path / "loose"
    nested = loose / "nested"
    nested.mkdir(parents=True)
    here = write(nested, "timeout = 60")
    assert config.find_project_config(nested) == here
    write(loose, "timeout = 30")
    assert config.find_project_config(nested) == here  # still the one in the cwd
    (nested / config.PROJECT_CONFIG_NAME).unlink()
    assert config.find_project_config(nested) is None  # the parent's is never walked to


def test_no_project_config_anywhere(tmp_path):
    assert config.find_project_config(make_repo(tmp_path, subdir="src")) is None


def test_user_config_path_honours_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.user_config_path() == tmp_path / "xdg" / "rocket-review" / "config.toml"


def test_user_config_path_defaults_to_dot_config(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert config.user_config_path() == (
        tmp_path / "home" / ".config" / "rocket-review" / "config.toml"
    )


def test_load_reads_project_then_user(tmp_path):
    run_from = make_repo(tmp_path, "timeout = 60")
    user = write_user_config("effort = 'high'")
    layers = config.load(no_config=False, cwd=run_from)
    assert [item.path for item in layers] == [run_from / config.PROJECT_CONFIG_NAME, user]


def test_no_config_ignores_both_files(tmp_path):
    run_from = make_repo(tmp_path, "timeout = 60")
    write_user_config("timeout = 30")
    assert config.load(no_config=True, cwd=run_from) == []


# --- precedence ----------------------------------------------------------------------

# A boolean key needs both rows: with only two values every single row has one leg where
# the winning layer and the layer below it agree, and that leg proves nothing.
SCALARS = [
    ("timeout", 1800, 1200, 600, None),
    ("effort", "max", "high", "low", None),
    ("fail_on", "critical", "high", "low", None),
    ("json", True, False, True, False),
    ("json", True, True, False, False),
    ("full", True, False, True, False),
    ("full", True, True, False, False),
    ("docs", ["from-cli.md"], ["from-project.md"], ["from-user.md"], None),
]


@pytest.mark.parametrize("key,flag,project,user,builtin", SCALARS)
def test_every_layer_wins_in_turn(key, flag, project, user, builtin):
    files = [layer(PROJECT, **{key: project}), layer(USER, **{key: user})]
    assert getattr(config.resolve({key: flag}, files), key) == flag
    assert getattr(config.resolve({}, files), key) == project
    assert getattr(config.resolve({}, files[1:]), key) == user
    assert getattr(config.resolve({}, []), key) == builtin


@pytest.mark.parametrize("key,flag,project,user,builtin", SCALARS)
def test_sources_name_the_layer_that_won(key, flag, project, user, builtin):
    files = [layer(PROJECT, **{key: project}), layer(USER, **{key: user})]
    assert config.resolve({key: flag}, files).from_file(key) is None
    assert config.resolve({}, files).from_file(key) == str(PROJECT)
    assert config.resolve({}, files[1:]).from_file(key) == str(USER)
    assert config.resolve({}, []).from_file(key) is None


def test_merge_is_per_key_not_per_file():
    settings = config.resolve(
        {},
        [
            layer(PROJECT, backends={"diff": "codex"}),
            layer(USER, timeout=1800, effort="high", backends={"plan": "claude"}),
        ],
    )
    assert settings.timeout == 1800  # the project file naming one key leaves the rest alone
    assert settings.effort == "high"
    assert settings.backends == {"plan": "claude", "code": "claude", "diff": "codex"}


def test_a_false_in_a_file_outranks_a_lower_layer():
    files = [layer(PROJECT, json=False, docs=False), layer(USER, json=True, docs=["u.md"])]
    settings = config.resolve({}, files)
    assert settings.json is False
    assert settings.docs is None  # "no docs", distinct from "docs unset"


def test_backends_default_covers_the_modes_no_file_names():
    settings = config.resolve({}, [layer(USER, backends={"default": "opencode", "plan": "api"})])
    assert settings.backends == {"plan": "api", "code": "opencode", "diff": "opencode"}


def test_a_named_mode_outranks_the_same_files_default():
    settings = config.resolve(
        {}, [layer(PROJECT, backends={"default": "opencode", "plan": "api"})]
    )
    assert settings.backends == {"plan": "api", "code": "opencode", "diff": "opencode"}


def test_a_higher_layers_default_outranks_a_lower_layers_named_mode():
    # Layer order decides between files, here as everywhere: a project standardising on one
    # backend is not overruled by whatever a developer once put in their personal file.
    settings = config.resolve(
        {},
        [layer(PROJECT, backends={"default": "opencode"}), layer(USER, backends={"plan": "api"})],
    )
    assert settings.backends == {"plan": "opencode", "code": "opencode", "diff": "opencode"}


def test_models_merge_per_backend():
    settings = config.resolve(
        {},
        [
            layer(PROJECT, models={"codex": "gpt-project"}),
            layer(USER, models={"codex": "gpt-user", "claude": "claude-user"}),
        ],
    )
    assert settings.models == {"codex": "gpt-project", "claude": "claude-user"}


def test_built_in_backends_apply_with_no_files():
    assert config.resolve({}, []).backends == config.DEFAULT_BACKEND_BY_MODE


# --- validation ----------------------------------------------------------------------


def test_full_schema_round_trips(tmp_path):
    path = write(tmp_path, """
timeout = 1800
effort = "high"
fail_on = "medium"
json = true
full = true
docs = true

[backends]
plan = "codex"
code = "claude"
diff = "claude"
default = "opencode"

[models]
codex = "gpt-5.6-sol"
claude = "claude-opus-5"
""")
    assert loaded(path) == {
        "timeout": 1800,
        "effort": "high",
        "fail_on": "medium",
        "json": True,
        "full": True,
        "docs": [],
        "backends": {"plan": "codex", "code": "claude", "diff": "claude", "default": "opencode"},
        "models": {"codex": "gpt-5.6-sol", "claude": "claude-opus-5"},
    }


def test_unknown_key_names_the_file_the_key_and_the_accepted_set(tmp_path):
    path = write(tmp_path, "timeuot = 60\n")
    message = error_from(path)
    assert str(path) in message
    assert "unknown key 'timeuot'" in message
    assert "Accepted: backends, docs, effort, fail_on, full, json, models, timeout." in message


def test_several_unknown_keys_are_all_named(tmp_path):
    message = error_from(write(tmp_path, "nope = 1\nalso = 2\n"))
    assert "unknown keys 'also', 'nope'" in message


@pytest.mark.parametrize("key", ["mode", "prompt", "diff", "staged", "commit", "pr", "repo",
                                "backend", "model", "llms", "api", "files", "no_config"])
def test_config_cannot_reach_flags_that_are_not_config_keys(tmp_path, key):
    # The config surface is exactly the mirrored preferences: what to review, and the
    # one-off flags that decide it, stay on the command line where they are visible.
    assert f"unknown key '{key}'" in error_from(write(tmp_path, f"{key} = 'x'\n"))


def test_malformed_toml_names_the_file_and_the_position(tmp_path):
    path = write(tmp_path, "timeout = 60\neffort \"high\"\n")
    message = error_from(path)
    assert f"invalid TOML in {path}" in message
    assert "line 2" in message


def test_non_utf8_file_is_rejected(tmp_path):
    path = tmp_path / config.PROJECT_CONFIG_NAME
    path.write_bytes(b"effort = '\xff\xfe'\n")
    assert "is not valid UTF-8" in error_from(path)


def test_unreadable_file_names_itself(tmp_path):
    directory = tmp_path / config.PROJECT_CONFIG_NAME
    directory.mkdir()
    assert f"could not read {directory}" in error_from(directory)


@pytest.mark.parametrize("body,expected", [
    ("timeout = 'soon'", "timeout must be a positive integer, got 'soon'"),
    ("timeout = 0", "timeout must be a positive integer, got 0"),
    ("timeout = -5", "timeout must be a positive integer, got -5"),
    ("timeout = true", "timeout must be a positive integer, got True"),
    ("timeout = 1.5", "timeout must be a positive integer, got 1.5"),
    ("effort = 3", "effort must be a non-empty string, got 3"),
    ("effort = '  '", "effort must be a non-empty string"),
    ("fail_on = 'huge'", "fail_on must be one of critical, high, medium, low, got 'huge'"),
    ("fail_on = true", "fail_on must be one of critical, high, medium, low, got True"),
    ("json = 'yes'", "json must be true or false, got 'yes'"),
    ("full = 1", "full must be true or false, got 1"),
    ("docs = 'llms.txt'", "docs must be true, false, or a list of paths, got 'llms.txt'"),
    ("docs = [1]", "docs must be true, false, or a list of paths, got [1]"),
    ("backends = 'codex'", "[backends] must be a table of mode = backend"),
    ("models = 'gpt'", "[models] must be a table of backend = model"),
])
def test_invalid_values_are_rejected(tmp_path, body, expected):
    message = error_from(write(tmp_path, f"{body}\n"))
    assert str(tmp_path) in message
    assert expected in message


def test_unknown_backends_mode_is_rejected(tmp_path):
    message = error_from(write(tmp_path, "[backends]\ndifff = 'codex'\n"))
    assert "unknown mode 'difff' in [backends]" in message
    assert "Accepted: plan, code, diff, default." in message


def test_unknown_backend_name_is_rejected(tmp_path):
    message = error_from(write(tmp_path, "[backends]\ndiff = 'gemini'\n"))
    assert "unknown backend 'gemini' in backends.diff" in message
    assert "Accepted (one per mode): codex, claude, opencode, api." in message


@pytest.mark.parametrize("value,shown", [
    ("'codex,claude'", "'codex,claude'"),  # the --backend comma list
    ("['codex', 'claude']", "['codex', 'claude']"),  # a TOML array, which is unhashable
    ("7", "7"),
])
def test_a_backend_list_is_not_a_backend_name(tmp_path, value, shown):
    # --backend takes a list; a per-mode default takes one name, and says so.
    message = error_from(write(tmp_path, f"[backends]\ndiff = {value}\n"))
    assert f"unknown backend {shown} in backends.diff" in message
    assert "one per mode" in message


def test_unknown_models_backend_is_rejected(tmp_path):
    message = error_from(write(tmp_path, "[models]\ngemini = 'flash'\n"))
    assert "unknown backend 'gemini' in [models]" in message
    assert "Accepted: codex, claude, opencode, api." in message


@pytest.mark.parametrize("value", ["'high low'", "'''high\nsandbox_mode = \"x\"'''"])
def test_effort_must_be_a_single_word(tmp_path, value):
    # codex takes effort as -c model_reasoning_effort=<value>, the one setting interpolated
    # into a constructed argument — and a project file's value comes from the repository.
    assert "effort must be a single word" in error_from(write(tmp_path, f"effort = {value}\n"))


def test_model_must_be_a_string(tmp_path):
    assert "models.codex must be a non-empty string, got 7" in error_from(
        write(tmp_path, "[models]\ncodex = 7\n")
    )


def test_every_key_rejects_a_wrong_type_as_a_config_error(tmp_path):
    # Whatever type a user types, they get rr's error and not a traceback: a TOML array is
    # unhashable, so a membership test on one raises TypeError unless the type is checked
    # first.
    assignments = [
        "timeout = {v}", "effort = {v}", "fail_on = {v}", "json = {v}", "full = {v}",
        "docs = {v}", "backends = {v}", "models = {v}",
        "[backends]\ndiff = {v}", "[models]\ncodex = {v}",
    ]
    values = ["'x'", "7", "1.5", "true", "[1, 2]", "['a']", "{ a = 1 }", "[]"]
    crashed = []
    for assignment in assignments:
        for value in values:
            body = assignment.format(v=value)
            try:
                config.load_file(write(tmp_path, body + "\n"), repo_supplied=True)
            except config.ConfigError:
                pass  # the accepted outcome; the valid combinations simply load
            except Exception as e:
                crashed.append(f"{body!r} -> {type(e).__name__}: {e}")
    assert crashed == []


def test_docs_paths_resolve_against_the_config_file(tmp_path):
    path = write(tmp_path, "docs = ['standards/llms.txt']\n")
    assert loaded(path)["docs"] == [str(tmp_path.resolve() / "standards" / "llms.txt")]


def test_who_named_the_path_is_what_the_funnel_is_wired_to(tmp_path):
    # The whole boundary in one place, and the only place: the same path refused for the
    # repository and read for the user. Flipping either call site's user_named fails here.
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-SECRET\n")
    run_from = make_repo(tmp_path)
    assert cli.resolve_doc_path(outside, user_named=False, base=run_from) is None
    assert cli.resolve_doc_path(outside, user_named=True, base=run_from) == outside.resolve()

    local = run_from / "local.md"
    local.write_text("local notes\n")
    assert cli.resolve_doc_path(local, user_named=False, base=run_from) is None
    track(run_from, "local.md")
    cli.tracked_files.cache_clear()
    assert cli.resolve_doc_path(local, user_named=False, base=run_from) == local.resolve()

    # The footgun check survives even the user's own word.
    assert cli.resolve_doc_path(run_from / ".git" / "config", user_named=True,
                                base=run_from) is None


def test_a_repo_with_no_commits_carries_nothing(tmp_path):
    # No HEAD, so git cannot answer: nothing is tracked and the funnel refuses rather than
    # falling open.
    run_from = make_repo(tmp_path)
    doc = run_from / "std.md"
    doc.write_text("rules\n")
    subprocess.run(["git", "-C", str(run_from), "add", "std.md"], check=True, capture_output=True)
    cli.tracked_files.cache_clear()
    assert cli.resolve_doc_path(doc, user_named=False, base=run_from) is None


def test_the_index_does_not_widen_what_a_repository_offers(tmp_path):
    # A stray `git add .env` stages a file the repository does not carry; HEAD decides.
    run_from = make_repo(tmp_path)
    (run_from / "std.md").write_text("rules\n")
    track(run_from, "std.md")
    secret = run_from / ".env"
    secret.write_text("AWS_KEY=SECRET\n")
    subprocess.run(["git", "-C", str(run_from), "add", "-f", ".env"], check=True,
                   capture_output=True)
    cli.tracked_files.cache_clear()
    assert cli.resolve_doc_path(secret, user_named=False, base=run_from) is None


@pytest.mark.parametrize("path", [
    "/repo/.git/config", "/repo/.GIT/config", "/repo/.Git/hooks/x",
    # Win32 strips trailing dots and spaces while resolving, so these reach .git there.
    "/repo/.git./config", "/repo/.git /config", "/repo/.GIT. /config", "/repo/.git/",
])
def test_inside_dot_git_covers_every_spelling_that_reaches_metadata(path):
    assert config.inside_dot_git(Path(path))


@pytest.mark.parametrize("path", [
    "/repo/.github/workflows/ci.yml", "/repo/.gitignore", "/repo/git/config",
    "/repo/docs/git.md", "/repo/agit/x",
])
def test_inside_dot_git_leaves_ordinary_paths_alone(path):
    assert not config.inside_dot_git(Path(path))


@pytest.mark.parametrize(
    "item", [".git/config", ".GIT/config", "./.Git/config", "src/../.GIT/config"]
)
def test_a_project_config_may_not_name_docs_inside_dot_git(tmp_path, monkeypatch, item):
    # Inside the repo but not of it: .git holds local state a clone does not control, and a
    # named doc is copied into the prompt verbatim. Compared case-insensitively because
    # resolve() does not canonicalise case, and on a case-insensitive filesystem '.GIT'
    # opens the real .git.
    run_from = make_repo(tmp_path, f"docs = ['{item}']\n")
    (run_from / "src").mkdir(exist_ok=True)
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    assert jobs == {}


def test_a_project_config_may_not_name_docs_outside_itself(tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-SECRET\n")
    run_from = make_repo(tmp_path, f"docs = ['{outside}']\n")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    assert jobs == {}


def test_a_user_config_may_still_name_docs_anywhere(tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("MY OWN STANDARDS\n")
    run_from = make_repo(tmp_path)
    write_user_config(f"docs = ['{outside}']\n")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert "MY OWN STANDARDS" in jobs["claude"].docs_content


def test_an_unusable_docs_path_is_a_config_error_not_a_traceback(tmp_path):
    # An embedded null byte makes resolve() raise ValueError before any stat; a value that
    # validates as a string still has to come back as rr's error, not a traceback.
    assert "is not a usable path" in error_from(write(tmp_path, 'docs = ["a\\u0000b"]\n'))


# --- through the CLI -----------------------------------------------------------------


def run_main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    try:
        main()
    except SystemExit as e:
        return e.code if e.code is not None else 0
    return 0


def fake_backends(monkeypatch):
    """Record every job each backend is handed; every backend counts as installed."""
    jobs = {}

    def make(name):
        def review(job):
            jobs[name] = job
            return '{"verdict": "approve", "summary": "s", "findings": []}'
        return review

    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS",
        {n: types.SimpleNamespace(review=make(n)) for n in ("codex", "claude", "opencode", "api")},
    )
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.available", lambda name: True)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    monkeypatch.setattr("rocket_review.cli.get_diff", lambda staged: "diff --git a b\n+x")
    return jobs


def test_project_config_sets_the_backend_for_a_mode(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "[backends]\ndiff = 'codex'\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert list(jobs) == ["codex"]


def test_an_explicit_backend_still_wins_over_the_config(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "[backends]\ndiff = 'codex'\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--backend", "opencode"]) == 0
    assert list(jobs) == ["opencode"]


def test_no_config_restores_the_built_in_default(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "[backends]\ndiff = 'codex'\ntimeout = 60\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--no-config"]) == 0
    assert list(jobs) == ["claude"]
    assert jobs["claude"].timeout is None


def test_the_config_is_read_from_a_subdirectory_of_the_repo(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "[backends]\ndiff = 'codex'\n", subdir="src/deep"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert list(jobs) == ["codex"]


def test_models_table_pins_the_backends_model(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "[models]\nclaude = 'claude-opus-5'\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert jobs["claude"].model == "claude-opus-5"


def test_models_table_fills_only_the_unpinned_backends(monkeypatch, tmp_path):
    monkeypatch.chdir(
        make_repo(tmp_path, "[models]\ncodex = 'gpt-config'\nclaude = 'claude-config'\n")
    )
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--backend", "codex:gpt-pinned,claude"]) == 0
    assert jobs["codex"].model == "gpt-pinned"
    assert jobs["claude"].model == "claude-config"


def test_model_flag_beats_the_models_table(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "[models]\nclaude = 'claude-config'\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--model", "claude-flag"]) == 0
    assert jobs["claude"].model == "claude-flag"


def test_user_config_applies_where_the_project_file_is_silent(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "[backends]\ndiff = 'codex'\n"))
    write_user_config("timeout = 1800\neffort = 'high'\n[backends]\ndiff = 'claude'\n")
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert list(jobs) == ["codex"]  # the project file wins the key it sets
    assert jobs["codex"].timeout == 1800  # and leaves the user file's keys in force
    assert jobs["codex"].effort == "high"


def test_flags_beat_the_user_config(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path))
    write_user_config("timeout = 1800\neffort = 'high'\n")
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--timeout", "60", "--effort", "low"]) == 0
    assert jobs["claude"].timeout == 60
    assert jobs["claude"].effort == "low"


def test_config_json_emits_the_envelope(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(make_repo(tmp_path, "json = true\n"))
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert '"schema_version"' in capsys.readouterr().out


def test_config_fail_on_gates_the_run(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "json = true\nfail_on = 'high'\n"))
    fake_backends(monkeypatch)
    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS",
        {"claude": types.SimpleNamespace(review=lambda job: (
            '{"verdict": "needs_fixes", "summary": "s", "findings": [{"severity": "high", '
            '"title": "t", "file": null, "line": null, "why": "w", "fix": "f"}]}'
        ))},
    )
    assert run_main(monkeypatch, ["--diff"]) == 2


def test_config_fail_on_without_json_errors_and_names_the_file(monkeypatch, tmp_path, capsys):
    # The same refusal the flags get: the config layer changes defaults, not what is legal.
    run_from = make_repo(tmp_path, "fail_on = 'high'\n")
    monkeypatch.chdir(run_from)
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    err = capsys.readouterr().err
    assert "--fail-on requires --json" in err
    assert f"fail_on is set in {run_from / config.PROJECT_CONFIG_NAME}" in err


def test_config_full_without_json_errors_and_names_the_file(monkeypatch, tmp_path, capsys):
    run_from = make_repo(tmp_path, "full = true\n")
    monkeypatch.chdir(run_from)
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    err = capsys.readouterr().err
    assert "--full requires --json" in err
    assert f"full is set in {run_from / config.PROJECT_CONFIG_NAME}" in err


def test_config_effort_with_opencode_errors_and_names_the_file(monkeypatch, tmp_path, capsys):
    # Neither half was typed: effort came from the file, and opencode may have too.
    run_from = make_repo(tmp_path, "effort = 'high'\n[backends]\ndiff = 'opencode'\n")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    err = capsys.readouterr().err
    assert "--effort is not supported by the opencode backend" in err
    assert f"effort is set in {run_from / config.PROJECT_CONFIG_NAME}" in err
    assert jobs == {}


def test_the_substitution_notice_names_the_config_that_chose_the_backend(
    monkeypatch, tmp_path, capsys
):
    run_from = make_repo(tmp_path, "[backends]\ndefault = 'claude'\n")
    monkeypatch.chdir(run_from)
    fake_backends(monkeypatch)
    monkeypatch.setattr("rocket_review.cli.available", lambda name: name == "codex")
    assert run_main(monkeypatch, ["--diff"]) == 0
    err = capsys.readouterr().err
    assert "default backend 'claude' for diff review is unavailable; using 'codex'" in err
    assert f"the diff backend is set in {run_from / config.PROJECT_CONFIG_NAME}" in err


def test_a_flag_conflict_the_user_typed_names_no_file(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(make_repo(tmp_path))
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--fail-on", "high"]) == 1
    assert "is set in" not in capsys.readouterr().err


def test_config_docs_true_auto_discovers(monkeypatch, tmp_path):
    run_from = make_repo(tmp_path, "docs = true\n")
    (run_from / "llms.txt").write_text("project standards: no bare excepts\n")
    track(run_from, "llms.txt")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert "no bare excepts" in jobs["claude"].docs_content


def test_config_docs_paths_are_relative_to_the_config_file(monkeypatch, tmp_path):
    # Read from a subdirectory, so a cwd-relative reading of the path would miss the file.
    run_from = make_repo(tmp_path, "docs = ['docs/standards.md']\n", subdir="src")
    (tmp_path / "repo" / "docs").mkdir()
    (tmp_path / "repo" / "docs" / "standards.md").write_text("standards: prefer small diffs\n")
    track(tmp_path / "repo", "docs/standards.md")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert "prefer small diffs" in jobs["claude"].docs_content


def test_docs_flag_replaces_the_config_docs(monkeypatch, tmp_path):
    run_from = make_repo(tmp_path, "docs = true\n")
    (run_from / "llms.txt").write_text("from llms\n")
    (run_from / "other.md").write_text("from the flag\n")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--docs", "other.md"]) == 0
    docs = jobs["claude"].docs_content
    assert "from the flag" in docs
    assert "from llms" not in docs


def test_a_broken_config_fails_before_any_git_or_backend_work(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(make_repo(tmp_path, "timeout = 'soon'\n"))
    jobs = fake_backends(monkeypatch)
    # Put the real diff preflight back and watch the git layer itself: config has to be
    # settled before rr shells out, not merely before a backend is launched.
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", cli.ensure_diff_exists)
    git = []

    def spy(cmd):
        git.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr("rocket_review.cli.run_capture", spy)
    assert run_main(monkeypatch, ["--diff"]) == 1
    assert "timeout must be a positive integer" in capsys.readouterr().err
    assert git == []
    assert jobs == {}


def test_a_broken_user_config_is_reported_too(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(make_repo(tmp_path))
    user = write_user_config("[backends]\nplan = 'gemini'\n")
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    assert f"{user}: unknown backend 'gemini' in backends.plan" in capsys.readouterr().err


def test_no_config_ignores_a_broken_config(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path, "timeout = 'soon'\n"))
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--no-config"]) == 0


def test_no_config_ignores_the_user_file_too(monkeypatch, tmp_path):
    monkeypatch.chdir(make_repo(tmp_path))
    write_user_config("timeout = 1800\n[backends]\ndiff = 'codex'\n")
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--no-config"]) == 0
    assert list(jobs) == ["claude"]
    assert jobs["claude"].timeout is None


def test_backends_default_applies_to_every_mode(monkeypatch, tmp_path):
    run_from = make_repo(tmp_path, "[backends]\ndefault = 'opencode'\n")
    (run_from / "plan.md").write_text("# Plan\nstep one\n")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert run_main(monkeypatch, ["plan.md"]) == 0
    assert list(jobs) == ["opencode"]
    assert jobs["opencode"].mode == "plan"  # the second run, over the plan default too


SECRET = "SECRET-ghp-DEADBEEF"


def repo_with_a_credentialed_remote(tmp_path, body):
    """A repo whose .git/config holds a secret, plus the project config under test."""
    run_from = make_repo(tmp_path, body)
    (run_from / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = https://x-token:{SECRET}@github.com/acme/private.git\n'
    )
    return run_from


@pytest.mark.parametrize("item", [".git/config", ".GIT/config", "./.Git/config"])
def test_a_repo_config_cannot_get_git_metadata_into_the_payload(monkeypatch, tmp_path, item):
    # The end-to-end form of the confinement: whatever the config names, the secret in
    # .git/config must never reach what a backend is handed.
    monkeypatch.chdir(repo_with_a_credentialed_remote(tmp_path, f"docs = ['{item}']\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    assert jobs == {}


def test_a_repo_standards_doc_cannot_link_git_metadata_into_the_payload(monkeypatch, tmp_path):
    # One markdown hop out of a committed doc: the doc is read, the link into .git is not.
    run_from = repo_with_a_credentialed_remote(tmp_path, "docs = true\n")
    (run_from / "llms.txt").write_text("# llms\nno bare excepts\n[cfg](.GIT/config)\n")
    track(run_from, "llms.txt")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert SECRET not in jobs["claude"].docs_content
    assert "no bare excepts" in jobs["claude"].docs_content


ENV_SECRET = "SECRET-env-DEADBEEF"
PEM_SECRET = "SECRET-pem-DEADBEEF"


def repo_with_local_secrets(tmp_path, body):
    """A repo carrying the project config under test, plus gitignored local secrets.

    Tracked: .gitignore, STANDARDS.md, llms.txt — the last two linking to the secrets, the
    way a hostile repo would. Untracked: .env and id.pem, which are the developer's.
    """
    run_from = make_repo(tmp_path, body)
    (run_from / ".gitignore").write_text(".env\n*.pem\n")
    (run_from / ".env").write_text(f"AWS_SECRET_ACCESS_KEY={ENV_SECRET}\n")
    (run_from / "id.pem").write_text(f"-----BEGIN KEY-----\n{PEM_SECRET}\n")
    (run_from / "STANDARDS.md").write_text("# Standards\nsmall diffs\n[a](.env) [b](id.pem)\n")
    (run_from / "llms.txt").write_text("# llms\nno bare excepts\n[a](.env) [b](id.pem)\n")
    track(run_from, ".gitignore", "STANDARDS.md", "llms.txt")
    return run_from


@pytest.mark.parametrize("value", ["['.env']", "['id.pem']", "['.env', 'id.pem']"])
def test_a_repo_config_cannot_name_an_untracked_local_file(monkeypatch, tmp_path, capsys, value):
    monkeypatch.chdir(repo_with_local_secrets(tmp_path, f"docs = {value}\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 1
    err = capsys.readouterr().err
    assert "refusing docs path" in err
    assert "the repository tracks it" in err
    assert jobs == {}  # refused before any backend ran


@pytest.mark.parametrize("body", ["docs = true\n", "docs = ['STANDARDS.md']\n"])
def test_a_repo_chosen_doc_cannot_link_an_untracked_local_file_into_the_payload(
    monkeypatch, tmp_path, body
):
    monkeypatch.chdir(repo_with_local_secrets(tmp_path, body))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    docs = jobs["claude"].docs_content
    assert ENV_SECRET not in docs and PEM_SECRET not in docs
    assert "small diffs" in docs or "no bare excepts" in docs  # the doc itself still applies


def test_a_user_config_docs_true_applies_the_projects_standards(monkeypatch, tmp_path):
    # Not the doc sitting beside ~/.config/rocket-review/config.toml, which is no project's
    # standards and would otherwise ride into every repo the user reviews.
    run_from = repo_with_local_secrets(tmp_path, None)
    write_user_config("docs = true\n")
    beside_the_user_config = config.user_config_path().parent / "llms.txt"
    beside_the_user_config.write_text("STANDARDS FROM THE HOME DIRECTORY\n")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    docs = jobs["claude"].docs_content
    assert "no bare excepts" in docs
    assert "HOME DIRECTORY" not in docs
    assert ENV_SECRET not in docs and PEM_SECRET not in docs


def test_a_user_config_docs_true_applies_them_from_a_subdirectory_too(monkeypatch, tmp_path):
    run_from = repo_with_local_secrets(tmp_path, None)
    nested = run_from / "src" / "deep"
    nested.mkdir(parents=True)
    write_user_config("docs = true\n")
    monkeypatch.chdir(nested)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert "no bare excepts" in jobs["claude"].docs_content


def test_outside_a_repo_a_project_config_keeps_confinement_alone(monkeypatch, tmp_path):
    # No repository, so nothing is tracked and the tracked rule cannot apply; the
    # directory confinement and the .git guard are what remain.
    loose = tmp_path / "loose"
    loose.mkdir()
    write(loose, "docs = ['standards.md']\n")
    (loose / "standards.md").write_text("loose standards\n")
    monkeypatch.chdir(loose)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert "loose standards" in jobs["claude"].docs_content


@pytest.mark.parametrize("target,secret", [(".env", ENV_SECRET), (".git/config", "GIT-SECRET")])
@pytest.mark.parametrize("layer_body", [("project", "docs = true\n"), ("user", "docs = true\n")])
def test_a_tracked_symlink_never_reaches_the_payload(
    monkeypatch, tmp_path, target, secret, layer_body
):
    # The route that had no payload-level test is where the fourth bypass lived: a symlink
    # the repository tracks is not a licence to read whatever it points at.
    which, body = layer_body
    run_from = repo_with_local_secrets(tmp_path, body if which == "project" else None)
    (run_from / ".git" / "config").write_text(
        '[remote "o"]\n\turl = https://x:GIT-SECRET@h/r\n'
    )
    if which == "user":
        write_user_config(body)
    (run_from / "llms.txt").unlink()
    (run_from / "llms.txt").symlink_to(target)
    track(run_from, "llms.txt")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert jobs["claude"].docs_content is None or secret not in jobs["claude"].docs_content


def test_config_docs_true_discovers_beside_the_config_not_the_cwd(monkeypatch, tmp_path):
    # A project standardising on `docs = true` applies to everyone who runs rr in it, not
    # only to whoever happens to be standing at the repo root.
    run_from = make_repo(tmp_path, "docs = true\n", subdir="src/deep")
    (tmp_path / "repo" / "llms.txt").write_text("project standards: small diffs\n")
    track(tmp_path / "repo", "llms.txt")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert "small diffs" in jobs["claude"].docs_content


def test_config_docs_true_is_silent_where_a_project_has_no_standards_doc(
    monkeypatch, tmp_path, capsys
):
    # A standing preference means "use this project's standards doc if it has one"; the
    # typed flag stays an error, since it asked for something specific that is not there.
    monkeypatch.chdir(make_repo(tmp_path, "docs = true\n"))
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert jobs["claude"].docs_content is None
    assert "--docs given without paths" not in capsys.readouterr().err


def test_the_docs_flag_still_errors_when_discovery_finds_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(make_repo(tmp_path, "docs = true\n"))
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--docs"]) == 1
    assert "--docs given without paths" in capsys.readouterr().err


def test_config_docs_and_the_llms_alias_combine(monkeypatch, tmp_path):
    run_from = make_repo(tmp_path, "docs = ['standards.md']\n")
    (run_from / "standards.md").write_text("standards: prefer small diffs\n")
    (run_from / "llms.txt").write_text("llms: no bare excepts\n")
    track(run_from, "standards.md")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--llms"]) == 0
    docs = jobs["claude"].docs_content
    assert "prefer small diffs" in docs
    assert "no bare excepts" in docs
