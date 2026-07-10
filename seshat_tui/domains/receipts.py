"""domains/receipts.py — Receipts domain: hash chain drawn as a chain.

Sync only ever writes ~/.seshat/receipts/.last_synced (a bookkeeping
marker, not one of the three protected enforcement files) — it mirrors
cli.py's receipts_sync logic (imported constants/helpers only; the Click
command itself calls sys.exit() on failure, which would kill the TUI
process, so the POST + marker-write is reimplemented here with
notify()-based error handling instead).
"""

from __future__ import annotations

import hashlib
import json
import os

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import ListItem, ListView, Static, TabPane

import receipts as receipts_module
from vault import RECEIPTS_API_KEY_VAULT_KEY, Vault

from ..colors import COLORS
from ..graph import ReceiptNode
from ..palette import PaletteCommand
from ..widgets import EmptyState

_vault = Vault()

_BLOCK_FULL = "█"
_BLOCK_PART = "▓"
_BLOCK_REST = "░"


def _verify_chain() -> tuple[bool, int, str | None]:
    """Mirrors cli.py's receipts_verify() algorithm, read-only, no printing."""
    files = sorted(receipts_module.RECEIPTS_DIR.glob("*.json"))
    expected_previous = None
    total = 0
    for f in files:
        total += 1
        try:
            receipt = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            return False, total, f.name
        if receipt.get("previous_hash") != expected_previous:
            return False, total, f.name
        stored_hash = receipt.get("receipt_hash")
        verify_copy = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))
        computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if computed_hash != stored_hash:
            return False, total, f.name
        expected_previous = stored_hash
    return True, total, None


class ReceiptsDomainMixin:
    def compose_receipts(self):
        with TabPane("⧫ Receipts", id="tab-receipts"):
            yield Static(self._receipts_cmdstrip(), id="receipts-cmdstrip", classes="cmdstrip")
            yield Vertical(id="receipts-body")

    @staticmethod
    def _receipts_cmdstrip() -> str:
        return (
            "[#9A8B6E]actions[/#9A8B6E]  "
            "[#F6C56E][#F6C56E b]f[/#F6C56E b] follow[/#F6C56E]  "
            "[#C3B492][#E8AE52 b]v[/#E8AE52 b] verify chain[/#C3B492]  "
            "[#C3B492][#E8AE52 b]y[/#E8AE52 b] sync[/#C3B492]"
        )

    def on_mount_receipts(self) -> None:
        self.receipts_following = False
        self._receipts_cache: dict[str, dict] = {}
        self._receipts_follow_timer = None
        self._receipts_built = False
        self._receipts_detailed_key: str | None = None

    def get_receipts_palette_commands(self) -> list[PaletteCommand]:
        return [
            PaletteCommand("receipts", "⧫", "Toggle follow mode", "f", self.action_receipts_follow),
            PaletteCommand("receipts", "⧫", "Verify receipt chain integrity", "v", self.action_receipts_verify),
            PaletteCommand("receipts", "⧫", "Sync receipts to liminate.dev", "y", self.action_receipts_sync),
        ]

    def get_receipts_help(self) -> list[tuple[str, str]]:
        return [
            ("f", "toggle follow (live)"),
            ("v", "verify chain integrity"),
            ("y", "sync to liminate.dev"),
            ("↵", "inspect receipt"),
        ]

    @work(thread=True, group="receipts-refresh", exclusive=True)
    def refresh_receipts(self) -> None:
        rows = receipts_module.load(limit=80)
        intact, total, broken_at = _verify_chain()
        last_synced = self._receipts_last_synced()
        unsent = self._receipts_unsent_count(last_synced)
        self.call_from_thread(self._apply_receipts_data, rows, intact, total, broken_at, unsent)

    @staticmethod
    def _receipts_last_synced() -> str | None:
        from cli import LAST_SYNCED_PATH
        try:
            return LAST_SYNCED_PATH.read_text().strip() or None
        except FileNotFoundError:
            return None

    @staticmethod
    def _receipts_unsent_count(last_synced: str | None) -> int:
        files = sorted(receipts_module.RECEIPTS_DIR.glob("*.json"))
        if last_synced is None:
            return len(files)
        past = False
        count = 0
        for f in files:
            if not past:
                if f.name == last_synced:
                    past = True
                continue
            count += 1
        return count

    def _apply_receipts_data(self, rows: list, intact: bool, total: int, broken_at: str | None, unsent: int) -> None:
        body = self.query_one("#receipts-body", Vertical)

        if not rows:
            if getattr(self, "_receipts_built", False):
                body.remove_children()
                self._receipts_built = False
            elif body.children:
                return
            body.mount(EmptyState(
                "No receipts yet",
                "Receipts record every machine action Seshat takes — starts, stops, "
                "denials — as a hash-chained, tamper-evident log.",
                [("receipts are written automatically as you use Seshat", "seshat start <name>")],
                glyph="⧫",
            ))
            return

        follow_label = "[#74C767]●[/#74C767] following [#9A8B6E]· live[/#9A8B6E]" if getattr(self, "receipts_following", False) \
            else "[#9A8B6E]○ paused[/#9A8B6E]"
        pane_head_text = f"[b]Receipt chain[/b]           {follow_label}"

        self._receipts_cache = {}
        items = []
        for idx, r in enumerate(rows):
            key = r.get("receipt_hash", "")[:16] or str(id(r))
            self._receipts_cache[key] = r
            is_success = r.get("result", {}).get("status") == "success"
            link_color = COLORS["green"] if is_success else COLORS["red"]
            ts = r.get("timestamp", "")[:19].replace("T", " ")
            actor = r.get("actor", {})
            short_id = actor.get("session_id", "")[:14] or "—"
            target = r.get("target", {})
            target_str = target.get("project") or target.get("group") or target.get("key") or "—"
            # A connector line above every node but the first draws the
            # chain as a chain (nodes joined by │) inside a ListView, since
            # ListView expects homogeneous ListItem children — there's no
            # separate non-selectable connector row between items.
            connector = "[#5F5340]  │[/#5F5340]\n" if idx > 0 else ""
            content = f"{connector}[{link_color}]◆[/{link_color}] [#9A8B6E]{ts}[/#9A8B6E]  [b]{r.get('action', '')}[/b] [#9A8B6E]·[/#9A8B6E] {target_str}   [#B78FE0]{short_id}[/#B78FE0]"
            if not is_success:
                reason = r.get("result", {}).get("error") or (r.get("result", {}).get("rule") and f"forbid: {r['result']['rule']}") or "denied"
                content += f"\n  [#5F5340]└[/#5F5340] [#DD6E5A]{reason}[/#DD6E5A]"
            items.append(ListItem(Static(content), id=f"rk-{key}"))

        bar = _BLOCK_FULL * 12 if intact else (_BLOCK_PART * 6 + _BLOCK_REST * 6)
        bar_color = COLORS["green"] if intact else COLORS["orange"]
        head_hash = rows[0].get("receipt_hash", "")[:14] + "…" if rows else "—"
        sync_bar_filled = min(int((1 - min(unsent, total or 1) / max(total, 1)) * 12), 12)
        sync_bar = _BLOCK_PART * sync_bar_filled + _BLOCK_REST * (12 - sync_bar_filled)

        integ_text = (
            f"[#9A8B6E b]INTEGRITY[/#9A8B6E b]\n"
            f"[{bar_color}]{bar}[/{bar_color}]\n"
            f"chain     [{COLORS['green'] if intact else COLORS['red']}]"
            f"{'intact' if intact else f'broken at {broken_at}'}[/]\n"
            f"hashed    {total}\n"
            f"\n[#9A8B6E b]SYNC[/#9A8B6E b]\n"
            f"[#E8A052]{sync_bar}[/#E8A052]\n"
            f"unsent    [#E8A052]{unsent}[/#E8A052]\n"
            f"\n[#9A8B6E b]HEAD[/#9A8B6E b]\n"
            f"hash      [#63C6BE]{head_hash}[/#63C6BE]"
        )

        if getattr(self, "_receipts_built", False):
            self.query_one("#receipts-pane .pane-head", Static).update(pane_head_text)
            self.query_one("#receipts-integ", Vertical).query_one(Static).update(integ_text)
            chain_list = self.query_one("#receipts-chain", ListView)
            chain_list.clear()
            chain_list.call_after_refresh(chain_list.extend, items)
            return

        body.remove_children()
        pane_head = Static(pane_head_text, classes="pane-head")
        chain_list = ListView(*items, id="receipts-chain")
        pane = Vertical(pane_head, chain_list, id="receipts-pane", classes="pane")
        integ = Vertical(Static(integ_text), id="receipts-integ", classes="integ")
        body.mount(Horizontal(pane, integ, id="receipts-work", classes="work"))
        self._receipts_built = True

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "receipts-chain" or event.item is None or not event.item.id:
            return
        key = event.item.id[len("rk-"):]
        receipt = self._receipts_cache.get(key)
        if not receipt:
            return
        if key == self._receipts_detailed_key:
            self.push_drill(ReceiptNode(receipt))
            return
        self._receipts_detailed_key = key
        self.notify(
            f"{receipt.get('action')} · {json.dumps(receipt.get('target', {}))}",
            title=f"Receipt {key}",
            timeout=6,
        )

    # ── Actions ──────────────────────────────────────────────────────────

    def action_receipts_follow(self) -> None:
        if self._current_domain() != "receipts":
            return
        self.receipts_following = not getattr(self, "receipts_following", False)
        if self.receipts_following:
            self._receipts_follow_timer = self.set_interval(2, self.refresh_receipts)
        elif self._receipts_follow_timer:
            self._receipts_follow_timer.stop()
            self._receipts_follow_timer = None
        self.refresh_receipts()

    def action_receipts_verify(self) -> None:
        if self._current_domain() != "receipts":
            return
        intact, total, broken_at = _verify_chain()
        if intact:
            self.notify(f"Chain intact — {total} receipt(s) verified.", title="Verify")
        else:
            self.notify(f"Chain broken at {broken_at} (of {total}).", title="Verify", severity="error")
        self._show_echo("receipts", "seshat receipts verify", "read-only")

    def action_receipts_sync(self) -> None:
        if self._current_domain() != "receipts":
            return
        self._do_receipts_sync()

    @work(thread=True)
    def _do_receipts_sync(self) -> None:
        import httpx
        from cli import LAST_SYNCED_PATH, RECEIPTS_API_DEFAULT, SESSION_ID as CLI_SESSION_ID

        api_key = _vault.get(RECEIPTS_API_KEY_VAULT_KEY)
        if not api_key:
            self.call_from_thread(
                self.notify,
                f"No Receipts API key configured. Set one with: seshat vault set {RECEIPTS_API_KEY_VAULT_KEY} <key>",
                severity="error",
            )
            return

        last_synced = self._receipts_last_synced()
        files = sorted(receipts_module.RECEIPTS_DIR.glob("*.json"))
        past = last_synced is None
        unsent = []
        for f in files:
            if not past:
                if f.name == last_synced:
                    past = True
                continue
            try:
                unsent.append((f.name, json.loads(f.read_text())))
            except (json.JSONDecodeError, OSError):
                continue

        if not unsent:
            self.call_from_thread(self.notify, "All receipts are synced.")
            return

        api_base = os.environ.get("SESHAT_RECEIPTS_API", RECEIPTS_API_DEFAULT)
        try:
            resp = httpx.post(
                f"{api_base}/api/v1/ingest",
                json={"receipts": [r for _, r in unsent], "source": "seshat", "session_id": CLI_SESSION_ID},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=30.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            self.call_from_thread(self.notify, f"Sync failed: {e}", severity="error")
            return

        LAST_SYNCED_PATH.write_text(unsent[-1][0] + "\n")
        result = resp.json()
        ingested = result.get("ingested", len(unsent))
        self.call_from_thread(self.notify, f"{ingested} receipt(s) synced to {api_base}.")
        self.call_from_thread(self.refresh_receipts)
        self.call_from_thread(self._show_echo, "receipts", "seshat receipts sync", f"{ingested} receipt(s) synced")
