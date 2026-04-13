from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SenderPolicy:
    name: str
    # Risk cap override (cents) for this sender. If None => use global RiskAgent cap.
    risk_cap_cents_override: float | None = None

    # Allowed TP indices (1-based). If None => keep current default behavior.
    allowed_tp_indices: set[int] | None = None

    # Parsing / execution behavior
    ignore_limit_lot_lines: bool = False
    split_on_atau: bool = True

    # Entry zone behavior
    entry_zone_levels: str | None = None  # "low_mid_high" | None
    touch_entry_only: bool = False
    touch_tolerance_pips: float = 2.0
    touch_expiry_seconds: int = 80

    # Pips-based interpretation
    tp1_is_50_pips: bool = False


class PolicyAgent:
    """
    Menentukan policy berdasarkan sumber sinyal:
    - chat_id (channel asal)
    - forward_sender_id (untuk sinyal forward)
    """

    def __init__(self):
        # chat_id policies
        self._chat_policies: dict[int, SenderPolicy] = {
            -1003518891443: SenderPolicy(
                name="FOREX GOLD SNIPERS",
                risk_cap_cents_override=40.0,
                allowed_tp_indices={1, 2, 3},
                ignore_limit_lot_lines=False,
                split_on_atau=True,
            ),
            -1001702096089: SenderPolicy(
                name="THE TRADING DEITY",
                allowed_tp_indices={1, 2},
                ignore_limit_lot_lines=True,
                split_on_atau=True,
            ),
            -1002735612780: SenderPolicy(
                name="Alpha Institute VIP Signal",
                allowed_tp_indices={1},
                entry_zone_levels="low_mid_high",
                touch_entry_only=True,
                touch_tolerance_pips=2.0,
                touch_expiry_seconds=80,
                split_on_atau=True,
            ),
            -1003545354452: SenderPolicy(
                name="Alpha Institute Public",
                allowed_tp_indices={1},
                entry_zone_levels="low_mid_high",
                touch_entry_only=True,
                touch_tolerance_pips=2.0,
                touch_expiry_seconds=80,
                split_on_atau=True,
            ),
        }

        # forward sender policies
        self._fwd_sender_policies: dict[int, SenderPolicy] = {
            -8607169820: SenderPolicy(
                name="Forex Clarity VIP (forward sender)",
                allowed_tp_indices={1},
                entry_zone_levels="low_mid_high",
                touch_entry_only=True,
                touch_tolerance_pips=2.0,
                touch_expiry_seconds=80,
                split_on_atau=True,
                tp1_is_50_pips=True,
            )
        }

    def get_policy(self, *, chat_id: int | None, fwd_sender_id: int | None) -> SenderPolicy:
        if fwd_sender_id is not None:
            p = self._fwd_sender_policies.get(int(fwd_sender_id))
            if p is None:
                p = self._fwd_sender_policies.get(-int(fwd_sender_id))
            if p is not None:
                return p
        if chat_id is not None:
            p = self._chat_policies.get(int(chat_id))
            if p is not None:
                return p
        return SenderPolicy(name="default")

