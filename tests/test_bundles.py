"""Tests for nira.bundles: registry, gating, requirements, priority."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import nira.bundles  # noqa: F401  populate registry
from nira.core.bundle import REGISTRY, Requirement
from nira.core.model import HostConfig, Op, Status


def host(os: str = "macos", bundles: list[str] | None = None, **kw) -> HostConfig:
    return HostConfig(
        name="h",
        ssh_user="u",
        address="a",
        os=os,
        bundles=bundles if bundles is not None else list(bundles or []),
        **kw,
    )


def names(bundle_name: str, h: HostConfig) -> set[str]:
    b = REGISTRY[bundle_name]()
    return {r.name for r in b.requirements(h)}


# ---------------- registry ----------------


def test_all_bundles_registered() -> None:
    expected = {
        "core",
        "agent-runtimes",
        "search-retrieval",
        "skills",
        "agent-config",
        "services-mini",
        "orca",
        "search-gateway",
        "secret-broker",
        "media-audio",
        "macos-desktop",
        "bifrost-stack",
    }
    assert expected <= set(REGISTRY)


def test_default_priority() -> None:
    assert REGISTRY["core"].priority == 50
    assert REGISTRY["secret-broker"].priority == 0


def test_secret_broker_sorts_first() -> None:
    order = sorted(
        ("core", "secret-broker", "orca"), key=lambda n: REGISTRY[n].priority
    )
    assert order[0] == "secret-broker"


# ---------------- OS gating ----------------


def test_services_mini_macos_only() -> None:
    b = REGISTRY["services-mini"]()
    assert b.supports(host("macos"))
    assert not b.supports(host("linux"))


def test_search_gateway_linux_only() -> None:
    b = REGISTRY["search-gateway"]()
    assert b.supports(host("linux"))
    assert not b.supports(host("macos"))


def test_search_retrieval_linux_with_macos_override() -> None:
    b = REGISTRY["search-retrieval"]()
    assert b.supports(host("linux"))
    assert not b.supports(host("macos"))  # not selected
    assert b.supports(host("macos", bundles=["search-retrieval"]))  # explicit override


def test_media_audio_macos_only() -> None:
    assert REGISTRY["media-audio"]().supports(host("macos"))
    assert not REGISTRY["media-audio"]().supports(host("linux"))


def test_core_cross_platform() -> None:
    b = REGISTRY["core"]()
    assert b.supports(host("macos")) and b.supports(host("linux"))


# ---------------- requirements ----------------


def test_core_requirements() -> None:
    got = names("core", host("macos"))
    assert {"mise", "git", "nono"} <= got


def test_agent_runtimes_mise_tools() -> None:
    got = names("agent-runtimes", host("linux"))
    assert {"mise:claude", "mise:bun", "mise:node"} <= got


def test_search_retrieval_tools() -> None:
    got = names("search-retrieval", host("linux"))
    assert {"ripgrep", "ugrep", "fd", "fzf", "jq", "ast-grep", "gh"} <= got


def test_bifrost_stack_secret_and_env_ref() -> None:
    h = host("linux")
    reqs = {r.name: r for r in REGISTRY["bifrost-stack"]().requirements(h)}
    assert "BIFROST_ENCRYPTION_KEY" in reqs
    assert reqs["BIFROST_ENCRYPTION_KEY"].kind == "secret"
    cfg = reqs[str(Path.home() / ".config/bifrost/config.json")]
    assert cfg.params["env_ref"] == "BIFROST_ENCRYPTION_KEY"


def test_bifrost_service_gating_by_os() -> None:
    mac = list(REGISTRY["bifrost-stack"]().requirements(host("macos")))
    lin = list(REGISTRY["bifrost-stack"]().requirements(host("linux")))
    svc_mac = next(r for r in mac if r.name == "bifrost-daemon")
    svc_lin = next(r for r in lin if r.name == "bifrost-daemon")
    assert "plist_path" in svc_mac.params
    assert "unit_path" in svc_lin.params


def test_search_gateway_hardening() -> None:
    h = host("linux")
    reqs = {r.name: r for r in REGISTRY["search-gateway"]().requirements(h)}
    deg = reqs["degoog"]
    assert deg.kind == "service"
    assert deg.params["MemoryMax"] == "300M"
    assert deg.params["bind"] == "127.0.0.1"


def test_secret_broker_seed_is_sops_only() -> None:
    reqs = {r.name: r for r in REGISTRY["secret-broker"]().requirements(host("macos"))}
    seed = reqs["agent-vault-seed"]
    assert seed.kind == "secret"
    assert seed.params["method"] == "sops-exec-env"
    assert "sops_file" in seed.params


def test_orca_pairing_verify_only() -> None:
    reqs = {r.name: r for r in REGISTRY["orca"]().requirements(host("macos"))}
    assert reqs["orca-pairing"].params == {"check": "config-presence-only"}


def test_services_mini_uses_launchd_bootstrap() -> None:
    reqs = {r.name: r for r in REGISTRY["services-mini"]().requirements(host("macos"))}
    svc = reqs["mcp-sim"]
    assert svc.params["bootstrap"] is True
    assert "LaunchDaemons" in svc.params["plist_path"]


# ---------------- plan() default behavior ----------------


def test_default_plan_assesses_facts() -> None:
    b = REGISTRY["core"]()
    h = host("macos")
    facts = {"package": {"mise": True, "git": True}, "custom": {}}
    entries = list(b.plan(h, facts))
    statuses = {e.component: e.status for e in entries}
    assert statuses["mise"] == Status.OK
    assert statuses["nono"] == Status.MISSING
    assert all(e.bundle == "core" for e in entries)
    assert all(e.op == Op.INSTALL for e in entries)


def test_plan_drifted_when_fact_is_dict() -> None:
    b = REGISTRY["agent-config"]()
    h = host("macos")
    req = next(b.requirements(h))
    facts = {"file": {req.name: {"drift": "hash differs"}}}
    entries = list(b.plan(h, facts))
    assert entries[0].status == Status.DRIFTED
