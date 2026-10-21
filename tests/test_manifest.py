"""Fleet manifest schema + cross-check against the bundle REGISTRY."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from nira.bundle_paths import hp
from nira.core.bundle import Bundle, Requirement
from nira.core.manifest import (
    Disagreement,
    ManifestError,
    cross_check,
    filter_known,
    load_fleet_manifest,
    load_known_gaps,
    norm_home_path,
    norm_repo,
    template_vars_for,
)
from nira.core.model import HostConfig, load_hosts

KNOWN = Path(__file__).parent / "known_manifest_gaps.yaml"
FLEET = Path(os.environ.get("NIRA_FLEET", Path.home() / "Documents/prjcts/nira-fleet"))

BASE = {
    "schema_version": 1,
    "fleet": {"name": "t", "tailscale_domain": "t.ts.net"},
    "bundles": {"b": {"description": "d", "requirements": {}}},
    "protected": [],
}


def write(tmp: Path, data: dict) -> Path:
    p = tmp / "manifest.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def manifest_with(tmp: Path, **reqs):
    data = {**BASE, "bundles": {"b": {"description": "d", "requirements": reqs}}}
    return load_fleet_manifest(write(tmp, data))


class B(Bundle):
    name = "b"

    def requirements(self, host):
        yield Requirement("fd", "package")
        yield Requirement("mise:node", "custom", {"mise": True, "tool": "node"})
        yield Requirement("svc", "service", {"unit_path": "/etc/x"})
        yield Requirement(hp(".config/a.md"), "file", {"verbatim": True})
        yield Requirement(hp("skills"), "directory", {"repo": "https://github.com/me/skills"})


def kinds(found: list[Disagreement]) -> set[tuple[str, str]]:
    return {(d.kind, d.item) for d in found}


# ------------------------------------------------------------------ schema


def test_schema_rejects_bad_manifests(tmp_path):
    for bad in (
        {**BASE, "schema_version": 2},
        {**BASE, "bundles": {}},
        {**BASE, "fleet": {}},
        {**BASE, "bundles": {"b": {"requirements": {}}}},  # no description
        {**BASE, "bundles": {"b": {"description": "d", "requirements": {"packges": []}}}},
    ):
        with pytest.raises(ManifestError):
            load_fleet_manifest(write(tmp_path, bad))
    with pytest.raises(ManifestError, match="not found"):
        load_fleet_manifest(tmp_path / "nope")


def test_normalizers():
    assert norm_home_path("~/x/y") == "x/y"
    assert norm_home_path("{{ home }}/x/y") == "x/y"
    assert norm_home_path(".sops.yaml") is None
    assert norm_repo("git@github.com:O/R.git") == norm_repo("https://github.com/o/r")


def test_template_vars_precedence(tmp_path):
    m = load_fleet_manifest(write(tmp_path, BASE))
    assert template_vars_for(m, {}, "h") == {"tailnet_domain": "t.ts.net"}
    ov = {"h": {"template_vars": {"tailnet_domain": "x.ts.net", "k": 1}}}
    assert template_vars_for(m, ov, "h") == {"tailnet_domain": "x.ts.net", "k": "1"}


# ------------------------------------------------------------------ cross-check


def test_clean_match_has_no_disagreement(tmp_path):
    m = manifest_with(
        tmp_path,
        packages=["fd-find"],  # alias of fd via apt package name
        mise_tools=["node"],
        services=["svc"],
        files=[{"path": "~/.config/a.md"}],
        repos=[{"url": "git@github.com:me/skills.git", "path": "~/skills"}],
    )
    assert cross_check(m, {"b": B}) == []


def test_every_kind_of_disagreement_reported_precisely(tmp_path):
    m = manifest_with(
        tmp_path,
        packages=["orca-cli"],
        tools=["uv"],
        mise_tools=["python"],
        services=["other"],
        files=[{"path": "~/a.md"}, {"path": "~/zzz"}],
        repos=[{"url": "git@github.com:espetro/skills.git", "path": "~/.agents/skills"}],
    )
    data = {**m.model_dump(), "bundles": {**m.model_dump()["bundles"], "gone": m.bundles["b"]}}
    m = type(m).model_validate(data)
    found = cross_check(m, {"b": B, "extra": B})
    got = kinds(found)
    assert ("bundle-missing-in-python", "gone") in got
    assert ("bundle-missing-in-manifest", "extra") in got
    assert ("package-uncovered", "orca-cli") in got
    assert ("tool-uncovered", "uv") in got
    assert ("mise-tool-uncovered", "python") in got
    assert ("service-uncovered", "other") in got
    assert ("file-dest-mismatch", "a.md") in got
    assert ("file-uncovered", "~/zzz") in got
    assert ("repo-url-mismatch", "git@github.com:espetro/skills.git") in got
    assert ("repo-dest-mismatch", "git@github.com:espetro/skills.git") in got
    assert ("extra-in-bundle", "fd") in got and ("extra-in-bundle", "mise:node") in got
    assert any("~/a.md" in d.detail and ".config/a.md" in d.detail for d in found)


def test_disabled_bundle_may_be_missing_from_manifest(tmp_path):
    class Off(B):
        name = "off"
        enabled = False

    m = manifest_with(tmp_path)
    assert ("bundle-missing-in-manifest", "off") not in kinds(cross_check(m, {"b": B, "off": Off}))


def test_manifest_host_must_be_supported_by_bundle(tmp_path):
    class Lin(B):
        os_support = ("linux",)

    m = manifest_with(tmp_path, hosts=["mac"])
    mac = HostConfig("mac", "u", "a", "macos", ["b"])
    assert ("host-unsupported", "mac") in kinds(cross_check(m, {"b": Lin}, [mac]))


# ------------------------------------------------------------------ allow-list


def test_filter_known_globs_and_stale():
    found = [Disagreement("b", "extra-in-bundle", "mise:x"), Disagreement("c", "file-uncovered", "f")]
    known = [
        {"bundle": "b", "kind": "extra-in-bundle", "item": "mise:*", "reason": "r"},
        {"bundle": "z", "kind": "*", "item": "*", "reason": "r"},
    ]
    unexpected, stale = filter_known(found, known)
    assert [d.bundle for d in unexpected] == ["c"]
    assert [g["bundle"] for g in stale] == ["z"]


def test_known_gap_entries_require_reason(tmp_path):
    p = tmp_path / "g.yaml"
    p.write_text("gaps:\n  - {bundle: a, kind: b, item: c}\n")
    with pytest.raises(ManifestError, match="reason"):
        load_known_gaps(p)


# ------------------------------------------------------------------ the real fleet


@pytest.mark.skipif(not (FLEET / "manifest.yaml").is_file(), reason="nira-fleet not available")
def test_real_fleet_manifest_matches_bundles_modulo_known_gaps(capsys):
    import nira.bundles  # noqa: F401
    from nira.core.bundle import REGISTRY

    manifest = load_fleet_manifest(FLEET)
    hosts = load_hosts(FLEET / "inventory")
    found = cross_check(manifest, REGISTRY, hosts)
    unexpected, stale = filter_known(found, load_known_gaps(KNOWN))
    for g in stale:  # informational: the merge step should delete these
        print(f"stale known gap (no longer disagrees): {g}")
    assert not unexpected, "new manifest/bundle disagreements:\n" + "\n".join(map(str, unexpected))
