"""Spectrum-sharing policy for the RAN control dApps hackathon.

Drop this file over ``examples/hackathon_policy.py`` in the dApp library and run::

    python3 -m examples.spectrum_dapp --link zmq --transport tcp --encoding-method asn1 \\
        --control -ru rfsim --num-prbs 106 --center-freq 3619200000 --e \\
        --policy examples.hackathon_policy:MyPolicy --demo-gui

``MyPolicy`` works in three layers, run on every observation:

1. Robust floor and z-score.
   The noise floor and its spread are estimated from this observation alone. The floor is the
   median of the quiet PRBs and the spread is 1.4826 x their MAD, computed in two passes so the
   incumbents fall out of the reference set. Each PRB then gets
   ``z = (level - floor) / sigma``. Thresholds are in sigmas, not dB, so they hold at any noise
   level and for partial slots (fewer symbols give a wider spread, which the MAD picks up).
   PRBs that are blocked or already gathering evidence are left out of the reference set, so a
   constant incumbent never becomes the floor.

2. Evidence over time: a CUSUM on every PRB.
   ``S = clip(S + min(z, z_cap) - k, 0, s_max)``; a PRB is blocked when ``S >= h``. A strong
   incumbent trips it in one or two observations (there is also a one-observation fast path at
   ``z >= z_fast``), and a weak or duty-cycled one adds up over several. Pure noise drifts down
   by ``k`` per observation, so false blocks are rare. A PRB that was not measured in an
   observation (scheduled to the UE) keeps its evidence and gains none.
   Release needs evidence that the incumbent has gone: a second CUSUM,
   ``R = max(0, R + k_rel - clip(z, -3, 5))``, must reach ``h_rel``, and no observation may have
   reached ``z_hot`` for ``hold_s`` seconds. Duty-cycled incumbents therefore stay blocked
   through their off time, and a hopping tone frees its old PRB about 0.2 s after it leaves.
   A PRB more than ``leak_db`` (20 dB) below the strongest PRB on the carrier gains no evidence.
   The gNB's FFT spreads a strong incumbent's energy far beyond its PRBs (about 16 dB down next
   to it and 26-32 dB down across the rest of the carrier), and without this guard a -30 dBFS
   band blocked about 40 extra PRBs. In the generated scenarios, concurrent incumbents never
   differ by more than about 15 dB in level, so this does not hide a real one. A PRB right next to one the UE was
   scheduled on in this slot gains no evidence from that slot either, because the UE's bleed
   lands there; it still gains evidence from the sensing slot and the other slots.

3. Safety mask, applied last so nothing can override it.
   PRBs ``[0, protect_hi)`` carry the UE's PUCCH and the PRACH and are never blocked. PRBs
   ``[protect_hi, caution_hi)`` are blocked only as the lower end of a contiguous blocked run
   that reaches ``caution_hi``, so PUCCH energy that spills just above the protected zone cannot
   block on its own. The DC guard (the dApp strips it anyway) is never measured or returned.
   Confirmed PRBs are widened by ``dilate`` on each side; the scorer does not count the PRBs
   next to an incumbent as over-blocking, and the widening covers weaker edge PRBs of a band.

Every parameter can be changed without editing the file, as JSON in an environment variable::

    MYPOLICY_PARAMS='{"h": 12, "protect_hi": 14}' python3 -m examples.spectrum_dapp ...

Block and release decisions, plus a status line every 10 s, are written to /tmp/dapp.log
with the prefix ``[MyPolicy]``.
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np

from spectrum.policy import BaselinePolicy, Observation, Policy

log = logging.getLogger("dapp_logger")   # the dApp's own logger, which writes /tmp/dapp.log


class MyPolicy(Policy):
    """Safety mask + robust z-score floor + CUSUM evidence over time."""

    DEFAULTS = dict(
        # --- safety mask ---------------------------------------------------------------
        protect_hi=12,       # PRBs 0 .. protect_hi-1 are never blocked (PUCCH, PRACH)
        caution_hi=16,       # PRBs protect_hi .. caution_hi-1 only as the tail of a band reaching caution_hi
        protect_extra=[],    # any other PRBs never to block (for example a PUCCH found elsewhere)
        dc_guard=3,          # PRBs within this distance of the carrier centre are ignored
        # --- robust floor --------------------------------------------------------------
        min_ref=16,          # observations with fewer reference PRBs are skipped
        clip_z=3.0,          # the second pass of the floor drops PRBs above this z
        sigma_min_db=0.05,   # lower bound of sigma, for quantised or clipped levels
        # --- evidence: block -----------------------------------------------------------
        k=0.5,               # CUSUM drift: pure noise loses k per observation
        h=15.0,              # CUSUM threshold to block
        z_cap=10.0,          # largest z one observation can add (a strong hit needs two)
        z_fast=12.0,         # one observation at or above this blocks immediately
        s_max=45.0,          # cap on the stored evidence
        busy_frac=0.33,      # PRBs with S >= busy_frac*h stay out of the floor estimate
        leak_db=20.0,        # a PRB this far below the strongest PRB within leak_win gains no evidence
        leak_win=None,       #   (spectral leakage of a strong incumbent; None = whole carrier;
                             #    leak_db None turns the guard off)
        ue_edge_guard=True,  # a PRB next to one scheduled to the UE in this slot gains no evidence
        # --- evidence: release ---------------------------------------------------------
        k_rel=0.3,           # release CUSUM drift
        h_rel=8.0,           # release CUSUM threshold
        z_hot=4.0,           # an observation at or above this restarts the hold timer
        hold_s=0.2,          # minimum time since the last hot observation before release
        # --- output --------------------------------------------------------------------
        dilate=0,            # widen every blocked PRB by this many PRBs on each side
        log_changes=True,
        status_every_s=10.0,
    )

    def __init__(self, history_len: int = 64, **params) -> None:
        super().__init__(history_len)
        p = dict(self.DEFAULTS)
        env = os.environ.get("MYPOLICY_PARAMS", "").strip()
        if env:
            p.update(json.loads(env))
        p.update(params)
        unknown = set(p) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"MyPolicy: unknown parameters {sorted(unknown)}")
        self.p = p
        self._n = 0
        self.threshold_db = 0.0   # shown by the dashboard: floor + z_hot * sigma

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        super().reset()
        self._n = 0

    def _alloc(self, n: int) -> None:
        p = self.p
        self._n = n
        idx = np.arange(n)
        dc_c = n // 2
        dc = np.abs(idx - dc_c) <= p["dc_guard"]
        protect = idx < p["protect_hi"]
        for q in p["protect_extra"]:
            if 0 <= int(q) < n:
                protect[int(q)] = True
        caution = (idx >= p["protect_hi"]) & (idx < p["caution_hi"])
        self._allowed = ~protect & ~dc                   # may appear in the output
        self._eligible = self._allowed.copy()            # gathers evidence
        self._ref_zone = self._allowed & ~caution        # may serve as floor reference
        self._S = np.zeros(n)                            # evidence of presence
        self._R = np.zeros(n)                            # evidence of absence (blocked PRBs only)
        self._blocked = np.zeros(n, dtype=bool)          # internal decision, before the mask
        self._last_hot = np.full(n, -np.inf)
        self._out = frozenset()
        self._floor = np.nan
        self._sigma = np.nan
        self._n_obs = self._n_skip = 0
        self._t_status = None

    # ------------------------------------------------------------------ helpers

    def _robust_floor(self, x: np.ndarray) -> tuple[float, float]:
        """Median and MAD-sigma of x, then again without the PRBs above clip_z."""
        smin = self.p["sigma_min_db"]
        med = float(np.median(x))
        sig = max(1.4826 * float(np.median(np.abs(x - med))), smin)
        keep = x < med + self.p["clip_z"] * sig
        if keep.sum() >= self.p["min_ref"]:
            x = x[keep]
            med = float(np.median(x))
            sig = max(1.4826 * float(np.median(np.abs(x - med))), smin)
        return med, sig

    def _dilated(self, mask: np.ndarray, d: int) -> np.ndarray:
        out = mask.copy()
        for s in range(1, d + 1):
            out[:-s] |= mask[s:]
            out[s:] |= mask[:-s]
        return out

    def _output(self) -> set[int]:
        p = self.p
        core = self._blocked & self._allowed
        # Caution zone: keep a PRB only if the blocked run continues up to caution_hi.
        lo, hi = p["protect_hi"], min(p["caution_hi"], self._n - 1)
        for q in range(hi - 1, lo - 1, -1):
            core[q] = core[q] and core[q + 1]
        out = self._dilated(core, int(p["dilate"])) & self._allowed
        return set(np.flatnonzero(out).tolist())

    # ------------------------------------------------------------------ main

    def update(self, obs: Observation) -> set[int]:
        n = obs.num_prbs
        if n != self._n:
            self._alloc(n)
        p = self.p
        t = obs.t
        self._n_obs += 1

        level = np.asarray(obs.prb_power_db, dtype=np.float64)
        measured = np.isfinite(level) & self._eligible

        # 1. Robust floor from the quiet, measured PRBs of this observation.
        busy = self._dilated(self._blocked, 1) | (self._S >= p["busy_frac"] * p["h"])
        ref = measured & self._ref_zone & ~busy
        if ref.sum() < p["min_ref"]:
            self._n_skip += 1                     # too little to estimate the floor: no decision
            return self._finish(t)
        floor, sigma = self._robust_floor(level[ref])
        self._floor, self._sigma = floor, sigma
        self.threshold_db = floor + p["z_hot"] * sigma

        z = (level[measured] - floor) / sigma
        m_idx = np.flatnonzero(measured)

        # UE edge guard: the PRBs right next to the UE's allocation of this slot can carry its
        # spectral and CP-overlap bleed. They gain no evidence here (they do in other slots).
        z_ev = z
        if p["ue_edge_guard"]:
            raw = np.isfinite(level)
            gap = np.zeros(n, dtype=bool)
            gap[1:] |= ~raw[:-1]
            gap[:-1] |= ~raw[1:]
            gap[:p["protect_hi"] + 1] = False     # the protected zone is not the UE's data
            edge = gap[m_idx]
            if edge.any():
                z_ev = np.where(edge, np.minimum(z_ev, 0.0), z_ev)

        # Sidelobe guard: energy far below a strong neighbour is leakage, not a new incumbent.
        if p["leak_db"] is not None:
            lv = np.where(measured, level, -np.inf)
            if p["leak_win"] is None:
                peak = np.full(n, lv.max())
            else:
                w = int(p["leak_win"])
                padded = np.pad(lv, w, constant_values=-np.inf)
                peak = np.lib.stride_tricks.sliding_window_view(padded, 2 * w + 1).max(axis=1)
            side = (level[m_idx] < peak[m_idx] - p["leak_db"])
            if side.any():
                z_ev = np.where(side, np.minimum(z_ev, 0.0), z_ev)

        # 2a. Evidence of presence.
        S = self._S
        S[m_idx] = np.clip(S[m_idx] + np.minimum(z_ev, p["z_cap"]) - p["k"], 0.0, p["s_max"])
        hot = m_idx[z_ev >= p["z_hot"]]
        self._last_hot[hot] = t
        fast = m_idx[z_ev >= p["z_fast"]]
        new = (S >= p["h"]) & ~self._blocked
        new[fast] |= ~self._blocked[fast]
        if new.any():
            self._blocked |= new
            self._R[new] = 0.0
            self._last_hot[new] = np.maximum(self._last_hot[new], t)

        # 2b. Evidence of absence, for the PRBs that are blocked (and measured).
        b = self._blocked[m_idx]
        if b.any():
            bi = m_idx[b]
            self._R[bi] = np.maximum(0.0, self._R[bi] + p["k_rel"] - np.clip(z[b], -3.0, 5.0))
        gone = self._blocked & (self._R >= p["h_rel"]) & (t - self._last_hot >= p["hold_s"])
        if gone.any():
            self._blocked &= ~gone
            S[gone] = 0.0
            self._R[gone] = 0.0

        return self._finish(t)

    def _finish(self, t: float) -> set[int]:
        # 3. Safety mask and widening.
        out = self._output()
        if self.p["log_changes"]:
            fs = frozenset(out)
            if fs != self._out:
                add, rem = sorted(fs - self._out), sorted(self._out - fs)
                log.info(f"[MyPolicy] t={t:.3f} block {add} release {rem} -> {len(fs)} PRBs "
                         f"(floor {self._floor:.1f} dB, sigma {self._sigma:.2f} dB)")
            if self._t_status is None or t - self._t_status >= self.p["status_every_s"]:
                self._t_status = t
                log.info(f"[MyPolicy] status: {self._n_obs} obs, {self._n_skip} skipped, "
                         f"floor {self._floor:.1f} dB, sigma {self._sigma:.2f} dB, "
                         f"blocked {sorted(fs)}")
        self._out = frozenset(out)
        return out


class ReferencePolicy(BaselinePolicy):
    """A working but simple policy to compare against: ``--policy examples.hackathon_policy:ReferencePolicy``."""
