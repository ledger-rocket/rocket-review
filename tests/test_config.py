import subprocess
import types
from pathlib import Path

import pytest

from rocket_review import cli, config
from rocket_review.cli import main

PROJECT = Path("/repo/.rocket-review.toml")
USER = Path("/home/u/.config/rocket-review/config.toml")


def layer(path, **values):
    return config.Layer(path=path, values=values)


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
    """A git repo with an optional project config; returns the directory to run from."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    if body is not None:
        write(repo, body)
    if not subdir:
        return repo
    nested = repo / subdir
    nested.mkdir(parents=True)
    return nested


def loaded(path: Path, *, confine_docs: bool = False) -> dict:
    return config.load_file(path, confine_docs=confine_docs).values


def error_from(path: Path, *, confine_docs: bool = False) -> str:
    with pytest.raises(config.ConfigError) as e:
        config.load_file(path, confine_docs=confine_docs)
    return str(e.value)


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
                config.load_file(write(tmp_path, body + "\n"), confine_docs=True)
            except config.ConfigError:
                pass  # the accepted outcome; the valid combinations simply load
            except Exception as e:
                crashed.append(f"{body!r} -> {type(e).__name__}: {e}")
    assert crashed == []


def test_docs_paths_resolve_against_the_config_file(tmp_path):
    path = write(tmp_path, "docs = ['standards/llms.txt']\n")
    assert loaded(path)["docs"] == [str(tmp_path.resolve() / "standards" / "llms.txt")]


def test_a_project_config_may_not_name_docs_outside_itself(tmp_path):
    # The project file comes from the repository, so an absolute or escaping path would let
    # a cloned repo have any file on the machine read and sent to a backend.
    path = write(tmp_path / "repo", "docs = ['../../../etc/passwd']\n")
    message = error_from(path, confine_docs=True)
    assert "resolves outside" in message
    assert "may only name docs inside its own directory" in message


def test_a_project_config_may_not_name_docs_inside_dot_git(tmp_path):
    # Inside the repo but not of it: .git holds local state a clone does not control, and a
    # named doc is copied into the prompt verbatim.
    path = write(tmp_path / "repo", "docs = ['.git/config']\n")
    assert "is inside .git" in error_from(path, confine_docs=True)


def test_confinement_is_wired_to_the_project_file_and_only_it(tmp_path):
    # The whole point of the confinement, exercised through discovery rather than by
    # passing the flag by hand: swapping the two call sites has to fail here.
    outside = tmp_path / "outside.md"
    outside.write_text("secrets\n")
    run_from = make_repo(tmp_path, f"docs = ['{outside}']\n")
    with pytest.raises(config.ConfigError, match="resolves outside"):
        config.load(no_config=False, cwd=run_from)

    (run_from / config.PROJECT_CONFIG_NAME).unlink()
    write_user_config(f"docs = ['{outside}']\n")
    layers = config.load(no_config=False, cwd=run_from)
    assert layers[0].values["docs"] == [str(outside)]  # the user's own file may name it


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


def test_a_flag_conflict_the_user_typed_names_no_file(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(make_repo(tmp_path))
    fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--fail-on", "high"]) == 1
    assert "is set in" not in capsys.readouterr().err


def test_config_docs_true_auto_discovers(monkeypatch, tmp_path):
    run_from = make_repo(tmp_path, "docs = true\n")
    (run_from / "llms.txt").write_text("project standards: no bare excepts\n")
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert "no bare excepts" in jobs["claude"].docs_content


def test_config_docs_paths_are_relative_to_the_config_file(monkeypatch, tmp_path):
    # Read from a subdirectory, so a cwd-relative reading of the path would miss the file.
    run_from = make_repo(tmp_path, "docs = ['docs/standards.md']\n", subdir="src")
    (tmp_path / "repo" / "docs").mkdir()
    (tmp_path / "repo" / "docs" / "standards.md").write_text("standards: prefer small diffs\n")
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
    monkeypatch.chdir(run_from)
    jobs = fake_backends(monkeypatch)
    assert run_main(monkeypatch, ["--diff", "--llms"]) == 0
    docs = jobs["claude"].docs_content
    assert "prefer small diffs" in docs
    assert "no bare excepts" in docs
