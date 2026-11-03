#!/usr/bin/env python3
"""Check a warp map against the audio: how far the notes sit from the grid before and after.

The strong hits of the beat fall in three groups (from analyze_take.py's tracking):
  grid notes      on the map's snap grid: these are what the warp tightens
  in-between      on the tracker's finer lattice but between grid lines
                  (16ths and ghosts when snapping to 8ths): they move with the grid
  secondary       extra transients right after a note (buzz/roll inside a
                  beatboxed kick, flams, room hits): ignored
Grid notes are measured before warping two ways, then after:
  steady   one best-fit tempo through the whole beat (the most generous "no warp" case)
  set      the set tempo from the first downbeat (what a single warp marker gives you)
Finally, per drum class, where the markers sit relative to the attack start at a
10 % threshold: a few ms is normal; a large bias means markers on the wrong part of the transient.

usage: qc_grid.py <audio> <map.json> <analysis.json> [--out qc.json]
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from audio_hits import load_mono, AttackFinder

SNAP = {'16th': 0.25, '8th': 0.5, 'beat': 1.0, 'quarter': 1.0, 'bar': None}


def stats(d):
    d = np.abs(np.asarray(d))
    if not len(d):
        return dict(n=0)
    return dict(n=len(d), median_ms=round(float(np.median(d)), 1), p90_ms=round(float(np.percentile(d, 90)), 1),
                max_ms=round(float(d.max()), 1), within_5ms_pct=round(float(np.mean(d < 5) * 100)),
                within_15ms_pct=round(float(np.mean(d < 15) * 100)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('audio')
    ap.add_argument('map')
    ap.add_argument('analysis')
    ap.add_argument('--out', default='qc.json')
    a = ap.parse_args()
    W, A = json.load(open(a.map)), json.load(open(a.analysis))
    M = np.array(W['markers'])
    bpm, bpb = W['bpm'], W['beats_per_bar']
    g = SNAP[W['snap']] or float(bpb)
    spb = 60 / bpm
    k, phi = A['level']['beats_per_pulse'], A['level']['phase_pulse']
    t0, t1 = W['beat_span_s']
    hits = A['hits']
    groups = {'grid': [], 'between': [], 'secondary': []}
    for h in hits:
        if not h.get('strong') or not t0 - 0.02 <= h['t'] <= t1 + 0.02:
            continue
        if h['kind'] in ('int', 'half', 'quarter') and h['pos'] is not None:
            b = (h['pos'] - phi) * k
            groups['grid' if abs(b / g - round(b / g)) < 1e-6 else 'between'].append(h)
        else:
            groups['secondary'].append(h)

    off = lambda b, step: (b - np.round(b / step) * step) * spb * 1000
    t = np.array([h['t'] for h in groups['grid']])
    inner = M[(M[:, 0] >= t0 - 1e-6) & (M[:, 0] <= t1 + 1e-6)]
    fit = np.polyfit(inner[:, 0], inner[:, 1], 1)
    before_steady = off(np.polyval(fit, t), g)
    before_set = off(W['first_downbeat_clip_beat'] + (t - W['first_downbeat_s']) * bpm / 60, g)
    after = off(np.interp(t, M[:, 0], M[:, 1]), g)
    tb = np.array([h['t'] for h in groups['between']])
    fine = min(g / 2, 0.25)
    between = off(np.interp(tb, M[:, 0], M[:, 1]), fine) if len(tb) else []

    y, sr = load_mono(a.audio)
    af = AttackFinder(y, sr)
    T = np.array([h['t'] for h in hits])
    bias = {}
    for s, _ in inner:
        j = int(np.argmin(np.abs(T - s)))
        if abs(T[j] - s) > 0.003:
            continue                                   # interpolated or weak-hit marker
        a10, _ = af.attack(int(s * sr), before=0.02, after=0.05, frac=0.10)
        bias.setdefault(hits[j].get('cls', '?'), []).append((a10 - s) * 1000)

    res = dict(snap=W['snap'], bpm=bpm,
               grid_notes=dict(before_steady_tempo=stats(before_steady), before_set_tempo_one_marker=stats(before_set),
                               after=stats(after)),
               in_between_notes=dict(n=len(tb), vs_nearest=('16th' if fine == 0.25 else f'{fine} beat'),
                                     **({k_: v for k_, v in stats(between).items() if k_ != 'n'} if len(tb) else {})),
               secondary_transients_ignored=len(groups['secondary']),
               marker_vs_10pct_attack_ms={c: round(float(np.mean(v)), 1) for c, v in bias.items()})
    json.dump(res, open(a.out, 'w'), indent=1)
    f = lambda s: (f"median {s['median_ms']} ms, p90 {s['p90_ms']}, max {s['max_ms']} "
                   f"({s['within_15ms_pct']}% within 15 ms)") if s.get('n') else 'none'
    gn = res['grid_notes']
    print(f"{len(t)} grid notes ({W['snap']}) at {bpm:g} BPM")
    print('  before, one steady tempo   :', f(gn['before_steady_tempo']))
    print(f'  before, {bpm:g} BPM from beat 1:', f(gn['before_set_tempo_one_marker']))
    print('  after warp                 :', f(gn['after']))
    if len(tb):
        ib = res['in_between_notes']
        print(f"{len(tb)} in-between notes (moved with the grid, not pinned): median {ib['median_ms']} ms, "
              f"max {ib['max_ms']} ms from the nearest {ib['vs_nearest']}")
    print(f"{res['secondary_transients_ignored']} secondary transients ignored (buzz, flams, room)")
    print('markers vs 10 % attack start (ms, - = starts before the marker):',
          ', '.join(f'{c} {v:+.1f}' for c, v in res['marker_vs_10pct_attack_ms'].items()))
    print('wrote', a.out)


if __name__ == '__main__':
    main()
