# ruff: noqa: UP031
"""bifrost admin API client for ``mcp`` requirements.

Everything above the ``NIRA SIDE`` marker is stdlib-only, python>=3.7 source that is
SHIPPED to the target host (``python3 -c <source> probe|apply``) and talks to the
bifrost admin API on 127.0.0.1. Credentials never appear in argv or logs: they are
read from stdin (a dotenv stream: ``sops -d --output-type dotenv file |`` or
``< file``) or from the process environment (BIFROST_ADMIN_USER/_PASSWORD).
The (secret-free) request spec travels in the NIRA_MCP_SPEC environment variable.

Admin API semantics (learned from the live gateway):
  * create ``POST /api/mcp/client``; update ``PUT /api/mcp/client/{id}``;
    list ``GET /api/mcp/clients``.
  * ``disabled: true`` is honored by PUT (so a create is followed by a PUT when
    the client must start disabled).
  * PUT can answer 500 'reconnect already in progress' for unreachable http
    clients: retried with exponential backoff, then reported FAILED (rc 1).
  * VK grants: ``PUT /api/governance/virtual-keys/{id}`` with
    ``mcp_configs: [{mcp_client_name, tools_to_execute: ["*"]}]``.
    GOTCHA (not needed for MCP, kept for when VKs are managed here): on VK
    create/update ``provider_configs`` need ``key_ids: ["*"]``, not
    ``allow_all_keys``; this client rewrites ``allow_all_keys`` accordingly when it
    has to send provider_configs back.

Exit codes. probe: 0 satisfied, 1 client missing, 2 drifted (disabled flag or VK
grant), 4 admin API unreachable/unauthorized (surfaced as a probe ERROR, never as
"missing"). apply: 0 (prints NIRA_CHANGED / NIRA_UNCHANGED), 1 FAILED (reason on stderr).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

CHANGED_MARK = "NIRA_CHANGED"
UNCHANGED_MARK = "NIRA_UNCHANGED"
DEFAULT_PORT = 8317
USER_KEY, PASS_KEY = "BIFROST_ADMIN_USER", "BIFROST_ADMIN_PASSWORD"


class AdminError(Exception):
    pass


def parse_dotenv(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ")
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_creds(spec, stdin_text=None):
    env = dict(os.environ)
    if spec.get("creds_stdin") and stdin_text:
        env.update(parse_dotenv(stdin_text))
    user, pw = env.get(USER_KEY), env.get(PASS_KEY)
    return (user, pw) if user and pw else None


class Admin:
    def __init__(self, spec, creds):
        self.base = "http://127.0.0.1:%d" % int(spec.get("port") or DEFAULT_PORT)
        self.creds = creds
        self.retries = int(spec.get("retries", 5))
        self.backoff = float(spec.get("backoff", 0.5))

    def _scrub(self, text):
        if self.creds:
            for secret in self.creds:
                text = text.replace(secret, "***")
        return text[:300]

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.creds:
            tok = base64.b64encode(("%s:%s" % self.creds).encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise AdminError("admin API unreachable: %s" % self._scrub(str(exc)))
        text = raw.decode("utf-8", "replace")
        try:
            return status, json.loads(text) if text.strip() else None
        except ValueError:
            return status, text

    def call(self, method, path, body=None):
        """request() retrying 'reconnect already in progress' 500s with backoff."""
        for attempt in range(self.retries + 1):
            status, data = self.request(method, path, body)
            busy = status == 500 and "reconnect already in progress" in json.dumps(data)
            if not busy or attempt == self.retries:
                if status == 401:
                    raise AdminError("admin API rejected the credentials (401)")
                if status >= 400:
                    raise AdminError(
                        "%s %s -> HTTP %d %s"
                        % (method, path, status, self._scrub(json.dumps(data)))
                    )
                return data
            time.sleep(self.backoff * (2**attempt))
        raise AdminError("unreachable")  # pragma: no cover

    def clients(self):
        """name -> {id, disabled}"""
        data = self.call("GET", "/api/mcp/clients")
        items = data.get("clients", []) if isinstance(data, dict) else (data or [])
        out = {}
        for e in items:
            cfg = e.get("config", e) if isinstance(e, dict) else {}
            name = cfg.get("name")
            if name:
                cid = cfg.get("client_id") or cfg.get("id") or name
                out[name] = {"id": cid, "disabled": bool(cfg.get("disabled", False))}
        return out

    def virtual_key(self, ref):
        data = self.call("GET", "/api/governance/virtual-keys")
        items = data.get("virtual_keys", []) if isinstance(data, dict) else (data or [])
        for vk in items:
            if ref in (vk.get("name"), vk.get("id")):
                return vk
        return None


def client_body(spec):
    c = spec["client"]
    body = {
        "name": c["name"],
        "connection_type": c["transport"],
        "tools_to_execute": c.get("tools_to_execute") or ["*"],
        "auth_type": c.get("auth", "none"),
        "disabled": not spec.get("enabled", False),
    }
    if c["transport"] == "http":
        body["connection_string"] = c["url"]
        if c.get("auth") == "headers" and c.get("auth_env"):
            # ASSUMPTION: bifrost resolves 'env.NAME' header values from its own environment.
            body["headers"] = {"Authorization": "env." + c["auth_env"][0]}
    else:
        body["stdio_config"] = {
            "command": c["command"],
            "args": list(c.get("args") or []),
            "envs": [],
        }
    return body


def grant_state(spec, admin):
    """None when no VK is managed; else (vk or None, granted bool)."""
    ref = spec.get("virtual_key")
    if not ref:
        return None
    vk = admin.virtual_key(ref)
    tools = spec["client"].get("tools_to_execute") or ["*"]
    granted = bool(vk) and any(
        m.get("mcp_client_name") == spec["client"]["name"] and m.get("tools_to_execute") == tools
        for m in (vk.get("mcp_configs") or [])
    )
    return vk, granted


def probe(spec, admin):
    cur = admin.clients().get(spec["client"]["name"])
    if cur is None:
        return 1
    if cur["disabled"] != (not spec.get("enabled", False)):
        return 2
    g = grant_state(spec, admin)
    if g is not None and not g[1]:
        return 2
    return 0


def apply(spec, admin):
    name = spec["client"]["name"]
    want_disabled = not spec.get("enabled", False)
    body = client_body(spec)
    changed = False
    cur = admin.clients().get(name)
    if cur is None:
        admin.call("POST", "/api/mcp/client", body)
        changed = True
        cur = admin.clients().get(name)
        if cur is None:
            raise AdminError("client %s not listed after create" % name)
        if cur["disabled"] != want_disabled:  # PUT is what honors disabled
            admin.call("PUT", "/api/mcp/client/%s" % cur["id"], body)
    elif cur["disabled"] != want_disabled:
        admin.call("PUT", "/api/mcp/client/%s" % cur["id"], body)
        changed = True
    g = grant_state(spec, admin)
    if g is not None and not g[1]:
        vk = g[0]
        if vk is None:
            raise AdminError("virtual key %r not found" % spec["virtual_key"])
        tools = spec["client"].get("tools_to_execute") or ["*"]
        cfgs = [m for m in (vk.get("mcp_configs") or []) if m.get("mcp_client_name") != name]
        cfgs.append({"mcp_client_name": name, "tools_to_execute": tools})
        upd = {"mcp_configs": cfgs}
        if vk.get("provider_configs"):
            pcs = []
            for pc in vk["provider_configs"]:
                pc = dict(pc)
                if pc.pop("allow_all_keys", False) and not pc.get("key_ids"):
                    pc["key_ids"] = ["*"]
                pcs.append(pc)
            upd["provider_configs"] = pcs
        admin.call("PUT", "/api/governance/virtual-keys/%s" % vk["id"], upd)
        changed = True
    return changed


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    spec = json.loads(os.environ["NIRA_MCP_SPEC"])
    stdin_text = sys.stdin.read() if spec.get("creds_stdin") else None
    try:
        admin = Admin(spec, load_creds(spec, stdin_text))
        if mode == "probe":
            return probe(spec, admin)
        if mode == "apply":
            print(CHANGED_MARK if apply(spec, admin) else UNCHANGED_MARK)
            return 0
        raise AdminError("unknown mode %r" % mode)
    except Exception as exc:  # noqa: BLE001 - never let a traceback look like "missing" (rc 1)
        sys.stderr.write("FAILED: %s: %s\n" % (type(exc).__name__, exc))
        return 4 if mode == "probe" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# ---- NIRA SIDE (not shipped) ----


def shipped_source() -> str:
    from pathlib import Path

    return Path(__file__).read_text().split("# ---- NIRA " + "SIDE")[0]


def spec_from_params(params: dict) -> dict:
    from nira.core.bundle import RequirementError

    client = params.get("client")
    if not isinstance(client, dict) or not client.get("name"):
        raise RequirementError("mcp requirement needs params['client'] with a name")
    if client.get("transport") == "http" and not client.get("url"):
        raise RequirementError(f"mcp client {client['name']!r}: http client without url")
    if client.get("transport") == "stdio" and not client.get("command"):
        raise RequirementError(f"mcp client {client['name']!r}: stdio client without command")
    spec: dict = {
        "client": client,
        "enabled": bool(params.get("enabled", False)),
        "port": int(params.get("port") or DEFAULT_PORT),
        "creds_stdin": bool(params.get("creds_sops") or params.get("creds_file")),
    }
    for key in ("virtual_key", "retries", "backoff"):
        if params.get(key) is not None:
            spec[key] = params[key]
    return spec


def helper_command(mode: str, params: dict) -> str:
    """Shell snippet running the shipped helper; credentials only via stdin."""
    import shlex

    q = shlex.quote
    spec = spec_from_params(params)
    run = f"NIRA_MCP_SPEC={q(json.dumps(spec))} python3 -c {q(shipped_source())} {mode}"
    for key, feed in (("creds_sops", "sops -d --output-type dotenv {p} 2>/dev/null | "),
                      ("creds_file", "")):
        path = params.get(key)
        if path:
            guard = f"[ -r {q(path)} ] || {{ echo 'creds file {path} unreadable' >&2; exit 4; }}; "
            if feed:
                return guard + feed.format(p=q(path)) + run
            return guard + f"{run} < {q(path)}"
    return run


def build_mcp(name, params, ctx, scope, exists):
    """Executor builder for kind 'mcp' (called from executors.build_ops)."""
    from nira.ops.executors import Built, _shell

    return Built([_shell(ctx, helper_command("apply", params), scope, f"mcp {name}")])
