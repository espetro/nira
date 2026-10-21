"""Executor honesty tests: changed/unchanged/failed, verify, run-as, convergence.

Everything runs against fakes (no ssh, no sudo); the single @local pyinfra
test is marked ``integration``.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nira.cli.main import app
from nira.core.bundle import REGISTRY, Bundle, Requirement, RequirementError
from nira.core.model import HostConfig, Op, Plan, PlanEntry, Status
from nira.core.packages import PackageError, resolve_package
from nira.ops import executors
from nira.ops.apply import apply_plan
from nira.ops.executors import ExecCtx, OpSpec, Outcome, build_ops

runner = CliRunner()


# ------------------------------------------------------------------ fakes


class FakeSession:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend

    def exists(self, path: str) -> bool:
        return path in self.backend.existing

    def run(self, specs: list[OpSpec], verify_cmd: str | None) -> Outcome:
        self.backend.runs.append((specs, verify_cmd))
        return self.backend.outcome(specs, verify_cmd)


class FakeBackend:
    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome or (lambda specs, v: Outcome(changed=True, verified=True))
        self.runs: list[tuple[list[OpSpec], str | None]] = []
        self.existing: set[str] = set()
        self.sessions = 0

    @contextlib.contextmanager
    def session(self) -> Iterator[FakeSession]:
        self.sessions += 1
        yield FakeSession(self)


def entry(kind: str, name: str, params: dict | None = None, status: Status = Status.MISSING):
    return PlanEntry("b", name, Op.INSTALL, status, kind=kind, params=params or {})


def run_exec(backend: FakeBackend, entries: list[Any], data: dict | None = None) -> list[dict]:
    results: list[dict] = []
    host = ("h", {"os": "linux", "ssh_user": "deb", **(data or {})})
    with patch.object(executors, "_make_backend", lambda h: backend):
        executors.execute(host, entries, results)
    return results


# ------------------------------------------------------------------ honest results


def test_changed_unchanged_failed_from_actual_outcomes() -> None:
    outcomes = {
        "jq": Outcome(changed=True, verified=True),
        "git": Outcome(changed=False, verified=True),
        "rsync": Outcome(error="apt.packages: E: unable to locate package"),
    }

    def outcome(specs: list[OpSpec], _v: str | None) -> Outcome:
        return outcomes[specs[0].kwargs["packages"][0]]

    res = run_exec(
        FakeBackend(outcome),
        [entry("package", "jq"), entry("package", "git"), entry("package", "rsync")],
    )
    assert [r["result"] for r in res] == ["changed", "unchanged", "failed"]
    assert "unable to locate" in res[2]["detail"]
    assert res[0]["verify"] == "ok"


def test_verify_failure_makes_entry_failed() -> None:
    backend = FakeBackend(lambda s, v: Outcome(changed=True, verify_error="not active"))
    (r,) = run_exec(backend, [entry("package", "jq")])
    assert r["result"] == "failed" and r["verify"] == "failed"
    assert "verify failed" in r["detail"]
    assert backend.runs[0][1]  # a verify command was actually issued


def test_unreachable_host_fails_all_without_retrying() -> None:
    class Dead(FakeBackend):
        @contextlib.contextmanager
        def session(self) -> Iterator[FakeSession]:
            self.sessions += 1
            raise ConnectionError("host h unreachable")
            yield  # pragma: no cover

    dead = Dead()
    res = run_exec(dead, [entry("package", "jq"), entry("package", "git")])
    assert [r["result"] for r in res] == ["failed", "failed"]
    assert dead.sessions == 1


def test_report_only_custom_check_is_skipped() -> None:
    (r,) = run_exec(
        FakeBackend(), [entry("custom", "orca-pairing", {"check": "config-presence-only"})]
    )
    assert r["result"] == "skipped"


def test_each_entry_gets_its_own_session() -> None:
    backend = FakeBackend()
    run_exec(backend, [entry("package", "jq"), entry("package", "git")])
    assert backend.sessions == 2  # fresh pyinfra State per entry


FLEET_HOSTS = (
    "HOSTS = [{'name': 'h1', 'ssh_user': 'deb', 'address': 'h1.example',"
    " 'os': 'linux', 'bundles': ['x'], 'protected_components': []}]\n"
)


def test_cli_exits_nonzero_when_verify_fails(tmp_path: Path) -> None:
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "hosts.py").write_text(FLEET_HOSTS)
    plan = Plan(host="h1")
    plan.add(entry("package", "jq"))
    backend = FakeBackend(lambda s, v: Outcome(changed=True, verify_error="jq: command not found"))
    with (
        patch("nira.core.planner.build_plan", return_value=plan),
        patch.object(executors, "_make_backend", lambda h: backend),
        patch("nira.ops.inventory.host_inventory", lambda h: ("h1", {"os": "linux"})),
    ):
        r = runner.invoke(app, ["apply", "--host", "h1", "--fleet", str(tmp_path), "--json"])
    assert r.exit_code == 1, r.output
    assert '"failures": 1' in r.output


def test_cli_exits_zero_when_everything_verifies(tmp_path: Path) -> None:
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "hosts.py").write_text(FLEET_HOSTS)
    plan = Plan(host="h1")
    plan.add(entry("package", "jq"))
    with (
        patch("nira.core.planner.build_plan", return_value=plan),
        patch.object(executors, "_make_backend", lambda h: FakeBackend()),
        patch("nira.ops.inventory.host_inventory", lambda h: ("h1", {"os": "linux"})),
    ):
        r = runner.invoke(app, ["apply", "--host", "h1", "--fleet", str(tmp_path)])
    assert r.exit_code == 0, r.output


# ------------------------------------------------------------------ protection


def test_protected_units_never_get_file_ops() -> None:
    backend = FakeBackend()
    plan = Plan(host="h", protected_patterns=("brioso-*",))
    unit = {"unit_path": "/etc/systemd/system/x.service", "unit_content": "[Unit]\n"}
    for name in ("brioso-api", "bifrost", "orca.service", "web-services"):
        plan.add(entry("service", name, unit))
    plan.add(entry("service", "plain", unit))
    with (
        patch.object(executors, "_make_backend", lambda h: backend),
        patch("nira.ops.inventory.host_inventory", lambda h: ("h", {"os": "linux"})),
    ):
        results = apply_plan(plan)
    touched = {s.label for specs, _ in backend.runs for s in specs}
    assert touched == {"unit /etc/systemd/system/x.service", "service plain"}
    protected = [r for r in results if r["component"] != "plain"]
    assert len(protected) == 4 and all("protected" in r["detail"] for r in protected)


def test_existing_unit_not_overwritten_without_adopt() -> None:
    ctx = ExecCtx(os="linux", ssh_user="deb")
    unit = "/etc/systemd/system/x.service"
    params = {"unit_path": unit, "unit_content": "[Unit]\n"}
    built = build_ops("service", "x", params, ctx, exists=lambda p: True)
    assert [s.op for s in built.specs] == ["systemd.service"]


# ------------------------------------------------------------------ run-as / sudo


def test_agent_scope_runs_as_agent_user_and_admin_uses_sudo() -> None:
    ctx = ExecCtx(os="linux", ssh_user="deb", agent_user="agent")
    mise = build_ops("custom", "mise:uv", {"mise": True, "tool": "uv"}, ctx).specs[0]
    assert mise.kwargs["_sudo"] is True and mise.kwargs["_sudo_user"] == "agent"
    cfg = build_ops("file", "/home/agent/.config/a", {"content": "x"}, ctx).specs[0]
    assert cfg.kwargs["_sudo_user"] == "agent"
    apt = build_ops("package", "jq", {}, ctx).specs[0]
    assert apt.op == "apt.packages" and apt.kwargs["_sudo"] is True
    assert "_sudo_user" not in apt.kwargs
    svc = build_ops("service", "x", {}, ctx).specs[-1]
    assert svc.op == "systemd.service" and svc.kwargs["_sudo"] is True
    assert "_sudo_user" not in svc.kwargs


def test_no_agent_user_means_no_sudo_user_for_agent_scope() -> None:
    ctx = ExecCtx(os="linux", ssh_user="deb")
    spec = build_ops("custom", "mise:uv", {"mise": True, "tool": "uv"}, ctx).specs[0]
    assert "_sudo" not in spec.kwargs


def test_explicit_scope_param_is_honored() -> None:
    ctx = ExecCtx(os="linux", ssh_user="deb", agent_user="agent")
    admin_file = build_ops("file", "/etc/x", {"content": "x", "scope": "admin"}, ctx).specs[0]
    assert admin_file.kwargs["_sudo"] is True and "_sudo_user" not in admin_file.kwargs


def test_verify_command_runs_as_agent_user() -> None:
    backend = FakeBackend()
    run_exec(
        backend,
        [entry("custom", "mise:uv", {"mise": True, "tool": "uv"})],
        data={"agent_user": "agent"},
    )
    verify = backend.runs[0][1]
    assert verify and "sudo -n -u agent" in verify and "uv --version" in verify


# ------------------------------------------------------------------ mise specs


def test_mise_uses_explicit_specs_never_fx_shortname() -> None:
    ctx = ExecCtx(os="linux", ssh_user="deb")
    spec = build_ops(
        "custom", "mise:fx", {"mise": True, "tool": "github:vercel-labs/fx"}, ctx
    ).specs[0]
    cmd = spec.kwargs["commands"][0]
    assert "mise use -g github:vercel-labs/fx" in cmd
    assert "mise use -g fx" not in cmd
    with pytest.raises(RequirementError, match="ambiguous"):
        build_ops("custom", "mise:fx", {"mise": True, "tool": "fx"}, ctx)
    maki = build_ops(
        "custom", "mise:maki", {"mise": True, "tool": "github:tontinton/maki"}, ctx
    ).specs[0]
    assert "mise use -g github:tontinton/maki" in maki.kwargs["commands"][0]


def test_mise_probe_goes_through_mise_not_dpkg() -> None:
    from nira.ops import probes

    ctx = probes.Ctx(os="linux", ssh_user="deb")
    snippet = probes.probe_snippet("custom", "mise:uv", {"mise": True, "tool": "uv"}, ctx)
    assert "mise where uv" in snippet and "dpkg" not in snippet
    assert ".local/bin" in probes.PATH_PRELUDE  # mise found without an interactive PATH


# ------------------------------------------------------------------ secrets / launchd / relink


def test_secret_sops_path_is_absolute_and_staged(tmp_path: Path) -> None:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "a.yaml").write_text("enc")
    ctx = ExecCtx(os="linux", ssh_user="deb", fleet_path=str(tmp_path))
    params = {"sops_file": "secrets/a.yaml", "consumer": "env", "key": "K"}
    specs = build_ops("secret", "K", params, ctx).specs
    put = specs[0]
    assert put.op == "files.put" and put.kwargs["src"] == str(tmp_path / "secrets" / "a.yaml")
    assert put.kwargs["dest"].startswith("/home/deb/.config/nira/secrets/")
    assert put.kwargs["mode"] == "0600"
    assert "sops exec-env /home/deb/.config/nira/secrets/a.yaml" in specs[1].kwargs["commands"][0]
    with pytest.raises(executors.ExecError):  # no fleet -> cannot resolve the sops file
        build_ops("secret", "K", params, ExecCtx(os="linux", ssh_user="deb"))


def test_launchctl_failures_are_not_hidden() -> None:
    ctx = ExecCtx(os="macos", ssh_user="me")
    params = {"plist_path": "/Library/LaunchDaemons/io.x.plist", "label": "io.x"}
    cmd = build_ops("service", "x", params, ctx).specs[0].kwargs["commands"][0]
    assert "2>/dev/null || true" not in cmd and "|| true" not in cmd
    assert "launchctl bootstrap" in cmd and "kickstart" in cmd
    assert "kickstart -k" not in cmd  # never restart a healthy daemon


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_skills_relink_skips_git_and_real_files(tmp_path: Path) -> None:
    src, dest = tmp_path / "agent-skills", tmp_path / "skills"
    (src / ".git").mkdir(parents=True)
    (src / "alpha").mkdir()
    (src / "beta").mkdir()
    dest.mkdir()
    (dest / "beta").write_text("my own file")  # pre-existing real file
    script = executors._relink_script(str(dest), str(src))

    def run() -> str:
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        ).stdout.strip()

    assert run() == executors.CHANGED_MARK
    assert (dest / "alpha").is_symlink() and (dest / "alpha").resolve() == (src / "alpha")
    assert not (dest / ".git").exists()
    assert (dest / "beta").read_text() == "my own file"  # not clobbered
    assert run() == executors.UNCHANGED_MARK  # idempotent


# ------------------------------------------------------------------ validation


def test_unknown_requirement_params_raise() -> None:
    from nira.core.bundle import validate_requirement

    with pytest.raises(RequirementError, match="unknown params"):
        validate_requirement(Requirement("jq", "package", {"versoin": "1"}), "linux")
    with pytest.raises(RequirementError, match="unknown params"):
        validate_requirement(Requirement("/x", "file", {"templat": "a"}), "linux")
    with pytest.raises(RequirementError, match="unknown requirement kind"):
        validate_requirement(Requirement("x", "widget"), "linux")
    with pytest.raises(RequirementError, match="scope"):
        validate_requirement(Requirement("jq", "package", {"scope": "root"}), "linux")


def test_package_name_map_per_os() -> None:
    assert resolve_package("fd", "linux").package == "fd-find"
    assert resolve_package("fd", "macos").package == "fd"
    assert resolve_package("mise", "linux").manager == "mise-installer"
    assert resolve_package("mise", "macos").manager == "brew"
    assert resolve_package("cask:raycast", "macos").manager == "cask"
    assert resolve_package("bifrost", "linux").manager == "mise"
    with pytest.raises(PackageError):
        resolve_package("cask:raycast", "linux")


def test_bad_package_fails_at_plan_time() -> None:
    from nira.core.planner import build_plan

    class _Bad(Bundle):
        name = "bad-pkg"

        def requirements(self, host: Any) -> Any:
            yield Requirement("cask:raycast", "package")

    REGISTRY["bad-pkg"] = _Bad
    try:
        host = HostConfig("h", "u", "a", "linux", bundles=["bad-pkg"])
        with pytest.raises(RequirementError, match="macOS-only"):
            build_plan(host)
    finally:
        del REGISTRY["bad-pkg"]


def test_priority_orders_bundles() -> None:
    from nira.core import planner

    class _Late(Bundle):
        name = "late"
        priority = 90

        def requirements(self, host: Any) -> Any:
            yield Requirement("a", "package")

    class _Early(Bundle):
        name = "early"
        priority = 0

        def requirements(self, host: Any) -> Any:
            yield Requirement("b", "package")

    REGISTRY.update({"late": _Late, "early": _Early})
    try:
        host = HostConfig("h", "u", "a", "linux", bundles=["late", "early"])
        with patch.object(planner, "collect_facts", lambda h, r=None: {}):
            plan = planner.build_plan(host)
        assert [e.bundle for e in plan.entries] == ["early", "late"]
    finally:
        del REGISTRY["late"], REGISTRY["early"]


# ------------------------------------------------------------------ convergence


def test_plan_apply_plan_converges_to_zero_gaps(tmp_path: Path) -> None:
    """plan -> apply (fake host that really changes) -> plan == no gaps, for every kind."""
    from nira.core import planner

    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "s.yaml").write_text("enc")

    class _All(Bundle):
        name = "conv"

        def requirements(self, host: Any) -> Any:
            yield Requirement("jq", "package")
            yield Requirement("fd", "package")
            yield Requirement("/home/deb/.config/a.conf", "file", {"content": "hello"})
            yield Requirement("/home/deb/skills-src", "directory")
            yield Requirement("S", "secret", {"sops_file": "secrets/s.yaml", "key": "S"})
            yield Requirement("mise:uv", "custom", {"mise": True, "tool": "uv"})
            yield Requirement(
                "mise:fx", "custom", {"mise": True, "tool": "github:vercel-labs/fx", "bin": "fx"}
            )
            yield Requirement(
                "svc", "service", {"unit_path": "/etc/systemd/system/svc.service",
                                   "unit_content": "[Unit]\n"},
            )  # fmt: skip

    world: set[tuple[str, str]] = set()
    kinds = {"package": "package", "file": "file", "dir": "directory", "repo": "directory",
             "service": "service", "secret": "secret", "custom": "custom"}  # fmt: skip

    class WorldBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__(self._apply)

        def _apply(self, specs: list[OpSpec], verify: str | None) -> Outcome:
            for s in specs:
                head, _, name = s.label.partition(" ")
                if head in kinds:
                    world.add((kinds[head], name))
            return Outcome(changed=True, verified=True)

    def fake_facts(host: Any, reqs: Any = None) -> dict:
        facts: dict[str, dict] = {k: {} for k in ("package", "file", "service", "secret",
                                                   "directory", "custom")}  # fmt: skip
        for r in reqs or []:
            facts[r.kind][r.name] = (r.kind, r.name) in world
        return facts

    REGISTRY["conv"] = _All
    host = HostConfig("h", "deb", "a", "linux", bundles=["conv"])
    backend = WorldBackend()
    try:
        with (
            patch.object(planner, "collect_facts", fake_facts),
            patch.object(executors, "_make_backend", lambda h: backend),
            patch(
                "nira.ops.inventory.host_inventory",
                lambda h: ("h", {"os": "linux", "ssh_user": "deb", "fleet_path": str(tmp_path)}),
            ),
        ):
            first = planner.build_plan(host)
            assert len(first.changes) == 8  # every kind is a gap on a fresh host
            results = apply_plan(first)
            assert {r["result"] for r in results} == {"changed"}, results
            second = planner.build_plan(host)
            assert second.changes == [], [(e.component, e.status) for e in second.changes]
    finally:
        del REGISTRY["conv"]


# ------------------------------------------------------------------ integration (@local pyinfra)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_pyinfra_local_backend_reports_changed_unchanged_failed(tmp_path: Path) -> None:
    """Real pyinfra against @local: no ssh, no sudo; skipped if pyinfra cannot run locally."""
    pytest.importorskip("pyinfra")
    data = {"os": "linux", "ssh_user": "ignored"}
    target = tmp_path / "d" / "f.txt"
    entries = [
        entry("file", str(target), {"content": "hi\n"}),
        entry("file", "/proc/nira-cannot-write-here/x", {"content": "x"}),
    ]
    results: list[dict] = []
    executors.execute(("@local", data), entries[:1], results)
    executors.execute(("@local", data), entries[:1], results)  # second time: nothing to do
    executors.execute(("@local", data), entries[1:], results)
    assert target.read_text() == "hi\n"
    assert [r["result"] for r in results] == ["changed", "unchanged", "failed"], results
    assert results[0]["verify"] == "ok"


def test_template_renders_strict_jinja_and_legacy(tmp_path):
    import pytest

    from nira.ops.executors import ExecCtx, ExecError, resolve_content

    fleet = tmp_path / "agent-config"
    fleet.mkdir()
    (fleet / "A.tmpl").write_text("{{ user }}@{{ tailnet_domain }} {{HOME}}\n")
    (fleet / "B.tmpl").write_text("{{ nope }}\n")
    ctx = ExecCtx(
        os="linux",
        ssh_user="debian",
        agent_user="own-agents",
        home="/home/own-agents",
        fleet_path=str(tmp_path),
        template_vars={"tailnet_domain": "t.ts.net"},
    )
    assert (
        resolve_content("A.md", {"template": "A.tmpl"}, ctx)
        == "own-agents@t.ts.net /home/own-agents\n"
    )
    with pytest.raises(ExecError):
        resolve_content("B.md", {"template": "B.tmpl"}, ctx)
