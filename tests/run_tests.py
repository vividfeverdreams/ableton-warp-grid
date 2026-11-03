#!/usr/bin/env python3
"""Run analyze -> build -> qc on every synthetic case and check against the truth.

Checks per case: the tempo and the way it is counted (91 not 182, 140 not 70),
the average tempo, the first downbeat, that the beat region starts and ends at
the first and last note (talking excluded), and that every note on the 8th
grid lands on its exact beat after warping (no slipped beats, little error).

usage: run_tests.py [work dir] [--seeds N]   (exit code 1 if any check fails)
"""
import json, os, subprocess, sys, tempfile
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, HERE)
import make_synthetic as ms
import soundfile as sf


def run(*args):
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)
    return r.stdout


def main():
    args = sys.argv[1:]
    seeds = 1
    if '--seeds' in args:
        seeds = int(args[args.index('--seeds') + 1]); del args[args.index('--seeds'):args.index('--seeds') + 2]
    work = args[0] if args else tempfile.mkdtemp(prefix='warp-tests-')
    os.makedirs(work, exist_ok=True)
    fails, runs = 0, [(n, s) for s in range(1, seeds + 1) for n in ms.CASES]
    for name, seed in runs:
        y, truth = ms.make(name, seed)
        name = name if seed == 1 else f'{name}_s{seed}'
        wav = os.path.join(work, f'{name}.wav')
        sf.write(wav, y, ms.SR, subtype='PCM_24')
        an, mp, qc = (os.path.join(work, f'{name}.{x}.json') for x in ('analysis', 'map', 'qc'))
        try:
            run(os.path.join(SCRIPTS, 'analyze_take.py'), wav, '--out', an)
            run(os.path.join(SCRIPTS, 'build_warp_map.py'), an, '--out', mp)
            run(os.path.join(SCRIPTS, 'qc_grid.py'), wav, mp, an, '--out', qc)
        except RuntimeError as e:
            print(f'FAIL {name}: script error\n{e}'); fails += 1; continue
        A, W = json.load(open(an)), json.load(open(mp))
        M = np.array(W['markers'])
        # one note per 8th-grid slot: the earliest (a kick and hat on one slot, jittered apart, are one flam)
        first = {}
        for n in truth['notes']:
            if (n['beat'] * 2) == int(n['beat'] * 2) and (n['beat'] not in first or n['t'] < first[n['beat']]['t']):
                first[n['beat']] = n
        notes = sorted(first.values(), key=lambda n: n['t'])
        t = np.array([n['t'] for n in notes]); b = np.array([n['beat'] for n in notes])
        warped = np.interp(t, M[:, 0], M[:, 1])
        err = (warped - (W['first_downbeat_clip_beat'] + b)) * 60 / W['bpm'] * 1000
        eighth_ms = 30000 / W['bpm']
        checks = {
            'tempo': W['bpm'] == truth['expect']['bpm'],
            'count': A['level']['feel'] == truth['expect']['feel'],
            'mean': abs(A['bpm_mean'] - truth['mean_bpm']) < 0.5,
            # a kick and a hat on beat 1 jittered apart are a flam: the marker goes on the louder one
            'downbeat': abs(W['first_downbeat_s'] - truth['first_downbeat_s']) < 0.012,
            'region': abs(W['beat_span_s'][0] - min(t)) < 0.03 and abs(W['beat_span_s'][1] - max(t)) < 0.03,
            'no slip': np.abs(err).max() < eighth_ms / 4,
            'tight': np.percentile(np.abs(err), 95) < 12,
        }
        ok = all(checks.values())
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:16s} {W['bpm']:>4g} BPM ({A['level']['feel']}, mean {A['bpm_mean']:.2f} vs "
              f"{truth['mean_bpm']:.2f}), downbeat {1000 * (W['first_downbeat_s'] - truth['first_downbeat_s']):+.1f} ms, "
              f"notes {len(t)}: median {np.median(np.abs(err)):.1f} / p95 {np.percentile(np.abs(err), 95):.1f} / "
              f"max {np.abs(err).max():.1f} ms" + ('' if ok else '  failed: ' + ', '.join(k for k, v in checks.items() if not v)))
    print(f'{len(runs) - fails}/{len(runs)} cases pass (files in {work})')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
