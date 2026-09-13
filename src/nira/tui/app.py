"""nira TUI: thin Textual dashboard over the CLI core.

Grid of plan statuses (host rows x bundle columns), plan preview, apply with
confirmation, and doctor checks. Core/ops modules are imported lazily and
guarded so the TUI still renders (and tests still run) without them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static

STATUS_COLORS: dict[str, str] = {
    "ok": "green",
    "drifted": "yellow",
    "missing": "red",
    "protected": "magenta",
    "skipped": "dim",
}

STATUS_SYMBOLS: dict[str, str] = {
    "ok": "OK",
    "drifted": "DRIFT",
    "missing": "MISS",
    "protected": "PROT",
    "skipped": "-",
}

SEVERITY_ORDER = {"missing": 4, "drifted": 3, "protected": 2, "skipped": 1, "ok": 0}


def load_hosts(inventory_dir: Path) -> list[Any]:
    """Lazy hook so tests can patch inventory loading."""
    from nira.core.model import load_hosts as _load_hosts

    return _load_hosts(inventory_dir)


def build_plan(host: Any) -> Any:
    """Lazy hook so tests can patch planning."""
    from nira.core.planner import build_plan as _build_plan

    return _build_plan(host)


def worst_status(entries: list[Any]) -> str:
    """Return the most severe status across entries (missing > drifted > ...)."""
    best = "ok"
    for e in entries:
        s = str(getattr(e, "status", e))
        if SEVERITY_ORDER.get(s, 0) > SEVERITY_ORDER.get(best, 0):
            best = s
    return best


@dataclass
class HostBundleCell:
    """Aggregated worst-status of one host's plan entries for one bundle."""

    host: str
    bundle: str
    status: str
    detail: str = ""


@dataclass
class TuiState:
    hosts: list[Any] | None = None
    plans: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.hosts is None:
            self.hosts = []
        if self.plans is None:
            self.plans = {}


class ConfirmScreen(ModalScreen[bool]):
    """Confirmation modal: y/Button confirms, n/escape cancels."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 60; height: auto; padding: 1 2;
        border: thick $warning; background: $surface;
    }
    .confirm-buttons { height: auto; align-horizontal: center; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message
        self.confirmed = False

    def compose(self):
        from textual.containers import Horizontal
        from textual.widgets import Button

        with Vertical(id="confirm-box"):
            yield Static(self.message)
            with Horizontal(classes="confirm-buttons"):
                yield Button("Apply", variant="warning", id="confirm-yes")
                yield Button("Cancel", variant="default", id="confirm-no")

    def on_button_pressed(self, event) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class NiraTui(App):
    TITLE = "nira fleet"
    SUB_TITLE = "manifest-first fleet replicator"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh plan"),
        Binding("a", "apply", "Apply"),
        Binding("d", "doctor", "Doctor"),
    ]

    CSS = """
    #status-grid { height: 50%; border: solid $accent; }
    #detail { height: 50%; border: solid $primary; }
    """

    def __init__(self, fleet_dir: Path | None = None) -> None:
        super().__init__()
        self.fleet_dir = fleet_dir or Path.cwd()
        self.state = TuiState()
        self._apply_stream: list[str] | None = None
        self._doctor_stream: list[str] | None = None
        self._doctor_target: str | None = None
        self._mode = "grid"
        self.apply_results: list[str] | None = None
        self.applied_host: str | None = None

    # --- data loading -------------------------------------------------

    def load_hosts(self) -> list[Any]:
        self.state.hosts = load_hosts(self.fleet_dir)
        return self.state.hosts

    def build_host_plan(self, host) -> Any:
        return build_plan(host)

    def refresh_plan(self) -> None:
        """Rebuild plans for every host."""
        self.state.plans = {}
        for host in self.load_hosts():
            try:
                self.state.plans[host.name] = self.build_host_plan(host)
            except Exception as exc:  # noqa: BLE001 - defensive: planner may be unavailable
                self.state.plans[host.name] = None
                self.log(f"plan failed for {host.name}: {exc}")
        self.render_grid()

    # --- layout -------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Vertical(id="status-grid"):
                yield DataTable(id="grid")
            with Vertical(id="detail"):
                yield DataTable(id="entries")
                yield Static("", id="statusline")
        yield Footer()

    def on_mount(self) -> None:
        grid = self.query_one("#grid", DataTable)
        grid.cursor_type = "row"
        entries = self.query_one("#entries", DataTable)
        entries.cursor_type = "row"
        self.refresh_plan()

    # --- grid rendering -----------------------------------------------

    def bundles(self) -> list[str]:
        seen: list[str] = []
        for plan in self.state.plans.values():
            if plan is None:
                continue
            for e in plan.entries:
                if e.bundle not in seen:
                    seen.append(e.bundle)
        return seen

    def render_grid(self) -> None:
        grid = self.query_one("#grid", DataTable)
        grid.clear(columns=True)
        for bundle in self.bundles():
            grid.add_column(bundle, key=bundle)

        for host in self.state.hosts:
            plan = self.state.plans.get(host.name)
            values = []
            for bundle in self.bundles():
                entries = [
                    e for e in (plan.entries if plan else []) if e.bundle == bundle
                ]
                status = worst_status(entries) if entries else "skipped"
                label = STATUS_SYMBOLS.get(status, "?")
                values.append(f"[{STATUS_COLORS.get(status, 'white')}]{label}[/]")
            grid.add_row(*values, key=host.name, label=host.name)
        self._show_selected_host_plan()

    # --- plan preview --------------------------------------------------

    def selected_host(self) -> str | None:
        grid = self.query_one("#grid", DataTable)
        try:
            key = grid.coordinate_to_cell_key(grid.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001 - no cursor yet
            key = None
        if key:
            return str(key)
        return self.state.hosts[0].name if self.state.hosts else None

    def on_data_table_row_highlighted(self, event) -> None:
        if event.data_table.id == "grid":
            self._show_selected_host_plan()

    def _show_selected_host_plan(self) -> None:
        if self._mode != "grid":
            return
        host = self.selected_host()
        entries_tbl = self.query_one("#entries", DataTable)
        entries_tbl.clear(columns=True)
        entries_tbl.add_column("bundle")
        entries_tbl.add_column("component")
        entries_tbl.add_column("op")
        entries_tbl.add_column("status")
        entries_tbl.add_column("detail")
        line = self.query_one("#statusline", Static)
        plan = self.state.plans.get(host) if host else None
        if plan is None:
            line.update(f"no plan for {host}" if host else "no host selected")
            return
        for e in plan.entries:
            status = str(e.status)
            entries_tbl.add_row(
                e.bundle,
                e.component,
                str(e.op),
                f"[{STATUS_COLORS.get(status, 'white')}]{status}[/]",
                e.detail,
            )
        line.update(
            f"{host}: {len(plan.entries)} entries, {len(plan.changes)} changes"
        )

    # --- apply ---------------------------------------------------------

    def action_apply(self) -> None:
        host = self.selected_host()
        if not host:
            return
        plan = self.state.plans.get(host)
        if plan is None:
            self.query_one("#statusline", Static).update(f"no plan for {host}")
            return
        self.push_screen(
            ConfirmScreen(f"Apply plan for {host}?"),
            callback=lambda confirmed, h=host, p=plan: self._run_apply(h, p)
            if confirmed
            else None,
        )

    def _run_apply(self, host: str, plan) -> None:
        """Stream apply results into the status line; ops may be absent."""
        line = self.query_one("#statusline", Static)
        try:
            from nira.ops.apply import apply_plan
        except ImportError as exc:
            self._apply_stream = [f"apply unavailable: {exc}"]
            line.update(self._apply_stream[0])
            return
        try:
            results = list(apply_plan(plan))
        except Exception as exc:  # noqa: BLE001 - surface op failures in UI
            line.update(f"apply failed: {exc}")
            return
        self._apply_stream = [str(r) for r in results]
        self.apply_results = self._apply_stream
        self.applied_host = host
        line.update(f"applied {host}: " + " | ".join(self._apply_stream))

    # --- doctor ---------------------------------------------------------

    def action_doctor(self) -> None:
        host = self.selected_host()
        if not host:
            return
        self._mode = "doctor"
        self._doctor_target = host
        entries_tbl = self.query_one("#entries", DataTable)
        entries_tbl.clear(columns=True)
        entries_tbl.add_column("check")
        entries_tbl.add_column("result")
        line = self.query_one("#statusline", Static)
        try:
            from nira.ops.doctor import run_checks
        except ImportError as exc:
            self._doctor_stream = [f"doctor unavailable: {exc}"]
            line.update(self._doctor_stream[0])
            return
        try:
            results = list(run_checks(host))
        except Exception as exc:  # noqa: BLE001 - surface op failures in UI
            line.update(f"doctor failed: {exc}")
            return
        self._doctor_stream = [str(r) for r in results]
        for r in self._doctor_stream:
            entries_tbl.add_row("check", r)
        line.update(f"doctor {host}: {len(results)} checks")

    # --- keys ------------------------------------------------------------

    def action_refresh(self) -> None:
        self._mode = "grid"
        self.refresh_plan()


def main() -> None:
    """Entry point for `nira-tui`: launch the dashboard on the fleet dir."""
    import os

    fleet = os.environ.get("NIRA_FLEET", str(Path.cwd()))
    NiraTui(Path(fleet)).run()
