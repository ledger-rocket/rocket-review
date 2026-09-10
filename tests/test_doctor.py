import shutil
import subprocess
import types
from pathlib import Path

import pytest

from rocket_review import config, doctor
from rocket_review.cli import main
from rocket_review.config import DEFAULT_BACKEND_BY_MODE


#: The real probe, kept from before the autouse fixture stubs it out for every other test.
REAL_PROBE = doctor._probe


def probe_result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture(autouse=True)
def no_real_probes(monkeypatch):
    """Every backend CLI present and authenticated, unless a test says otherwise.

    A doctor test must never reach the developer's own codex/claude installs: what those
    answer decides the exit code, so an unpatched probe would make the suite report on the
    machine running it rather than on the code.
    """
    monkeypatch.setattr(doctor, "missing_binary", lambda name: None)
    monkeypatch.setattr(doctor, "which", lambda binary: f"/usr/local/bin/{binary}")
    monkeypatch.setattr(doctor, "_probe", lambda cmd: probe_result())


def run_doctor(argv=()):
    return doctor.run_doctor(list(argv))


def run_init(argv=()):
    return doctor.run_init(list(argv))


# --- what must not change -------------------------------------------------------------


def patch_backends(monkeypatch, reviews):
    """The fake-backend swap test_cli uses, so a review runs without a backend CLI."""
    fakes = {
        name: types.SimpleNamespace(review=lambda job, out=out: out)
        for name, out in reviews.items()
    }
    monkeypatch.setattr("rocket_review.cli.BACKENDS", fakes)
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    # The per-mode default is chosen by the real availability check, which asks PATH.
    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)


def drive_main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    try:
        main()
    except SystemExit as e:
        return e.code if e.code is not None else 0
    return 0


def test_diff_still_parses(monkeypatch, capsys):
    patch_backends(monkeypatch, {"claude": "review body"})
    assert drive_main(monkeypatch, ["--diff"]) == 0
    assert "review body" in capsys.readouterr().out


def test_a_file_named_like_a_subcommand_is_still_reviewed(monkeypatch, capsys):
    Path("doctor.md").write_text("a plan\n")
    patch_backends(monkeypatch, {"codex": "plan review"})
    assert drive_main(monkeypatch, ["doctor.md"]) == 0
    assert "plan review" in capsys.readouterr().out


def test_a_file_named_exactly_like_a_subcommand_is_reviewed_through_a_path(
    monkeypatch, capsys
):
    """The one cost of dispatching on argv[0]: `./doctor` is the way to review that file."""
    Path("doctor").write_text("a plan\n")
    patch_backends(monkeypatch, {"claude": "file review"})
    assert drive_main(monkeypatch, ["./doctor"]) == 0
    assert "file review" in capsys.readouterr().out


def test_version_flag_is_untouched(monkeypatch, capsys):
    assert drive_main(monkeypatch, ["--version"]) == 0
    assert "rr " in capsys.readouterr().out


# --- rr init --------------------------------------------------------------------------


def test_init_writes_a_file_the_loader_accepts(capsys):
    assert run_init() == 0
    path = config.user_config_path()
    assert path is not None and path.is_file()
    assert str(path) in capsys.readouterr().out

    settings = config.resolve(
        dict.fromkeys(config.FLAG_KEYS), config.load(no_config=False, cwd=Path.cwd())
    )
    assert settings.effort == "medium"
    assert settings.backends == DEFAULT_BACKEND_BY_MODE
    assert settings.models == {"codex": "gpt-6-astra", "claude": "claude-fable-5"}
    assert settings.sources["effort"] == str(path)


def test_init_recommends_high_effort_for_the_final_pass():
    run_init()
    assert "--effort high" in config.user_config_path().read_text(encoding="utf-8")


def test_init_creates_the_parent_directories(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nested" / "deeper"))
    assert run_init() == 0
    assert config.user_config_path().is_file()


def test_init_never_overwrites_without_force(capsys):
    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("effort = 'high'\n", encoding="utf-8")

    assert run_init() == 0
    assert path.read_text(encoding="utf-8") == "effort = 'high'\n"
    assert str(path) in capsys.readouterr().out


def test_init_force_overwrites():
    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("effort = 'high'\n", encoding="utf-8")

    assert run_init(["--force"]) == 0
    assert "gpt-6-astra" in path.read_text(encoding="utf-8")


def test_init_without_a_home_fails(monkeypatch, capsys):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(config, "user_config_path", lambda: None)
    assert run_init() == 1
    assert "no user config path" in capsys.readouterr().err


def test_init_is_reachable_through_main(monkeypatch):
    monkeypatch.setattr("sys.argv", ["rr", "init"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    assert config.user_config_path().is_file()


# --- rr doctor: the report ------------------------------------------------------------


def test_reports_version_and_install_channel(capsys):
    assert run_doctor() == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert "rr" in first
    assert any(channel in first for channel in ("pipx", "brew", "pip", "unknown"))


def test_reports_the_xdg_user_config_path(capsys):
    path = config.user_config_path()
    run_doctor()
    out = capsys.readouterr().out
    assert str(path) in out
    assert "user config" in out


def test_reports_an_existing_user_config_as_ok(capsys):
    run_init()
    run_doctor()
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "user config" in ln)
    assert line.startswith("ok")


def test_reports_a_missing_user_config(capsys):
    run_doctor()
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "user config" in ln)
    assert line.startswith("missing")
    assert "rr init" in out


def test_reports_the_project_config_found_from_cwd(capsys):
    project = Path.cwd() / config.PROJECT_CONFIG_NAME
    project.write_text('effort = "low"\n', encoding="utf-8")
    run_doctor()
    out = capsys.readouterr().out
    assert str(project) in out
    assert "project config" in out


def test_settings_name_the_file_that_set_them(capsys):
    project = Path.cwd() / config.PROJECT_CONFIG_NAME
    project.write_text('effort = "low"\ntimeout = 1800\n', encoding="utf-8")
    run_doctor()
    out = capsys.readouterr().out
    effort = next(ln for ln in out.splitlines() if " effort" in ln)
    assert "low" in effort and str(project) in effort
    timeout = next(ln for ln in out.splitlines() if " timeout" in ln)
    assert "1800" in timeout
    fail_on = next(ln for ln in out.splitlines() if " fail_on" in ln)
    assert config.BUILT_IN in fail_on


def test_reports_the_backend_each_mode_uses(capsys):
    run_doctor()
    out = capsys.readouterr().out
    for mode, backend in DEFAULT_BACKEND_BY_MODE.items():
        line = next(ln for ln in out.splitlines() if f"backends.{mode}" in ln)
        assert backend in line


def test_reports_the_configured_model_pin(capsys):
    run_init()
    run_doctor()
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "backend codex" in ln)
    assert "gpt-6-astra" in line


def test_reports_no_model_pin_as_the_backend_default(capsys):
    run_doctor()
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "backend claude" in ln)
    assert "default" in line


def test_only_the_backends_the_modes_use_are_probed(capsys):
    run_doctor()
    out = capsys.readouterr().out
    assert "backend codex" in out and "backend claude" in out
    assert "backend opencode" not in out


def test_backend_flag_chooses_what_is_reported(capsys):
    assert run_doctor(["--backend", "opencode"]) == 0
    out = capsys.readouterr().out
    assert "backend opencode" in out
    assert "backend claude" not in out


def test_backend_flag_accepts_a_model_suffix(capsys):
    assert run_doctor(["--backend", "codex:gpt-6-astra"]) == 0
    assert "backend codex" in capsys.readouterr().out


def test_backend_flag_rejects_an_unknown_name(capsys):
    assert run_doctor(["--backend", "gemini"]) == 2
    assert "unknown backend" in capsys.readouterr().err


# --- rr doctor: the verdict -----------------------------------------------------------


def test_everything_present_and_authenticated_exits_0(capsys):
    assert run_doctor() == 0
    out = capsys.readouterr().out
    assert "failed" not in out


def test_a_missing_cli_exits_1_with_the_install_hint(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor, "missing_binary", lambda name: "npm i -g thing" if name == "claude" else None
    )
    assert run_doctor() == 1
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "backend claude" in ln)
    assert line.startswith("missing")
    assert "npm i -g thing" in out


def test_a_backend_the_modes_do_not_use_cannot_fail_the_run(monkeypatch):
    monkeypatch.setattr(
        doctor, "missing_binary", lambda name: "npm i -g thing" if name == "opencode" else None
    )
    assert run_doctor() == 0


def test_a_refused_login_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor, "_probe", lambda cmd: probe_result(returncode=1, stderr="Not logged in")
    )
    assert run_doctor() == 1
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "backend codex" in ln)
    assert "failed" in line


def test_an_unreadable_probe_is_unknown_not_failed(monkeypatch, capsys):
    """A CLI that cannot answer is not a CLI that answered no."""
    monkeypatch.setattr(doctor, "_probe", lambda cmd: None)
    assert run_doctor() == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "backend codex" in ln)
    assert "unknown" in line and "failed" not in line


def test_an_unrecognised_status_command_is_unknown_not_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor,
        "_probe",
        lambda cmd: probe_result(returncode=2, stderr="error: unrecognized subcommand 'login'"),
    )
    assert run_doctor() == 0
    assert "unknown" in capsys.readouterr().out


@pytest.mark.parametrize(
    "spoken", ['{\n  "loggedIn": false\n}', '{"loggedIn":false}', "Not logged in."]
)
def test_a_refusal_is_read_however_it_is_spelled(monkeypatch, capsys, spoken):
    """A logged-out CLI that exits 0 must not read as ok because of its whitespace."""
    monkeypatch.setattr(doctor, "_probe", lambda cmd: probe_result(stdout=spoken))
    assert run_doctor() == 1
    assert "failed" in capsys.readouterr().out


def test_a_backend_without_a_status_command_is_unknown(monkeypatch, capsys):
    """claude ships one; a backend that does not must still not read as broken."""
    monkeypatch.setattr(doctor, "AUTH_PROBES", {})
    assert run_doctor() == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "backend claude" in ln)
    assert "unknown" in line


def test_a_missing_user_config_is_not_a_failure():
    assert config.user_config_path().exists() is False
    assert run_doctor() == 0


def test_a_broken_config_is_failed_and_exits_1(capsys):
    (Path.cwd() / config.PROJECT_CONFIG_NAME).write_text("timeout = 'soon'\n", encoding="utf-8")
    assert run_doctor() == 1
    out = capsys.readouterr().out
    assert "failed" in out
    assert "timeout" in out


def test_an_internal_error_exits_2(monkeypatch, capsys):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor, "_install_channel", boom)
    assert run_doctor() == 2
    assert "kaboom" in capsys.readouterr().err


def test_fixes_are_printed_for_every_gap(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor, "missing_binary", lambda name: "npm i -g thing" if name == "claude" else None
    )
    monkeypatch.setattr(
        doctor,
        "_probe",
        lambda cmd: probe_result(returncode=1, stderr="Not logged in"),
    )
    assert run_doctor() == 1
    out = capsys.readouterr().out
    fixes = out.split("fixes:")[1]
    assert "npm i -g thing" in fixes
    assert "codex login" in fixes
    assert "rr init" in fixes


def test_no_fixes_section_when_nothing_is_wrong(capsys):
    run_init()
    assert run_doctor() == 0
    assert "fixes:" not in capsys.readouterr().out


# --- rr doctor: settings a review would refuse to start with ---------------------------


@pytest.mark.parametrize("key,value", [("fail_on", '"high"'), ("full", "true")])
def test_a_gate_without_json_is_failed(capsys, key, value):
    """rr itself exits 1 on these, so a doctor that passed them would pass a dead host."""
    (Path.cwd() / config.PROJECT_CONFIG_NAME).write_text(
        f"{key} = {value}\n", encoding="utf-8"
    )
    assert run_doctor() == 1
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("failed"))
    assert key in line and "json" in line
    assert "json = true" in out.split("fixes:")[1]


def test_a_gate_with_json_is_fine(capsys):
    (Path.cwd() / config.PROJECT_CONFIG_NAME).write_text(
        'fail_on = "high"\njson = true\n', encoding="utf-8"
    )
    assert run_doctor() == 0


def test_effort_with_opencode_is_failed(capsys):
    (Path.cwd() / config.PROJECT_CONFIG_NAME).write_text(
        'effort = "high"\n[backends]\ndefault = "opencode"\n', encoding="utf-8"
    )
    assert run_doctor() == 1
    assert "opencode" in next(
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("failed")
    )


def test_effort_without_opencode_is_fine():
    (Path.cwd() / config.PROJECT_CONFIG_NAME).write_text('effort = "high"\n', encoding="utf-8")
    assert run_doctor() == 0


def test_effort_conflicts_only_with_the_backends_checked(capsys):
    """--backend narrows what runs here, so it narrows what can conflict."""
    (Path.cwd() / config.PROJECT_CONFIG_NAME).write_text(
        'effort = "high"\n[backends]\ndefault = "opencode"\n', encoding="utf-8"
    )
    assert run_doctor(["--backend", "codex"]) == 0


# --- rr doctor: --quiet ---------------------------------------------------------------


def test_quiet_prints_nothing_when_healthy(capsys):
    assert run_doctor(["--quiet"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_quiet_prints_nothing_when_broken(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "missing_binary", lambda name: "npm i -g thing")
    assert run_doctor(["--quiet"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_quiet_prints_nothing_on_an_internal_error(monkeypatch, capsys):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor, "_install_channel", boom)
    assert run_doctor(["--quiet"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_doctor_is_reachable_through_main(monkeypatch):
    monkeypatch.setattr("sys.argv", ["rr", "doctor", "--quiet"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0


# --- the api backend, which has no CLI ------------------------------------------------


def api_only():
    """Point every mode at api, the one backend that is an SDK rather than a CLI."""
    project = Path.cwd() / config.PROJECT_CONFIG_NAME
    project.write_text('[backends]\ndefault = "api"\n', encoding="utf-8")


def test_api_without_a_key_is_failed(monkeypatch, capsys):
    api_only()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        doctor, "_api_ready", lambda: (False, "no OPENAI_API_KEY", doctor.API_KEY_FIX)
    )
    assert run_doctor() == 1
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "backend api" in ln)
    assert "failed" in line
    assert "OPENAI_API_KEY" in out.split("fixes:")[1]


def test_a_missing_sdk_is_fixed_by_installing_the_sdk(monkeypatch, capsys):
    """The key and the SDK fail separately; the repair for one does nothing for the other."""
    api_only()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("rocket_review.backends.api._load_env_file", lambda: None)
    monkeypatch.setattr("rocket_review.backends._openai_sdk_installed", lambda: False)
    assert run_doctor() == 1
    fixes = capsys.readouterr().out.split("fixes:")[1]
    assert "openai" in fixes and "OPENAI_API_KEY" not in fixes


def test_api_with_a_key_is_ok(monkeypatch, capsys):
    api_only()
    monkeypatch.setattr(doctor, "_api_ready", lambda: (True, "", ""))
    assert run_doctor() == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "backend api" in ln)
    assert line.startswith("ok")


def test_the_api_probe_never_shells_out(monkeypatch):
    """It is an SDK and a key, so a subprocess would be the wrong question entirely."""
    api_only()

    def refuse(cmd):
        raise AssertionError(f"api must not be probed with a subprocess: {cmd}")

    monkeypatch.setattr(doctor, "_probe", refuse)
    monkeypatch.setattr(doctor, "_api_ready", lambda: (True, "", ""))
    assert run_doctor() == 0


# --- the probe itself -----------------------------------------------------------------


def test_probe_is_bounded_and_reads_no_input(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs, cmd=cmd)
        return probe_result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert REAL_PROBE(["codex", "login", "status"]) is not None
    assert 0 < seen["timeout"] <= 10
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["capture_output"] is True


def test_probe_swallows_a_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert REAL_PROBE(["codex", "login", "status"]) is None


def test_probe_swallows_a_missing_binary(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert REAL_PROBE(["codex", "login", "status"]) is None
