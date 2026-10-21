"""Package resolution: OS manager -> mise spec map -> as written; mise name checks."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from nira.core import packages
from nira.core.bundle import Bundle, Requirement, RequirementError, validate_requirement
from nira.core.model import HostConfig, Status
from nira.core.packages import MISE_SPECS, PackageError, check_mise_spec, resolve_package
from nira.ops import probes
from nira.ops.executors import ExecCtx, SkipEntry, build_ops

CTX = ExecCtx(os="linux", ssh_user="deb", agent_user="own-agents", home="/home/own-agents")
REAL_MISE = shutil.which("mise")


def test_resolution_order() -> None:
    assert resolve_package("ripgrep", "linux").manager == "apt"
    assert resolve_package("ripgrep", "macos").manager == "brew"
    # not in apt/brew maps -> mise spec
    s = resolve_package("repomix", "linux")
    assert (s.manager, s.package, s.binary) == ("mise", "npm:repomix", "repomix")
    assert resolve_package("ast-grep", "macos").package == "aqua:ast-grep/ast-grep"
    assert resolve_package("ghgrab", "linux").package == "github:abhixdd/ghgrab"
    assert resolve_package("ghx", "linux").package == "github:brunoborges/ghx"
    # brew has sops / yt-dlp; apt does not
    assert resolve_package("sops", "macos").manager == "brew"
    assert resolve_package("sops", "linux").manager == "mise"


def test_verify_only_names() -> None:
    s = resolve_package("degoog-mcp", "linux")
    assert s.manager == "external" and s.verify_only
    assert probes.probe_snippet("package", "degoog-mcp", {}, probes.Ctx("linux", "deb")) == (
        "command -v degoog-mcp >/dev/null 2>&1"
    )
    with pytest.raises(SkipEntry):
        build_ops("package", "degoog-mcp", {}, CTX)
    # a requirement-level flag makes any package probe-only
    with pytest.raises(SkipEntry):
        build_ops("package", "bifrost", {"verify_only": True}, CTX)


def test_mise_package_probe_and_scope() -> None:
    ctx = probes.Ctx("linux", "deb", "own-agents")
    probe = probes.probe_snippet("package", "repomix", {}, ctx)
    assert probe == "mise where npm:repomix >/dev/null 2>&1"
    assert "command -v" in probes.probe_snippet("package", "bifrost", {"verify_only": True}, ctx)
    assert probes.effective_scope("package", "repomix", {}, "linux") == "agent"
    built = build_ops("package", "repomix", {}, CTX)
    (spec,) = built.specs
    assert spec.kwargs["_sudo_user"] == "own-agents"
    assert "mise use -g npm:repomix@latest" in spec.kwargs["commands"][0]
    ver = probes.verify_snippet("package", "repomix", {}, ctx)
    assert "command -v repomix" in ver


def test_bifrost_generates_platform_table() -> None:
    cmd = build_ops("package", "bifrost", {}, CTX).specs[0].kwargs["commands"][0]
    assert "tools.github:maximhq/bifrost.version_prefix" in cmd
    assert "transports/v" in cmd
    for plat, path in (
        ("linux-x64", "linux/amd64"),
        ("linux-arm64", "linux/arm64"),
        ("macos-arm64", "darwin/arm64"),
    ):
        assert f"platforms.{plat}.url" in cmd
        assert f"downloads.getmaxim.ai/bifrost/v{{{{version}}}}/{path}/bifrost-http" in cmd
    assert "mise config get" in cmd and "mise install github:maximhq/bifrost" in cmd


@pytest.mark.skipif(REAL_MISE is None, reason="mise not installed")
def test_config_merge_does_not_clobber(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config/mise").mkdir(parents=True)
    (home / ".local/bin").mkdir(parents=True)
    cfg = home / ".config/mise/config.toml"
    cfg.write_text(
        '[tools]\nnode = "22"\n\n[tools."github:maximhq/bifrost"]\nversion = "1.0.0"\n\n'
        "[settings]\nexperimental = true\n"
    )
    # shim: real `mise config ...`, but never touch the network for where/install
    shim = home / ".local/bin/mise"
    shim.write_text(
        f'#!/bin/sh\ncase "$1" in config) exec {REAL_MISE} "$@";; where) exit 1;; '
        f"install) echo installed >> {tmp_path}/log; exit 0;; esac\nexit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    script = build_ops("package", "bifrost", {}, CTX).specs[0].kwargs["commands"][0]
    env = {**os.environ, "HOME": str(home), "MISE_GLOBAL_CONFIG_FILE": str(cfg)}
    for _ in range(2):  # idempotent
        res = subprocess.run(
            ["sh", "-c", script], env=env, capture_output=True, text=True, check=False
        )
        assert res.returncode == 0, res.stderr
        assert "NIRA_CHANGED" in res.stdout
    data = tomllib.loads(cfg.read_text())
    assert data["tools"]["node"] == "22" and data["settings"]["experimental"] is True
    bif = data["tools"]["github:maximhq/bifrost"]
    assert bif["version"] == "1.0.0"  # existing entry kept
    assert bif["version_prefix"] == "transports/v"
    assert bif["platforms"]["linux-x64"]["url"].endswith("/linux/amd64/bifrost-http")


def test_plan_time_validation_of_mise_specs() -> None:
    validate_requirement(Requirement("repomix", "package"), "linux")
    with pytest.raises(RequirementError, match="ambiguous"):
        validate_requirement(
            Requirement("mise:fx", "custom", {"mise": True, "tool": "fx"}), "linux"
        )
    with pytest.raises(RequirementError, match="owner/repo"):
        validate_requirement(
            Requirement("x", "custom", {"mise": True, "tool": "github:nope"}), "linux"
        )


def test_check_mise_spec_syntax() -> None:
    check_mise_spec("github:vercel-labs/fx")
    check_mise_spec("npm:@scope/pkg")
    for bad in ("fx", "bogus:thing", "a b", "github:"):
        with pytest.raises(PackageError):
            check_mise_spec(bad)


@pytest.mark.skipif(REAL_MISE is None, reason="mise not installed")
def test_registry_shortnames_resolve_to_expected_backend() -> None:
    reg = packages.mise_registry()
    assert reg is not None
    expected = {
        "ast-grep": "aqua:ast-grep/ast-grep",
        "fd": "aqua:sharkdp/fd",
        "ripgrep": "aqua:BurntSushi/ripgrep",
        "sops": "aqua:getsops/sops",
        "claude": "aqua:anthropics/claude-code",
        "crush": "aqua:charmbracelet/crush",
        "yt-dlp": "github:yt-dlp/yt-dlp",
    }
    for short, backend in expected.items():
        assert reg[short][0] == backend, short
    # the bare shortname 'fx' is antonmedv/fx; vercel-labs fx must be spelled out
    assert reg["fx"][0] == "aqua:antonmedv/fx"
    with pytest.raises(PackageError):
        check_mise_spec("fx")
    check_mise_spec("github:vercel-labs/fx")
    # every spec the code ships is either explicit-backend or a registry name
    for name, ms in MISE_SPECS.items():
        if ms.spec and ":" not in ms.spec:
            assert ms.spec in reg, name


@pytest.mark.skipif(os.environ.get("NIRA_ONLINE") != "1", reason="set NIRA_ONLINE=1")
def test_online_specs_list_versions() -> None:
    for ms in MISE_SPECS.values():
        if ms.spec:
            check_mise_spec(ms.spec, online=True)


def test_disabled_bundle_is_skipped_by_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    from nira.core import planner

    monkeypatch.setattr(planner, "collect_facts", lambda host, reqs=None: {})
    host = HostConfig("h", "deb", "a", "linux", bundles=["orca"])
    plan = planner.build_plan(host)
    (e,) = plan.entries
    assert (e.bundle, e.status, e.detail) == ("orca", Status.SKIPPED, "disabled")


def test_enabled_defaults_true() -> None:
    class _B(Bundle):
        name = "x"

        def requirements(self, host: Any) -> Any:
            return iter(())

    assert _B.enabled is True
