"""``nira plan --all-hosts --check``: offline fleet render/validate with a tmp fleet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nira.bundle_paths import hp
from nira.cli.check import check_fleet
from nira.cli.main import app
from nira.core.bundle import REGISTRY, Bundle, Requirement

runner = CliRunner()

TEMPLATE_OK = "tailnet={{ tailnet_domain }} user={{ user }} home={{ home }} host={{ host }}\n"


class DemoBundle(Bundle):
    name = "t-demo"
    description = "test bundle"

    def requirements(self, host):
        yield Requirement(name="ripgrep", kind="package")
        yield Requirement(
            name=hp(".config/demo.md"),
            kind="file",
            params={"template": "demo.md.j2", "scope": "agent"},
        )
        yield Requirement(name="demo-svc", kind="service", params={"unit_path": "/etc/x.service"})


@pytest.fixture(autouse=True)
def _demo_bundle(monkeypatch):
    monkeypatch.setitem(REGISTRY, "t-demo", DemoBundle)


def make_fleet(
    tmp: Path,
    template: str = TEMPLATE_OK,
    protected: tuple[str, ...] = ("demo-svc",),
    bundles: tuple[str, ...] = ("t-demo",),
    manifest_protected: tuple[str, ...] = (),
    overrides: str | None = None,
) -> Path:
    (tmp / "inventory").mkdir(parents=True)
    (tmp / "agent-config").mkdir()
    (tmp / "group_data").mkdir()
    (tmp / "agent-config" / "demo.md.tmpl").write_text(template)
    hosts = [
        {
            "name": "ovh",
            "ssh_user": "debian",
            "address": "ovh.example",
            "os": "linux",
            "bundles": list(bundles),
            "protected_components": list(protected),
            "agent_user": "own-agents",
        },
        {
            "name": "mini",
            "ssh_user": "miniadmin",
            "address": "mini.example",
            "os": "macos",
            "bundles": list(bundles),
            "protected_components": [],
        },
    ]
    (tmp / "inventory" / "hosts.py").write_text(f"HOSTS = {hosts!r}\n")
    bundle_names = sorted({*bundles, "t-demo"})
    manifest = (
        "schema_version: 1\nfleet:\n  name: t\n  tailscale_domain: tail.example.ts.net\n"
        "bundles:\n"
        + "".join(f"  {b}:\n    description: d\n    requirements: {{}}\n" for b in bundle_names)
        + "protected: "
        + json.dumps(list(manifest_protected))
        + "\n"
    )
    (tmp / "manifest.yaml").write_text(manifest)
    (tmp / "group_data" / "overrides.yaml").write_text(
        overrides or "ovh:\n  agent_user: own-agents\n"
    )
    return tmp


def run(fleet: Path, *extra: str):
    return runner.invoke(app, ["plan", "--all-hosts", "--check", "--fleet", str(fleet), *extra])


def test_clean_fleet_passes_offline(tmp_path):
    res = run(make_fleet(tmp_path))
    assert res.exit_code == 0, res.output
    assert "check ok: 2 host(s), 0 error(s)" in res.output


def test_ssh_is_never_used(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("offline check must not run subprocesses")

    monkeypatch.setattr("subprocess.run", boom)
    assert run(make_fleet(tmp_path)).exit_code == 0


def test_undefined_template_var_fails(tmp_path):
    res = run(make_fleet(tmp_path, template="x={{ nope }}\n"))
    assert res.exit_code == 1
    assert "'nope' is undefined" in res.output
    assert "ovh/t-demo" in res.output and "mini/t-demo" in res.output  # all hosts listed


def test_override_template_vars_resolve(tmp_path):
    fleet = make_fleet(
        tmp_path,
        template="{{ custom }}|{{ user }}\n",
        overrides="ovh:\n  agent_user: own-agents\n  template_vars:\n    custom: hi\n"
        "mini:\n  template_vars:\n    custom: yo\n",
    )
    assert not check_fleet(fleet).errors


def test_override_template_vars_must_be_mapping(tmp_path):
    fleet = make_fleet(tmp_path, overrides="ovh:\n  template_vars: [a]\n")
    assert any("template_vars" in i.message for i in check_fleet(fleet).errors)


def test_unknown_bundle_and_invalid_requirement(tmp_path, monkeypatch):
    class Bad(Bundle):
        name = "t-bad"

        def requirements(self, host):
            yield Requirement(name="x", kind="file", params={"bogus": 1})

    monkeypatch.setitem(REGISTRY, "t-bad", Bad)
    res = run(make_fleet(tmp_path, bundles=("t-demo", "t-bad", "ghost")))
    assert res.exit_code == 1
    assert "unknown bundle" in res.output and "ghost" in res.output
    assert "unknown params ['bogus']" in res.output


def test_protected_glob_matching_nothing_is_error(tmp_path):
    res = run(make_fleet(tmp_path, protected=("demo-svc", "nomatch-*")))
    assert res.exit_code == 1
    assert "protected pattern 'nomatch-*' matches no component" in res.output
    assert "'demo-svc' matches" not in res.output


def test_protected_glob_for_external_resource_is_only_a_warning(tmp_path):
    fleet = make_fleet(tmp_path, protected=("brioso-*",), manifest_protected=("brioso-*",))
    res = run(fleet)
    assert res.exit_code == 0, res.output
    assert "WARNING" in res.output and "brioso-*" in res.output


def test_override_for_unknown_host_errors_and_disabled_bundle_skipped(tmp_path, monkeypatch):
    class Off(DemoBundle):
        name = "t-off"
        enabled = False

    monkeypatch.setitem(REGISTRY, "t-off", Off)
    fleet = make_fleet(
        tmp_path,
        bundles=("t-demo", "t-off"),
        overrides="ovh:\n  agent_user: own-agents\nzzz:\n  a: 1\n",
    )
    res = run(fleet)
    assert res.exit_code == 1
    assert "zzz" in res.output and "not in the inventory" in res.output
    assert "t-off" not in res.output  # disabled -> info only, not printed


def test_injected_home_of_used_for_rendering(tmp_path):
    seen = []

    def home_of(user):
        seen.append(user)
        return f"/h/{user}"

    check_fleet(make_fleet(tmp_path, template="{{ home }}\n"), home_of=home_of)
    assert "own-agents" in seen and "miniadmin" in seen


def test_json_output(tmp_path):
    res = run(make_fleet(tmp_path, template="{{ nope }}"), "--json")
    data = json.loads(res.output)
    assert res.exit_code == 1 and data["errors"] == 2 and data["hosts"] == 2


def test_probe_flag_plans_each_host(tmp_path):
    seen = []
    rep = check_fleet(make_fleet(tmp_path), probe=True, plan_fn=lambda h: seen.append(h.name))
    assert seen == ["ovh", "mini"] and not rep.errors

    def fail(h):
        raise RuntimeError("ssh down")

    assert check_fleet(make_fleet(tmp_path / "x"), probe=True, plan_fn=fail).errors


def test_usage_errors(tmp_path):
    fleet = str(make_fleet(tmp_path))
    assert runner.invoke(app, ["plan", "--check", "--fleet", fleet]).exit_code == 2
    assert runner.invoke(app, ["plan", "--fleet", fleet]).exit_code == 2
    both = ["plan", "--all-hosts", "--check", "--host", "x", "--fleet", fleet]
    assert runner.invoke(app, both).exit_code == 2
