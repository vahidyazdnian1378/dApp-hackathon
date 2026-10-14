# Spectrum-sharing dApp: NCSU Hackathon 2026, RAN Control dApps

A dApp policy that finds incumbents in the gNB's uplink spectrum and blocks their PRBs within
about 20 ms. It releases them about 0.2 s after they leave, and it never blocks the UE's own
control channels. It runs unattended across any number of scenarios with no retuning between
noise levels.

- **Team:** Vahid Yazdnian (vy9053@princeton.edu), Tianyi Zhang (tianyiz@iastate.edu)
- **dApp library fork:** https://github.com/vahidyazdnian1378/dApp-hackathon/tree/hackathon-2026
- **OAI gNB fork:** https://github.com/vahidyazdnian1378/openairinterface5g_dApp_Hackathon/tree/hackathon-2026

## 1. What is in the forks

| Repository | Base | Change |
|---|---|---|
| [dApp library](https://github.com/vahidyazdnian1378/dApp-hackathon/tree/hackathon-2026) | tag `0.3.1` (`44d4d16`) | `examples/hackathon_policy.py`: the policy (`MyPolicy`), described in section 2 |
| | | `src/spectrum/spectrum_dapp.py`: `_detector_run_interval` 4 → 1, so the policy sees every sensed slot (section 3, challenge 4) |
| [OAI gNB](https://github.com/vahidyazdnian1378/openairinterface5g_dApp_Hackathon/tree/hackathon-2026) | PR #495 (`e3-prb-blocking`, `301607c`) | No code changes. The gNB runs with the workshop's `conf/gnb.sa.band78.106prb.rfsim.e3.conf` |

## 2. Our approach

`MyPolicy.update(obs)` runs once per sensed slot and returns the PRBs to block:

```
level per PRB ──► robust floor + z-score ──► guards ──► presence evidence ──► absence evidence ──► safety mask ──► PRBs
```

### 2.1 Robust floor and z-score

Each slot, the noise floor is estimated from the quiet PRBs of that slot alone. These are the
PRBs that were measured, are outside the caution and DC zones, and are neither blocked nor
gathering evidence.

- Floor = median of these levels, spread (sigma) = 1.4826 × their median absolute deviation.
- Both are computed in two passes; the second drops anything above 3 sigma.
- Each PRB gets `z = (level − floor) / sigma`, its distance above the noise in standard
  deviations.
- Slots with fewer than 16 reference PRBs (the UE took most of the slot) are skipped.

Thresholds are in sigmas, not dB, so the same settings work at any noise level and for partial
slots with fewer symbols.

### 2.2 Evidence of presence (CUSUM)

`S = clip(S + min(z, 10) − 0.5, 0, 45)` for every measured PRB. A PRB is blocked when
`S ≥ 15`, or at once when a single slot has `z ≥ 12`.

- A strong incumbent is blocked in one slot.
- A weak or duty-cycled one adds up over several slots.
- Pure noise loses 0.5 per slot, so its evidence stays near zero.
- A PRB the UE used in a slot keeps its evidence and gains none.

### 2.3 Evidence of absence (release)

For blocked PRBs, `R = max(0, R + 0.3 − clip(z, −3, 5))`. A PRB is released when `R ≥ 8` and
no slot reached `z ≥ 4` for 0.2 s.

- Duty-cycled incumbents stay blocked through their off time.
- A hopping tone frees its old PRB about 0.2 s after it leaves.
- On release the PRB's evidence is reset, so every run starts clean.

### 2.4 Guards

These PRBs gain no evidence from the current slot:

- **Leakage guard:** a PRB more than 20 dB below the strongest PRB on the carrier. That energy
  is the spectral leakage of a strong incumbent, not a new one (section 3, challenge 3).
- **UE edge guard:** a PRB right next to one the UE used in this slot, where its bleed lands.

### 2.5 Safety mask

Applied last, so nothing above can override it:

- PRBs 0–11 (PUCCH and PRACH) are never blocked.
- PRBs 12–15 are blocked only as the lower end of a band that reaches PRB 16.
- The DC PRBs 50–56 are never blocked.

### 2.6 Blocking exactly the occupied PRBs

Our first version widened every blocked PRB by one PRB on each side (`dilate = 1`), to make
sure the weaker edges of a band were covered. On the live system that turned out to be a
problem:

- Next to a strong band, the leakage is only about 16 dB down, so the adjacent PRB is detected
  on its own. Widening then pushed the block one more PRB out on each side, for the whole
  time the band was on.
- Any single false detection became three blocked PRBs instead of one.

The final version blocks only the PRBs whose own evidence crosses the threshold
(`dilate = 0`). A band's edge PRBs carry the same energy as its centre, so they are found
anyway. With the injector's real waveform, a −30 dBFS band at PRBs 85–90 is blocked on its own
PRBs, plus at most the directly adjacent PRB, where that leakage is. Before the change,
widening alone added 2 more PRBs for as long as the band was on; after it, 0.

### Parameters

Every parameter can be changed without editing the file:
`MYPOLICY_PARAMS='{"h": 12}' python3 -m examples.spectrum_dapp ...`. Decisions are logged to
`/tmp/dapp.log` with the prefix `[MyPolicy]`.

| Parameter | Default | Role |
|---|---|---|
| `protect_hi`, `caution_hi` | 12, 16 | Safety mask |
| `k`, `h`, `z_cap`, `z_fast` | 0.5, 15, 10, 12 | Presence evidence and fast path |
| `k_rel`, `h_rel`, `z_hot`, `hold_s` | 0.3, 8, 4, 0.2 s | Release |
| `leak_db`, `leak_win` | 20 dB, whole carrier | Leakage guard |
| `ue_edge_guard` | on | UE edge guard |
| `dilate` | 0 | Block exactly the detected PRBs |

## 3. Challenges: what went wrong and how we fixed it

1. **A threshold in dB doesn't survive a change of noise level.** The given baseline blocks
   anything 12 dB above the median. In our simulator it scores 89.7 at −60 dBFS but only 62.4
   at −40 dBFS, where it misses levels 4–5 and `incumbent5_weak`. The per-PRB level of pure
   noise varies by only about 0.35 dB, so a 12 dB margin wastes most of the sensitivity.
   **Fix:** z-scores against a per-slot median/MAD floor (section 2.1).
2. **Blocking the UE's control channels drops it.** PUCCH and PRACH sit at PRBs 0–11, and the
   UE's PUCCH energy can look like an incumbent.
   **Fix:** the safety mask, applied after every other step, plus the caution zone 12–15
   (section 2.5). Live result: 0 PUCCH hits and 0 UL failures in every run.
3. **Strong incumbents caused massive over-blocking.**
   - *Symptom:* in the first live `incumbent2_band` run (−30 dBFS band, −50 dBFS noise),
     2600.7 PRB·s were blocked outside the band, about 39 extra PRBs for as long as it was on.
   - *Cause:* the gNB's per-symbol FFT spreads the injector's chirp far beyond its PRBs: about
     −16 dB next to it and −26 to −32 dB across the rest of the carrier. Against a 0.35 dB
     noise spread that is a large z-score. Our first guard only looked 3 PRBs to each side,
     and widening (section 2.6) made it worse.
   - *Fix:* a carrier-wide 20 dB guard, and blocking exactly the detected PRBs. An offline check
     with the injector's real waveform, demodulated like the gNB, gives 35–39 extra PRBs with
     the first version, 2 with the new guard but still widening, and 0 in the final version.
     Concurrent incumbents in the generated scenarios differ by at most about 15 dB in level,
     so the guard doesn't hide them. A weak tone next to a strong band is still caught.
4. **Some duty-cycled incumbents were never seen.** The stock dApp runs the detector on every
   4th sensed slot. Duty periods of 5/10/20 ms are multiples of the 5 ms TDD period, so for
   some start phases the incumbent is never on in a slot the policy sees. For example, 6 ms on
   every 20 ms is invisible for 15 % of phases (seed 3, level 4 in our simulator).
   **Fix:** `_detector_run_interval = 1`. This brings that case to 0 % invisible phases and
   seed 3, level 4 to 100 % protection. The policy costs about 0.3 ms per slot.
   **Remaining limit:** a 5 ms-period incumbent that is on only during downlink time can't be
   sensed by any policy (18–38 % of phases for 2–3 ms on-times).
5. **UE bleed next to its own allocation.** The dApp's own comments note CP-overlap bleed into
   partial sensing windows. In a simulator stress test with +2 dB at the UE's allocation
   edges, quiet runs had 56.7 PRB·s of false blocks.
   **Fix:** the UE edge guard (section 2.4), which brought them down to 0.
6. **Weak incumbents flapped at high noise.** On `incumbent5_weak` at −40 dBFS (z ≈ 1 per
   slot), the release evidence kept crossing its threshold.
   **Fix:** a slower release (`k_rel` 0.5 → 0.3, `h_rel` 10 → 8). Controls went from 46 to 27
   and protection from 98.0 to 98.7 %.

## 4. Results

**Demo:** a screen recording of the live demo on the announced seeds, showing the dashboard and
the score of each run, has been prepared and emailed with our submission.

### 4.1 Live (OAI RF simulator, one UE, 5 Mbit/s uplink iperf3)

| Scenario | Noise | Score | Notes |
|---|---|---|---|
| seed 4711, level 3 | −60 dBFS | 79.9 (+20 for uplink traffic = 99.9) | Protection ≈ 100 %, tone blocked in 15 ms, released in 0.215 s, UE kept, 0 PUCCH hits. The missing 20 points are the uplink-traffic term: the iperf3 flow could not run because the UE's data path to the core was down in this run. This is a setup issue, not a policy defect; the policy's own terms add up to 99.9 |

### 4.2 Offline simulator (final policy)

The simulator reproduces the slot timing, duty cycles, hops, UE occupancy and PUCCH leakage. It
uses the stock detector interval (every 4th sensed slot).

| Scenarios | Noise −60 dBFS | Noise −40 dBFS |
|---|---|---|
| Level 1, seeds 1–3 | 99.9–100.0 | 99.9–100.0 |
| Level 2, seeds 1–3 | 99.9 | 99.8–99.9 |
| Level 3, seeds 1–3 | 99.6–99.9 | 99.4–99.9 |
| Level 4, seeds 1–3 | 99.8–99.9; seed 3 invisible at interval 4 (0.0, see challenge 4) | 99.9 |
| Level 5, seeds 1–3 | 99.8–99.9 | 99.7–99.8 |
| `incumbent1`–`incumbent4` samples | 99.5–99.9 | 99.7–99.9 |
| `incumbent5_weak` | 99.9 | 96.7 |

- Typical time to block: 10–20 ms. Typical time to release: 0.2–0.35 s.
- The given baseline (`ReferencePolicy`) in the same simulator: mean 89.7 at −60 dBFS and 62.4
  at −40 dBFS.

## 5. Reproduce

**Versions:**
- dApp library `0.3.1` (commit `44d4d16`) with `libe3py` pinned to the installed libe3
  (`pip install -e ".[gui]" "libe3py==$(pkg-config --modversion libe3)"`)
- OAI PR #495 (`e3-prb-blocking`, commit `301607c`)
- workshop branch `hackathon-2026`

Build everything as in the workshop README. Then start each component in its own terminal,
in this order.

```bash
# 1. 5G core
cd ~/oai-workshops/cn && docker compose up -d
# 2. gNB, with timestamps so run_scenario.py can match the blocks to the scenario
cd ~/openairinterface5g/cmake_targets/ran_build/build && rm -f /dev/shm/e3_ran_buffers /dev/shm/e3_l2_sensing && sudo rm -rf /tmp/dapps && ./nr-softmodem -O ~/oai-workshops/hackathons/ran-control-dapps/conf/gnb.sa.band78.106prb.rfsim.e3.conf --rfsim -E --log_config.global_log_options level,nocolor,time 2>&1 | tee /tmp/gnb.log
# 3. Noise floor
cd ~/oai-workshops/hackathons/ran-control-dapps && tools/incumbent-injector/incumbent-injector --scenario scenarios/noise_floor.json --sample-rate 46080000
# 4. UE
cd ~/openairinterface5g/cmake_targets/ran_build/build && sudo ./nr-uesoftmodem -C 3619200000 -r 106 --numerology 1 --ssb 516 -O ~/oai-workshops/hackathons/ran-control-dapps/conf/ue.conf --rfsim -E
# 5. UE data path and iperf3 server for the uplink flow
ping -c 3 -I oaitun_ue1 192.168.70.135 && sudo docker exec oai-ext-dn iperf3 -s -D
# 6. dApp with the policy and the dashboard (port 5001)
source ~/dapp-venv/bin/activate && cd ~/dApp-library && python3 -m examples.spectrum_dapp --link zmq --transport tcp --encoding-method asn1 --control -ru rfsim --num-prbs 106 --center-freq 3619200000 --e --policy examples.hackathon_policy:MyPolicy --demo-gui
```

**Run scenarios back to back, without restarting anything.** Put the seeds and levels in the
loop, and add `--noise-dbfs -50` (or any level) to the command to change the noise.

```bash
cd ~/oai-workshops/hackathons/ran-control-dapps
for sl in "4711 3" "1 1" "31337 4" "2026 5"; do set -- $sl; tools/run_scenario.py --seed $1 --level $2 --gnb-log /tmp/gnb.log; done
for s in incumbent1_tone incumbent2_band incumbent3_hop incumbent4_two incumbent5_weak; do tools/run_scenario.py scenarios/$s.json --gnb-log /tmp/gnb.log; done
```

**Results of every run:**

```bash
cd ~/oai-workshops/hackathons/ran-control-dapps && python3 -c "
import json, glob
for f in sorted(glob.glob('results/*/score.json')):
    m = json.load(open(f)); p = m['protection']
    print(f\"{f.split('/')[1]:<28} protection {('-' if p is None else f'{100*p:5.1f}%')}  over-blocked {m['over_block_prb_s']:6.1f} PRB.s  UE {'DROPPED' if m['ue_dropped'] else 'kept':7}  score {m['score']}\")
"
```

## 6. Future directions

- **Shape-matched detection.** Test the strongest subcarrier of a PRB for tones, and sum z over
  runs of adjacent PRBs for bands. This would detect weak incumbents faster.
- **Incumbent tracking.** Group PRBs into incumbents, recognise a hop, and release the old
  position the moment the new one appears instead of after 0.2 s.
- **A leakage model that depends on distance.** Replace the flat 20 dB guard with the measured
  leakage profile of a strong incumbent, so a weak incumbent more than 20 dB below a strong
  one is still detected.
- **Exact noise statistics per PRB.** Pass the number of sensed symbols per PRB from the dApp
  to the policy, so partial slots get an exact sigma instead of a pooled one.
- **Per-PRB floor calibration.** Learn a per-PRB offset during quiet periods, for real radios
  whose noise floor is not flat across the carrier.
- **Bayesian per-PRB model.** Track the probability that each PRB is occupied, and set the
  block threshold from the cost of a miss versus a false block.
- **Learned classifier.** Train on SigMF recordings from the dashboard, once there are more
  signal types than tones and bands.
- **Protection inside the gNB.** Have OAI reject blocks on its configured PUCCH and PRACH PRBs,
  as a second line of defence behind the policy's safety mask.
- **Active sensing.** Use the sensing-policy control (Spectrum SM control 2) to request extra
  sensing slots when the evidence for a PRB is ambiguous.

## 7. AI tools used

We used **Claude (Anthropic)** for:
- brainstorming and comparing policy approaches;
- implementing the policy code, and creating an offline simulator and a leakage check that we used to test the algorithm;
- diagnosing the over-blocking (challenge 3).
