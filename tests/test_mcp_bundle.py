"""mcp bundle + bifrost admin client, against a fake HTTP admin API (no real bifrost)."""

from __future__ import annotations

import base64
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from nira.bundles.mcp import McpBundle
from nira.core.bundle import Requirement, RequirementError, validate_requirement
from nira.core.model import HostConfig, Op, Status
from nira.core.protect import is_protected
from nira.ops import executors, mcp_admin, probes
from nira.ops.pyinfra_facts import ProbeError, collect_via_pyinfra

USER, PASSWORD = "admin", "s3cr3t-pw-do-not-leak"


class FakeAdmin:
    def __init__(self) -> None:
        self.clients: dict[str, dict[str, Any]] = {}
        self.vks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.busy_puts = 0  # next N client PUTs answer 500 'reconnect already in progress'
        self.honor_create_disabled = False
        self._n = 0
        admin = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _reply(self, code: int, body: Any = None) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self) -> bool:
                want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    self._reply(401, {"error": "unauthorized"})
                    return False
                return True

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self) -> None:
                if not self._auth():
                    return
                if self.path == "/api/mcp/clients":
                    self._reply(200, [{"config": c, "state": "x"} for c in admin.clients.values()])
                elif self.path == "/api/governance/virtual-keys":
                    self._reply(200, {"virtual_keys": list(admin.vks.values())})
                else:
                    self._reply(404, {})

            def do_POST(self) -> None:
                if not self._auth():
                    return
                body = self._body()
                admin.calls.append(("POST", self.path, body))
                admin._n += 1
                cid = f"id{admin._n}"
                cfg = dict(body, client_id=cid)
                if not admin.honor_create_disabled:
                    cfg["disabled"] = False  # live gateway: only PUT honors disabled
                admin.clients[cid] = cfg
                self._reply(200, {"status": "ok"})

            def do_PUT(self) -> None:
                if not self._auth():
                    return
                body = self._body()
                admin.calls.append(("PUT", self.path, body))
                key = self.path.rsplit("/", 1)[1]
                if self.path.startswith("/api/mcp/client/"):
                    if admin.busy_puts:
                        admin.busy_puts -= 1
                        self._reply(500, {"error": "reconnect already in progress"})
                        return
                    admin.clients[key].update(body)
                else:
                    admin.vks[key].update(body)
                self._reply(200, {"status": "ok"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def disabled(self, name: str) -> bool | None:
        for c in self.clients.values():
            if c["name"] == name:
                return c["disabled"]
        return None


@pytest.fixture
def admin():
    a = FakeAdmin()
    yield a
    a.close()


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    m = tmp_path / "mcp"
    m.mkdir()
    (m / "stripe.yaml").write_text(
        "name: Stripe\ntransport: http\nurl: https://mcp.stripe.com\nauth: headers\n"
        "auth_env: [STRIPE_API_KEY]\ntools_to_execute: ['*']\n"
    )
    (m / "mindwtr.yaml").write_text(
        "name: mindwtr\ntransport: stdio\ncommand: {home: .config/bifrost/mcp-wrappers/m.sh}\n"
        "args: []\nauth: none\n"
    )
    (m / "todo.yaml").write_text("name: pending\ntransport: http\nurl: null\n")
    return tmp_path


def make_host(fleet: Path, admin: FakeAdmin, **extra: Any) -> HostConfig:
    creds = fleet / "creds.env"
    creds.write_text(f"BIFROST_ADMIN_USER={USER}\nBIFROST_ADMIN_PASSWORD={PASSWORD}\n")
    creds.chmod(0o600)
    ov = {
        "mcp_enable": ["stripe"],
        "bifrost_admin_port": admin.port,
        "mcp_creds_file": str(creds),
        **extra,
    }
    return HostConfig(
        "t", "tester", "localhost", "linux", bundles=["mcp"], local=True,
        fleet_path=str(fleet), overrides=ov,
    )


class LocalSession:
    """Runs op commands in a local bash instead of through pyinfra."""

    def __init__(self) -> None:
        self.outputs: list[str] = []

    def exists(self, path: str) -> bool:
        return False

    def run(self, specs: list[executors.OpSpec], verify_cmd: str | None) -> executors.Outcome:
        out = executors.Outcome()
        for spec in specs:
            p = subprocess.run(
                spec.kwargs["commands"][0], shell=True, capture_output=True, text=True, timeout=60, check=False
            )
            self.outputs.append(p.stdout + p.stderr)
            if p.returncode != 0:
                out.error = (p.stderr or p.stdout).strip()
                return out
            out.changed |= executors.CHANGED_MARK in p.stdout
        if verify_cmd:
            v = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True, timeout=60, check=False)
            if v.returncode == 0:
                out.verified = True
            else:
                out.verify_error = v.stderr.strip() or "verify failed"
        return out


def plan_of(host: HostConfig) -> list:
    b = McpBundle()
    facts = collect_via_pyinfra(host, list(b.resolved(host)))
    return list(b.plan(host, facts))


def apply_plan(host: HostConfig, entries: list) -> list[dict]:
    ctx = executors.ExecCtx(os="linux", ssh_user="tester", local=True)
    sess = LocalSession()
    res = [
        executors.run_entry(e, ctx, sess)
        for e in entries
        if e.status in (Status.MISSING, Status.DRIFTED)
    ]
    assert PASSWORD not in "".join(sess.outputs)
    return res


# ----------------------------------------------------------------- requirement shape


def test_requirements_one_per_client_disabled_by_default(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    reqs = {r.name: r for r in McpBundle().resolved(host)}
    assert set(reqs) == {"mcp:Stripe", "mcp:mindwtr"}  # null-url client is not a requirement
    assert reqs["mcp:Stripe"].kind == "mcp"
    assert reqs["mcp:Stripe"].params["enabled"] is True
    assert reqs["mcp:mindwtr"].params["enabled"] is False
    # stdio command is a home-relative path rendered for the run-as user
    assert reqs["mcp:mindwtr"].params["client"]["command"] == (
        "/home/tester/.config/bifrost/mcp-wrappers/m.sh"
    )


def test_stdio_command_uses_agent_user_home(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    host.agent_user = "own-agents"
    reqs = {r.name: r for r in McpBundle().resolved(host)}
    assert reqs["mcp:mindwtr"].params["client"]["command"].startswith("/home/own-agents/")


def test_validation_and_not_swallowed_by_bifrost_protection(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    for r in McpBundle().resolved(host):
        validate_requirement(r, "linux")
        assert not is_protected(r.name, ())  # bifrost* is protected; mcp:* is not
    with pytest.raises(RequirementError):
        validate_requirement(Requirement("mcp:x", "mcp", {"client": {}, "bogus": 1}))
    with pytest.raises(RequirementError):
        probes.probe_snippet("mcp", "mcp:x", {"client": {}}, probes.Ctx("linux", "u"))


def test_null_url_client_reported_skipped(fleet: Path, admin: FakeAdmin) -> None:
    entries = {e.component: e for e in plan_of(make_host(fleet, admin))}
    assert entries["mcp:pending"].status == Status.SKIPPED
    assert entries["mcp:pending"].op == Op.SKIP


def test_no_fleet_no_requirements() -> None:
    h = HostConfig("t", "u", "a", "linux")
    assert list(McpBundle().requirements(h)) == []


def test_snippet_never_carries_credentials(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    for r in McpBundle().resolved(host):
        for mode in ("probe", "apply"):
            cmd = mcp_admin.helper_command(mode, r.params)
            assert PASSWORD not in cmd and "BIFROST_ADMIN_PASSWORD=" not in cmd
            assert "creds.env" in cmd and "<" in cmd  # creds travel via stdin redirect


def test_shipped_source_is_standalone() -> None:
    src = mcp_admin.shipped_source()
    assert "# ---- NIRA" not in src and "from nira" not in src and "import nira" not in src
    compile(src, "mcp_admin_shipped", "exec")


# ----------------------------------------------------------------- against the fake admin


def test_plan_apply_plan_converges(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    first = plan_of(host)
    by = {e.component: e for e in first}
    assert by["mcp:Stripe"].status == Status.MISSING
    assert by["mcp:mindwtr"].status == Status.MISSING

    results = apply_plan(host, first)
    assert {r["result"] for r in results} == {"changed"}
    assert all(r["verify"] == "ok" for r in results)
    assert admin.disabled("Stripe") is False  # enabled via mcp_enable
    assert admin.disabled("mindwtr") is True  # default disabled (fixed up by PUT after create)
    stripe = next(c for c in admin.clients.values() if c["name"] == "Stripe")
    assert stripe["connection_string"] == "https://mcp.stripe.com"
    assert stripe["auth_type"] == "headers"
    mind = next(c for c in admin.clients.values() if c["name"] == "mindwtr")
    assert mind["stdio_config"]["command"] == "/home/tester/.config/bifrost/mcp-wrappers/m.sh"

    second = plan_of(host)
    gaps = [e for e in second if e.status in (Status.MISSING, Status.DRIFTED)]
    assert gaps == []  # idempotent
    n = len(admin.calls)
    assert apply_plan(host, second) == []
    assert len(admin.calls) == n  # no mutation on the second pass


def test_enable_set_drives_drift_both_ways(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    apply_plan(host, plan_of(host))
    host.overrides["mcp_enable"] = ["mindwtr"]  # Stripe now must be disabled, mindwtr enabled
    by = {e.component: e for e in plan_of(host)}
    assert by["mcp:Stripe"].status == Status.DRIFTED
    assert by["mcp:mindwtr"].status == Status.DRIFTED
    apply_plan(host, list(by.values()))
    assert admin.disabled("Stripe") is True and admin.disabled("mindwtr") is False
    assert not [e for e in plan_of(host) if e.status in (Status.MISSING, Status.DRIFTED)]


def test_reconnect_busy_is_retried_then_succeeds(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin, mcp_enable=[])
    apply_plan(host, plan_of(host))
    host.overrides["mcp_enable"] = ["Stripe"]
    admin.busy_puts = 2
    reqs = {r.name: r for r in McpBundle().resolved(host)}
    p = dict(reqs["mcp:Stripe"].params, backoff=0, retries=5)
    entry = _entry("mcp:Stripe", p)
    res = executors.run_entry(entry, executors.ExecCtx("linux", "tester", local=True), LocalSession())
    assert res["result"] == "changed", res
    assert admin.disabled("Stripe") is False


def test_reconnect_busy_exhausted_reports_failed_not_crash(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin, mcp_enable=[])
    apply_plan(host, plan_of(host))
    admin.busy_puts = 99
    reqs = {r.name: r for r in McpBundle().resolved(make_host(fleet, admin))}
    p = dict(reqs["mcp:Stripe"].params, backoff=0, retries=2)
    res = executors.run_entry(
        _entry("mcp:Stripe", p), executors.ExecCtx("linux", "tester", local=True), LocalSession()
    )
    assert res["result"] == "failed"
    assert "reconnect already in progress" in res["detail"]
    assert PASSWORD not in json.dumps(res)


def test_bad_credentials_is_probe_error_not_missing(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    Path(host.overrides["mcp_creds_file"]).write_text(
        "BIFROST_ADMIN_USER=admin\nBIFROST_ADMIN_PASSWORD=wrong\n"
    )
    with pytest.raises(ProbeError):
        plan_of(host)


def test_unreachable_admin_is_probe_error(fleet: Path, admin: FakeAdmin) -> None:
    host = make_host(fleet, admin)
    admin.close()
    with pytest.raises(ProbeError):
        plan_of(host)


def test_virtual_key_grant_and_provider_config_normalisation(
    fleet: Path, admin: FakeAdmin
) -> None:
    admin.vks["vk1"] = {
        "id": "vk1",
        "name": "agents",
        "mcp_configs": [{"mcp_client_name": "other", "tools_to_execute": ["*"]}],
        "provider_configs": [{"provider": "openai", "allow_all_keys": True}],
    }
    host = make_host(fleet, admin, mcp_virtual_key="agents")
    apply_plan(host, plan_of(host))
    vk = admin.vks["vk1"]
    names = {m["mcp_client_name"] for m in vk["mcp_configs"]}
    assert names == {"other", "Stripe", "mindwtr"}
    assert vk["provider_configs"] == [{"provider": "openai", "key_ids": ["*"]}]
    assert not [e for e in plan_of(host) if e.status in (Status.MISSING, Status.DRIFTED)]


def _entry(component: str, params: dict) -> Any:
    from nira.core.model import PlanEntry

    return PlanEntry("mcp", component, Op.INSTALL, Status.MISSING, kind="mcp", params=params)
