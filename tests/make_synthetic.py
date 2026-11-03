#!/usr/bin/env python3
"""Synthetic drum takes with a known answer, for testing the scripts.

Each case is a played groove with tempo drift, timing jitter, a per-position
feel (late or early 8ths), velocity changes, dropouts and optional talking
before and after the beat. The truth file holds the nominal tempo, how it
should be counted, the first downbeat and every note's time and beat.

usage: make_synthetic.py <case|all> <out dir>
"""
import json, os, sys
import numpy as np
import soundfile as sf
import scipy.signal as ss

SR = 44100
# patterns per bar in 16ths (0-15): k = kick, s = snare, h = hat
CASES = {
    'boombap_91': dict(bpm=91, wander=1.6, walk=0.25, jitter=0.008, feel={14: 0.028, 4: -0.012},
                       k=[0, 6, 10], s=[4, 12], h=[2, 8, 14], bars=16, dropouts=[(5, 5.6), (10, 10.5)],
                       talk_before=14.0, talk_after=9.0, expect=dict(bpm=91, feel='backbeat')),
    'dnb_174': dict(bpm=174, wander=0.8, walk=0.15, jitter=0.005, feel={},
                    k=[0, 10], s=[4, 12], h=[2, 6, 8, 14], hat16=True, bars=24, dropouts=[(12, 12.5)],
                    talk_before=0, talk_after=0, expect=dict(bpm=174, feel='backbeat')),
    'halftime_140': dict(bpm=140, wander=1.0, walk=0.2, jitter=0.006, feel={},
                         k=[0, 6], s=[8], h=[0, 2, 4, 6, 8, 10, 12, 14], bars=20, dropouts=[],
                         talk_before=4.0, talk_after=0, expect=dict(bpm=140, feel='half-time')),
    'house_126': dict(bpm=126, wander=0.4, walk=0.05, jitter=0.004, feel={},
                      k=[0, 4, 8, 12], s=[4, 12], h=[2, 6, 10, 14], bars=24, dropouts=[(16, 17)],
                      talk_before=0, talk_after=3.0, expect=dict(bpm=126, feel='backbeat')),
    'rushing_100': dict(bpm=100, ramp=(96.0, 104.5), wander=0.5, walk=0.2, jitter=0.009, feel={6: 0.015},
                        k=[0, 7, 10], s=[4, 12], h=[2, 6, 8, 14], bars=24, dropouts=[(8, 8.5), (15, 15.75)],
                        talk_before=6.0, talk_after=4.0, expect=dict(bpm=100, feel='backbeat')),
}


def kick(rng):
    t = np.arange(int(0.4 * SR)) / SR
    ph = 2 * np.pi * np.cumsum(48 + 110 * np.exp(-t / 0.03)) / SR
    y = np.sin(ph) * np.exp(-t / 0.22)
    y[:88] += rng.normal(0, 0.3, 88) * np.linspace(1, 0, 88)
    return y


def snare(rng):
    t = np.arange(int(0.3 * SR)) / SR
    n = ss.sosfilt(ss.butter(2, [900, 6000], btype='band', fs=SR, output='sos'), rng.normal(0, 1, len(t)))
    return 0.45 * np.sin(2 * np.pi * 190 * t) * np.exp(-t / 0.07) + 0.9 * n * np.exp(-t / 0.11)


def hat(rng):
    t = np.arange(int(0.08 * SR)) / SR
    n = ss.sosfilt(ss.butter(4, 7000, btype='high', fs=SR, output='sos'), rng.normal(0, 1, len(t)))
    return 0.9 * n * np.exp(-t / 0.025)


def talk(rng, dur):
    """Speech-like: syllables of band-limited noise and buzz at irregular times."""
    y = np.zeros(int(dur * SR))
    t = rng.uniform(0, 0.3)
    while t < dur - 0.3:
        n = int(rng.uniform(0.08, 0.25) * SR)
        env = np.minimum(np.linspace(0, 1, n) * 6, 1) * np.exp(-np.arange(n) / (0.07 * SR))
        f0 = rng.uniform(110, 210)
        buzz = ss.sawtooth(2 * np.pi * f0 * np.arange(n) / SR) * 0.4
        noise = ss.sosfilt(ss.butter(2, [300, 2800], btype='band', fs=SR, output='sos'), rng.normal(0, 1, n))
        i = int(t * SR)
        y[i:i + n] += rng.uniform(0.1, 0.35) * env * (buzz + noise)
        t += rng.exponential(0.19) + 0.06
    return y


def make(name, seed=1):
    c = CASES[name]
    rng = np.random.default_rng(seed)
    n16 = c['bars'] * 16
    if 'ramp' in c:
        base = np.linspace(*c['ramp'], n16)
    else:
        base = np.full(n16, float(c['bpm']))
    walk = np.cumsum(rng.normal(0, c['walk'], n16 // 4)).repeat(4)[:n16]
    walk -= walk.mean()
    bpm = base + c['wander'] * np.sin(2 * np.pi * np.arange(n16) / (n16 / 2.3)) + np.clip(walk, -2, 2)
    grid = np.concatenate([[0], np.cumsum(15.0 / bpm)])[:n16]          # nominal time of every 16th
    t0 = c['talk_before'] + 0.6
    sounds = {'k': kick(rng), 's': snare(rng), 'h': hat(rng)}
    hits = []
    for i in range(n16):
        bar, p = divmod(i, 16)
        if any(a * 16 <= i < b * 16 for a, b in c['dropouts']):
            continue
        for kind in ('k', 's', 'h'):
            pat = c[kind]
            if p in pat or (kind == 'h' and c.get('hat16') and p % 2 == 1 and rng.random() < 0.5):
                vel = rng.uniform(0.75, 1.0) if kind != 'h' else rng.uniform(0.25, 0.5)
                tt = t0 + grid[i] + c['feel'].get(p, 0) + rng.normal(0, c['jitter'])
                hits.append((tt, i / 4, kind, vel))
    end = t0 + grid[-1] + 15.0 / bpm[-1] + 0.5
    y = np.zeros(int((end + c['talk_after'] + 0.5) * SR))
    for tt, b, kind, vel in hits:
        s = sounds[kind]; i = int(round(tt * SR))
        y[i:i + len(s)] += vel * s[:len(y) - i]
    if c['talk_before']:
        y[:int(c['talk_before'] * SR)] += talk(rng, c['talk_before'])
    if c['talk_after']:
        a = int(end * SR); y[a:a + int(c['talk_after'] * SR)] += talk(rng, c['talk_after'])[:len(y) - a]
    y += rng.normal(0, 0.0005, len(y))
    y = 0.7 * y / np.abs(y).max()
    first_db = min(tt for tt, b, kind, vel in hits if b == 0)     # the first transient on beat 1
    truth = dict(case=name, expect=c['expect'], mean_bpm=float(60 * (n16 / 4) / (grid[-1] + 15 / bpm[-1])),
                 first_downbeat_s=first_db, beat_start_s=t0, beat_end_s=float(end - 0.5),
                 notes=[dict(t=float(tt), beat=float(b), kind=kind) for tt, b, kind, vel in sorted(hits)])
    return y.astype(np.float32), truth


def main():
    which, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    for name in (CASES if which == 'all' else [which]):
        y, truth = make(name)
        sf.write(os.path.join(out, f'{name}.wav'), y, SR, subtype='PCM_24')
        json.dump(truth, open(os.path.join(out, f'{name}.truth.json'), 'w'), indent=1)
        print(name, f'{len(y) / SR:.1f} s', f"{truth['mean_bpm']:.2f} BPM mean")


if __name__ == '__main__':
    main()
