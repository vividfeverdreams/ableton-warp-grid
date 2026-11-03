#!/usr/bin/env python3
"""Turn analyze_take.py's grid into a warp map: every on-grid hit pinned to its exact beat.

--snap decides how tight the grid is:
  16th / 8th (default)  every hit on that grid gets a marker: the flux AND the
                        player's push/pull are warped out (tightest, stacks with programmed drums)
  beat                  markers on the beats only: tempo drift goes, the swing inside each beat stays
  bar                   markers on the downbeats only: only the drift goes, the whole groove stays
Weak hits fill grid slots the strong ones left empty. Dropouts are bridged in a
straight line between the hits either side. A marker at the very start and end
of the file extends the intro and outro at the edge tempo, so talking or a
free-time intro plays at its own speed. Clip beats are laid out so the first
downbeat of the beat sits on a bar line and the file start is at beat >= 0.

usage: build_warp_map.py analysis.json [--out map.json] [--snap 8th]
                         [--first-bar N] [--bpm BPM]
"""
import argparse, json, math
import numpy as np

SNAP = {'16th': 0.25, '8th': 0.5, 'beat': 1.0, 'quarter': 1.0, 'bar': None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('analysis')
    ap.add_argument('--out', default='map.json')
    ap.add_argument('--snap', default='8th', choices=list(SNAP))
    ap.add_argument('--first-bar', type=int, help='clip bar (1-based) the first downbeat of the beat lands on')
    ap.add_argument('--bpm', type=float, help='set tempo to use (default: the average, rounded)')
    a = ap.parse_args()

    A = json.load(open(a.analysis))
    lv, bpb, P = A['level'], A['beats_per_bar'], A['pulse_s']
    k, phi = lv['beats_per_pulse'], lv['phase_pulse']
    g = SNAP[a.snap] or float(bpb)
    bpm = a.bpm or round(A['bpm_mean'])
    rel = lambda n: (n - phi) * k                       # pulse position -> beats from the take's bar grid
    on_grid = lambda b: abs(b / g - round(b / g)) < 1e-6
    hits = A['hits']
    ref = np.percentile([h['peak'] for h in hits], 95)

    slots = {}
    for h in hits:                                      # tracked hits first
        if h['kind'] in ('int', 'half', 'quarter') and h['pos'] is not None:
            b = rel(h['pos'])
            if on_grid(b) and b not in slots:
                slots[b] = (h['t'], 'hit')
    n_hit = len(slots)
    # weak hits fill empty slots, but not inside dropouts (> 1.2 s without a tracked hit),
    # where quiet sounds are usually breaths or bleed; the closest hit to each slot wins
    ht = np.array(sorted(t for t, _ in slots.values()))
    tol = 0.12 * k                                      # 0.12 pulse, in beats
    weak = {}
    for h in hits:
        if h['pos_interp'] is None or h['peak'] < 0.03 * ref:
            continue
        j = np.searchsorted(ht, h['t'])
        if j == 0 or j == len(ht) or ht[j] - ht[j - 1] > 1.2:
            continue
        b = rel(h['pos_interp'])
        s = round(b / g) * g
        if abs(b - s) < tol and s not in slots and (s not in weak or abs(b - s) < weak[s][0]):
            weak[s] = (abs(b - s), h['t'])
    for s, (_, t) in weak.items():
        slots[s] = (t, 'weak')
    M = sorted((t, b, kind) for b, (t, kind) in slots.items())
    clean = []
    for m in M:                                         # time and beat must both increase
        if not clean or (m[0] > clean[-1][0] + 1e-4 and m[1] > clean[-1][1]):
            clean.append(m)
    M = clean

    # a hit in the wrong slot makes a much faster segment next to a much slower one: drop it
    # (40 %: a played push or drag of a 16th, even heavy swing, stays well under that)
    dropped = []
    while True:
        t = np.array([m[0] for m in M]); b = np.array([m[1] for m in M])
        sp = np.diff(t) / np.diff(b)                    # seconds per beat per segment
        bad = None
        for i in range(1, len(M) - 1):
            lo, hi = max(0, i - 8), min(len(sp), i + 8)
            med = np.median(sp[lo:hi])
            d0, d1 = sp[i - 1] / med - 1, sp[i] / med - 1
            if abs(d0) > 0.4 and abs(d1) > 0.4 and d0 * d1 < 0:
                bad = i; break
        if bad is None:
            break
        dropped.append(round(M[bad][0], 3)); M.pop(bad)

    t = np.array([m[0] for m in M]); b = np.array([m[1] for m in M])
    m0, m1 = b <= b[0] + 8, b >= b[-1] - 8              # edge tempo from the first and last two bars
    spb0 = np.polyfit(b[m0], t[m0], 1)[0] if m0.sum() >= 3 else P / k
    spb1 = np.polyfit(b[m1], t[m1], 1)[0] if m1.sum() >= 3 else P / k
    b_at0 = b[0] - t[0] / spb0
    b_db = rel(lv['first_downbeat_pulse'])             # the take's first downbeat, in the same beats
    if a.first_bar:
        E = (a.first_bar - 1) * bpb
        if E + (b_at0 - b_db) < -1e-6:
            raise SystemExit(f'--first-bar {a.first_bar} puts the file start before clip beat 0; '
                             f'use at least bar {math.ceil((b_db - b_at0) / bpb - 1e-9) + 1}')
    else:
        E = bpb * math.ceil((b_db - b_at0) / bpb - 1e-9)
    clip = lambda x: E + (x - b_db)
    dur = A['duration_s']
    marks = []
    if t[0] > 1e-3:
        marks.append((0.0, clip(b_at0)))
    marks += [(float(ti), clip(float(bi))) for ti, bi in zip(t, b)]
    if dur - t[-1] > 1e-3:
        marks.append((dur, clip(b[-1] + (dur - t[-1]) / spb1)))
    mk = np.array(marks)
    assert np.all(np.diff(mk[:, 0]) > 0) and np.all(np.diff(mk[:, 1]) > 0)

    seg_bpm = 60 * np.diff(b) / np.diff(t)
    stretch = lambda spb: (spb * bpm / 60 - 1) * 100
    out = dict(source=A['file'], duration_s=dur, sample_rate=A['sample_rate'], frames=A['frames'],
               bpm=bpm, bpm_mean=A['bpm_mean'], beats_per_bar=bpb, snap=a.snap,
               first_downbeat_s=float(np.interp(b_db, b, t)),
               first_downbeat_clip_beat=E, beat_span_s=[float(t[0]), float(t[-1])],
               markers=mk.tolist(),
               stats=dict(markers=len(mk), from_hits=n_hit, from_weak_hits=sum(1 for m in M if m[2] == 'weak'),
                          dropped_spikes=dropped, segment_bpm_min=round(float(seg_bpm.min()), 2),
                          segment_bpm_max=round(float(seg_bpm.max()), 2),
                          intro_stretch_pct=round(stretch(spb0), 2), outro_stretch_pct=round(stretch(spb1), 2)))
    json.dump(out, open(a.out, 'w'), indent=1)

    print(f'{len(mk)} warp markers ({a.snap} grid): {n_hit} on tracked hits, '
          f"{out['stats']['from_weak_hits']} on weak hits, {len(mk) - len(M)} at the file edges"
          + (f'; dropped misplaced hits at {dropped}' if dropped else ''))
    print(f'segment tempo between markers {seg_bpm.min():.1f}-{seg_bpm.max():.1f} BPM')
    print(f'set tempo {bpm:g} (average {A["bpm_mean"]:.2f}); first downbeat {out["first_downbeat_s"]:.3f}s -> clip bar {E // bpb + 1}')
    print(f'file start = clip beat {mk[0, 1]:.3f}, file end = clip beat {mk[-1, 1]:.3f} '
          f'({(mk[-1, 1] - mk[0, 1]) / bpb:.1f} bars)')
    fs = lambda p: f"{abs(p):.2f}% {'faster' if p > 0 else 'slower'}"
    print(f"at {bpm:g} BPM the intro plays {fs(out['stats']['intro_stretch_pct'])} and the outro "
          f"{fs(out['stats']['outro_stretch_pct'])} than recorded")
    print('wrote', a.out)


if __name__ == '__main__':
    main()
