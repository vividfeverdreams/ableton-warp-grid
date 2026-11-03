#!/usr/bin/env python3
"""Find the real tempo, downbeat and every on-grid hit of a played recording.

Run it on the most percussive file of the take (the drum stem when there are
stems). It
  1. detects every hit and its attack start (audio_hits.py),
  2. estimates the pulse (the hit spacing, usually the 8th or the quarter)
     from the autocorrelation of the onset curve, summed over 4 multiples so a
     syncopation can't pass for the pulse,
  3. follows the pulse through the whole file with a dynamic-programming beat
     tracker, then fits a local tempo line through neighbouring hits so every
     hit gets an exact grid position, however the tempo drifts,
  4. keeps the stretch where hits land tightly on that grid (a played beat
     does, talking and free time land at random) as the beat,
  5. picks the downbeat and how to count it (91 vs 182, 140 vs 70) from where
     the low end (kick) and the 1-5 kHz body (snare, clap) fall in the bar:
     snare on 2 and 4 inside --range wins, then snare on 3 (half-time);
     --bpm-near overrides,
  6. reports the average and local tempo, how far hits sit off one steady
     tempo, consistent push/pull per position (the feel), dropouts, and any
     sign of a slipped beat.

usage: analyze_take.py <audio> [--out analysis.json] [--start S] [--end S]
                       [--bpm-near BPM] [--range 80-180] [--beats-per-bar 4]
                       [--plot analysis.png]
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from audio_hits import HOP, load_mono, detect_hits, classify


def acf(x):
    x = x - x.mean()
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    r = np.fft.irfft(f * np.conj(f))[:n]
    return r / (r[0] + 1e-12)


def find_regions(env, sr):
    """Stretches where the onset envelope is periodic (3 s windows, merged across gaps < 4 s)."""
    fr = sr / HOP
    W, H = int(3 * fr), int(0.5 * fr)
    lo, hi = int(0.2 * fr), int(1.0 * fr)
    starts = list(range(0, max(1, len(env) - W), H))
    score, energy = [], []
    for s in starts:
        w = env[s:s + W]
        r = acf(w)
        score.append(r[lo:hi].max() if len(r) > hi else 0.0)
        energy.append(w.mean())
    score, energy = np.array(score), np.array(energy)
    active = energy > 0.15 * np.percentile(energy, 90)
    if not active.any():
        return [[0.0, len(env) / fr]]
    thr = max(0.25, 0.5 * np.percentile(score[active], 90))
    segs = []
    for i, s in enumerate(starts):
        if active[i] and score[i] >= thr:
            a, b = s / fr, (s + W) / fr
            if segs and a - segs[-1][1] < 4.0:
                segs[-1][1] = b
            else:
                segs.append([a, b])
    return segs or [[0.0, len(env) / fr]]


def estimate_pulse(env, sr, a, b):
    """Hit spacing in seconds: the lag in 0.2-0.6 s whose first four multiples all recur."""
    fr = sr / HOP
    r = acf(env[int(a * fr):int(b * fr)])
    rr = lambda lag: r[max(0, int(round(lag * fr)) - 2):int(round(lag * fr)) + 3].max() if lag * fr + 3 < len(r) else 0.0
    lags = np.arange(int(0.2 * fr), int(0.6 * fr) + 1) / fr
    S = np.array([sum(rr(m * l) for m in range(1, 5)) for l in lags])
    i = int(np.argmax(S))
    if 0 < i < len(S) - 1:                           # parabolic refinement
        y0, y1, y2 = S[i - 1], S[i], S[i + 1]
        return float(lags[i] + np.clip(0.5 * (y0 - y2) / (y0 - 2 * y1 + y2 + 1e-12), -0.5, 0.5) / fr)
    return float(lags[i])


def dp_beats(env, fr, P0, tight=100.0):
    """Pulse times through the whole envelope: dynamic programming that rewards onsets
    and penalises spacing away from P0 (log-squared), so drift is followed smoothly."""
    o = env / (env.std() + 1e-9)
    P = P0 * fr
    taus = np.arange(int(round(P * 0.5)), int(round(P * 2)) + 1)
    pen = -tight * np.log(taus / P) ** 2
    score = o.astype(float).copy()
    back = np.full(len(o), -1)
    for t in range(taus[0], len(o)):
        c = t - taus
        ok = c >= 0
        v = score[c[ok]] + pen[ok]
        j = int(np.argmax(v))
        score[t] = o[t] + v[j]
        back[t] = c[ok][j]
    tail = max(0, len(o) - int(round(P)))
    t = tail + int(np.argmax(score[tail:]))
    beats = []
    while t >= 0:
        beats.append(t); t = back[t]
    return np.array(beats[::-1]) / fr


def local_fit(t, y, w, span=3.0):
    """Leave-one-out value at each t from a weighted line through the other points within +-span s."""
    out = np.full(len(t), np.nan)
    for i, x in enumerate(t):
        m = (np.abs(t - x) < span) & (w > 0) & (np.arange(len(t)) != i)
        if m.sum() < 3:
            continue
        sw = np.sqrt(w[m])
        A = np.vstack([t[m] - x, np.ones(m.sum())]).T * sw[:, None]
        out[i] = np.linalg.lstsq(A, y[m] * sw, rcond=None)[0][1]
    return out


def refine(ts, pos, solid, step, iters):
    """Snap to the lattice, fit a local tempo line through the hits that fit, re-place every hit; repeat."""
    for _ in range(iters):
        lat = np.round(pos / step) * step
        w = (solid & (np.abs(pos - lat) < 0.2 * step)).astype(float)
        new = local_fit(ts, lat, w)
        pos = np.where(np.isnan(new), pos, new)
    return pos


def beat_segment(ts, solid, fit, tight):
    """The longest run of solid hits sitting on the grid (dropouts up to 4 s, stray sounds in them, allowed).

    A hit is in the beat when the solid hits in the 2.5 s before or after it
    mostly fit (>= 80 %; a played push or drag still fits, talking lands at
    random); the run starts and ends on hits that fit tightly and whose next
    five solid hits (inward) fit, one flam or second transient allowed."""
    idx = np.where(solid)[0]
    if len(idx) == 0:
        return None

    def frac(i, d):
        m = solid & ((ts - ts[i]) * d >= 0) & (np.abs(ts - ts[i]) <= 2.5)
        return np.mean(fit[m]) if m.sum() >= 4 else 0.0
    inbeat = {i: max(frac(i, 1), frac(i, -1)) >= 0.8 for i in idx}
    segs, cur, last, out = [], None, None, 0
    for i in idx:
        if not inbeat[i]:
            out += 1                                  # a breath in a dropout doesn't end the beat; three in a row do
            if out >= 3:
                cur = None
            continue
        if not fit[i]:
            continue
        if cur is not None and ts[i] - last <= 4.0:
            cur[1] = i
        else:
            cur = [i, i]; segs.append(cur)
        last, out = ts[i], 0
    if not segs:
        return None
    s = max(segs, key=lambda g: np.sum(fit[g[0]:g[1] + 1] & solid[g[0]:g[1] + 1]))
    sol = [i for i in idx if s[0] <= i <= s[1]]
    ok = lambda j, nb: tight[j] and sum(fit[x] for x in nb) >= len(nb) - 1
    a = next((n for n, j in enumerate(sol) if ok(j, sol[n + 1:n + 6])), 0)
    b = next((n for n in range(len(sol) - 1, -1, -1) if ok(sol[n], sol[max(0, n - 5):n])), len(sol) - 1)
    return sol[a], sol[b]


def level_scores(table, k, phi, bpb):
    """Backbeat and half-time evidence for one way of counting.

    For every bar, the hit on each beat (or silence) gives a low-end fraction
    (kick) and a 1-5 kHz fraction (snare/clap body). Backbeat = more body on
    the even beats (2, 4) than the odd ones plus more low end on 1 than on 2
    and 4; half-time = body on 3 and low end on 1 rather than 3."""
    feat = {}
    for pos, lo, mid in table:
        b = (pos - phi) * k
        if abs(b - round(b)) > 1e-6:
            continue
        bar, q = divmod(int(round(b)), bpb)
        L, M = feat.get((bar, q), (0.0, 0.0))
        feat[(bar, q)] = (max(L, lo), max(M, mid))
    bars = sorted({bar for bar, q in feat})
    if len(bars) < 2:
        return 0.0, 0.0
    L = [np.mean([feat.get((b, q), (0, 0))[0] for b in bars]) for q in range(bpb)]
    M = [np.mean([feat.get((b, q), (0, 0))[1] for b in bars]) for q in range(bpb)]
    odd, even = list(range(1, bpb, 2)), list(range(0, bpb, 2))
    back = np.mean([M[q] for q in odd]) - np.mean([M[q] for q in even]) + L[0] - np.mean([L[q] for q in odd])
    if bpb % 2 == 0:
        h = bpb // 2
        half = M[h] - np.mean([M[q] for q in range(bpb) if q != h]) + L[0] - L[h]
    else:
        half = -9.0
    return float(back), float(half)


def choose_level(table, first_pos, P, bpb, rng, bpm_near):
    cands = []
    for k in (0.25, 0.5, 1.0):
        bpm = 60 * k / P
        if not 50 <= bpm <= 220:
            continue
        best = None
        # a quarter-note pulse may have locked to the off-beat 8ths, so try half-pulse phases too
        for phi in np.arange(0, bpb / k, 0.5 if k >= 1 else 1.0):
            phi = float(phi)
            back, half = level_scores(table, k, phi, bpb)
            s, feel = (back, 'backbeat') if back >= half - 0.05 else (half - 0.05, 'half-time')
            if abs(np.mod((first_pos - phi) * k, bpb)) < 1e-6:
                s += 0.03                              # ties (four-on-the-floor): the take starts on a downbeat
            if best is None or s > best['score']:
                best = dict(beats_per_pulse=k, phase_pulse=phi, bpm=bpm, feel=feel, score=round(s, 3),
                            backbeat=round(back, 2), half_time=round(half, 2))
        cands.append(best)
    if not cands:
        raise SystemExit(f'pulse {P:.3f}s gives no tempo in 50-220 BPM; pass --start/--end around the beat')
    if bpm_near:
        pick = min(cands, key=lambda c: abs(c['bpm'] - bpm_near))
        pick['why'] = f'closest to --bpm-near {bpm_near:g}'
        return pick, cands
    inr = [c for c in cands if rng[0] <= c['bpm'] <= rng[1]] or cands
    pick = max(inr, key=lambda c: c['score'])
    if pick['score'] < 0.08:
        pick = min(inr, key=lambda c: abs(c['bpm'] - 120))
        pick['feel'] = 'unclear'
        pick['why'] = 'no clear kick/snare pattern: tempo nearest 120 in range, downbeat by accent; check by ear'
    else:
        pick['why'] = f"{pick['feel']} pattern inside {rng[0]:g}-{rng[1]:g} BPM"
    return pick, cands


def accent_phase(anchors, k, bpb):
    """Fallback downbeat: the bar position with the most summed hit strength."""
    best, bp = -1, 0
    for phi in np.arange(0, bpb / k, 0.5 if k >= 1 else 1.0):
        phi = float(phi)
        s = sum(h['peak'] for h in anchors if abs(np.mod((h['pos'] - phi) * k, bpb)) < 1e-6)
        if s > best:
            best, bp = s, phi
    return bp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('audio')
    ap.add_argument('--out', default='analysis.json')
    ap.add_argument('--start', type=float)
    ap.add_argument('--end', type=float)
    ap.add_argument('--bpm-near', type=float)
    ap.add_argument('--range', default='80-180')
    ap.add_argument('--beats-per-bar', type=int, default=4)
    ap.add_argument('--plot')
    a = ap.parse_args()
    rng = [float(x) for x in a.range.split('-')]
    bpb = a.beats_per_bar

    y, sr = load_mono(a.audio)
    dur = len(y) / sr
    fr = sr / HOP
    hits, env = detect_hits(y, sr)
    classify(hits)
    regions = find_regions(env, sr)
    lo_t = a.start if a.start is not None else 0.0
    hi_t = a.end if a.end is not None else dur
    reg = [lo_t, hi_t] if (a.start is not None or a.end is not None) else max(regions, key=lambda r: r[1] - r[0])
    P0 = estimate_pulse(env, sr, *reg)

    strong = [h for h in hits if h['strong'] and lo_t <= h['t'] <= hi_t]
    if len(strong) < 8:
        raise SystemExit(f'only {len(strong)} strong hits in {lo_t:.1f}-{hi_t:.1f} s: nothing to grid')
    ts = np.array([h['t'] for h in strong])
    ref = np.percentile([h['peak'] for h in hits], 95)
    solid = np.array([h['peak'] >= 0.25 * ref for h in strong])
    # a hit right after another (within 0.3 pulse, max 100 ms) is the same note's buzz, roll or flam:
    # only the first one of each cluster counts when deciding where the beat is and how it moves
    primary = np.concatenate([[True], np.diff(ts) > min(0.3 * P0, 0.1)])
    solid = solid & primary

    beats = dp_beats(env[int(lo_t * fr):int(hi_t * fr)], fr, P0) + lo_t
    n = np.arange(len(beats), dtype=float)
    pos = np.interp(ts, beats, n)
    pos = np.where(ts < beats[0], (ts - beats[0]) / P0, pos)
    pos = np.where(ts > beats[-1], n[-1] + (ts - beats[-1]) / P0, pos)
    pos = refine(ts, pos, solid, 0.5, 2)
    # 16ths between pulses (DnB hats at a quarter-note pulse) need the finer lattice
    inr = solid & (ts >= reg[0]) & (ts <= reg[1])
    d = np.abs(pos * 2 - np.round(pos * 2)) / 2
    step = 0.25 if inr.any() and np.mean(d[inr] > 0.18) > 0.15 else 0.5
    pos = refine(ts, pos, solid, step, 3)
    lat = np.round(pos / step) * step
    r = np.abs(pos - lat)
    fit, tight = r < 0.3 * step, r < 0.2 * step
    seg = beat_segment(ts, solid, fit, tight)
    if seg is None:
        raise SystemExit('no stretch of the file sits on a steady grid: is this a played beat?')
    i0, i1 = seg
    # refit using only hits inside the beat, so talking next to it can't bend the tempo line at the edges
    inside = (ts >= ts[i0] - 1e-6) & (ts <= ts[i1] + 1e-6)
    pos = refine(ts, pos, solid & inside, step, 2)
    lat = np.round(pos / step) * step
    r = np.abs(pos - lat)
    fit, tight = r < 0.3 * step, r < 0.2 * step
    # hits just outside that still fit (a soft last hat, a pushed last note) belong to it;
    # misfits in between (flams, second transients) are stepped over
    for d in (-1, 1):
        e, j = (i0, i0 - 1) if d < 0 else (i1, i1 + 1)
        while 0 <= j < len(ts) and abs(ts[j] - ts[e]) < 1.25 * P0:
            if fit[j]:
                e = j
            j += d
        i0, i1 = (e, i1) if d < 0 else (i0, e)
    t0, t1 = ts[i0], ts[i1]
    print(f'pulse estimate {P0 * 1000:.1f} ms, lattice {step:g} pulse, beat found {t0:.3f}-{t1:.3f}s')

    kind_of = lambda p: {0.0: 'int', 0.5: 'half'}.get(float(np.mod(p, 1)), 'quarter')
    taken = set()
    for i, h in enumerate(strong):
        inside = t0 - 1e-6 <= h['t'] <= t1 + 1e-6
        if inside and fit[i] and lat[i] not in taken:  # one note per grid slot: the first transient
            h['pos'], h['kind'] = float(lat[i]), kind_of(lat[i])
            taken.add(lat[i])
        else:
            h['pos'], h['kind'] = None, None
        h['fit_residual'] = float(r[i])
    anc = [h for h in strong if h['kind'] in ('int', 'half')]
    at = np.array([h['t'] for h in anc]); an = np.array([h['pos'] for h in anc])
    keep = np.concatenate([[True], np.diff(an) > 0])  # one anchor per position
    at, an = at[keep], an[keep]

    for h in hits:                                      # every hit gets an interpolated position inside the beat
        h['pos_interp'] = float(np.interp(h['t'], at, an)) if at[0] - 0.05 <= h['t'] <= at[-1] + 0.05 else None
        h.setdefault('pos', None); h.setdefault('kind', None)

    P = float(np.polyfit(an, at, 1)[0])                # mean seconds per pulse
    table = [(h['pos'], h['lo'], h['mid']) for h in strong if h['pos'] is not None]
    level, cands = choose_level(table, an[0], P, bpb, rng, a.bpm_near)
    k = level['beats_per_pulse']
    if level['feel'] == 'unclear':
        level['phase_pulse'] = accent_phase(anc, k, bpb)
    phi = level['phase_pulse']
    bpm_mean = 60 * k / P
    for h in strong:                                  # finer than a 16th is a flam or a second transient, not a note
        if h['pos'] is not None and abs((h['pos'] - phi) * k * 4 - round((h['pos'] - phi) * k * 4)) > 1e-6:
            h['pos'], h['kind'] = None, None
    # an edge hit where the groove never plays is a stray sound that happened to fit: drop it
    slots = int(bpb * 4)
    q16 = lambda p: int(round((p - phi) * k * 4)) % slots
    on16 = [h for h in strong if h['pos'] is not None]
    nbars = max(1.0, (an[-1] - an[0]) * k / bpb)
    occ = np.bincount([q16(h['pos']) for h in on16], minlength=slots) / nbars
    trimmed = []
    for end in (0, -1):
        while len(on16) > 8 and occ[q16(on16[end]['pos'])] < 0.25:
            h = on16.pop(end); trimmed.append(round(h['t'], 3)); h['pos'], h['kind'] = None, None
    if trimmed:
        anc = [h for h in anc if h['pos'] is not None]
        at = np.array([h['t'] for h in anc]); an = np.array([h['pos'] for h in anc])
        keep = np.concatenate([[True], np.diff(an) > 0]); at, an = at[keep], an[keep]
        for h in hits:
            h['pos_interp'] = float(np.interp(h['t'], at, an)) if at[0] - 0.05 <= h['t'] <= at[-1] + 0.05 else None
        print(f'dropped stray edge hits at {trimmed} s (not where the groove plays)')
    n_db = phi + np.ceil((an[0] - phi) * k / bpb - 1e-9) * bpb / k     # first downbeat inside the beat
    t_db = float(np.interp(n_db, an, at))
    level['first_downbeat_pulse'] = float(n_db)

    # flux: hits against one steady tempo, and the local tempo through the take
    dev = (at - np.polyval(np.polyfit(an, at, 1), an)) * 1000
    loc = []
    for i in range(len(at)):
        m = np.abs(at - at[i]) < 2.0
        if m.sum() >= 4:
            loc.append(60 * k / np.polyfit(an[m], at[m], 1)[0])
    # feel: average push/pull per position in the bar against the local tempo
    feel = {}
    for i in range(len(at)):
        m = (np.abs(at - at[i]) < 3.0) & (np.arange(len(at)) != i)
        if m.sum() < 4:
            continue
        resid = (at[i] - np.polyval(np.polyfit(an[m], at[m], 1), an[i])) * 1000
        beat = np.mod((an[i] - phi) * k, bpb)
        feel.setdefault(round(float(beat), 3), []).append(resid)
    names = lambda b: f"{int(b) + 1}" + {0.0: '', 0.25: 'e', 0.5: '&', 0.75: 'a'}.get(round(b - int(b), 2), f'+{b - int(b):.2f}')
    feel_rep = {names(b): round(float(np.mean(v)), 1) for b, v in sorted(feel.items()) if len(v) >= 3}
    def side_bpm(m):
        return 60 * k / np.polyfit(an[m], at[m], 1)[0] if m.sum() >= 4 else np.nan
    gaps = []
    for i in range(1, len(at)):
        if at[i] - at[i - 1] > 1.2:
            bridge = 60 * k * (an[i] - an[i - 1]) / (at[i] - at[i - 1])
            around = np.nanmean([side_bpm((at <= at[i - 1]) & (at > at[i - 1] - 3)), side_bpm((at >= at[i]) & (at < at[i] + 3))])
            gaps.append(dict(start=round(float(at[i - 1]), 3), end=round(float(at[i]), 3), pulses=float(an[i] - an[i - 1]),
                             bridge_bpm=round(float(bridge), 2), bpm_around=round(float(around), 2)))
    warnings = []
    for rg in regions:
        if rg[1] - rg[0] >= 6 and (rg[1] < at[0] - 1 or rg[0] > at[-1] + 1):
            warnings.append(f'rhythmic part at {rg[0]:.1f}-{rg[1]:.1f}s is outside the tracked beat: '
                            'if it belongs to the take, analyse it separately with --start/--end')
    for g in gaps:                                    # one 16th slipped across a 1.5 s gap is ~10 % off
        if abs(g['bridge_bpm'] / g['bpm_around'] - 1) > 0.06:
            warnings.append(f"gap {g['start']:.2f}-{g['end']:.2f}s: tempo across it ({g['bridge_bpm']}) is "
                            f"{(g['bridge_bpm'] / g['bpm_around'] - 1) * 100:+.1f}% off the tempo either side: a beat may have slipped")
    # slip check: does the kick pattern stay put on both sides of each gap?
    Q = int(round(bpb / k * 2))                           # bar length in half-pulses
    kicks = [(h['t'], int(round((h['pos'] - phi) * 2)) % Q) for h in anc if h['cls'] == 'kick' and h['pos'] is not None]
    if len(kicks) >= 8 and gaps:
        bounds = [at[0] - 1] + [g['end'] - 1e-3 for g in gaps] + [at[-1] + 1]
        glob = np.bincount([p for t, p in kicks], minlength=Q).astype(float)
        for s0, s1 in zip(bounds[:-1], bounds[1:]):
            sg = [p for t, p in kicks if s0 < t <= s1]
            if len(sg) < 4:
                continue
            hgram = np.bincount(sg, minlength=Q).astype(float)
            sc = [np.dot(np.roll(hgram, s), glob) for s in range(Q)]
            if int(np.argmax(sc)) != 0 and max(sc) > 1.3 * sc[0]:
                warnings.append(f'kick pattern between {s0:.1f}s and {s1:.1f}s is shifted '
                                f'{np.argmax(sc) / 2:g} pulses against the rest: possible slipped beat')

    res = dict(file=os.path.abspath(a.audio), sample_rate=sr, frames=len(y), duration_s=round(dur, 4),
               regions_s=[[round(x, 2) for x in rg] for rg in regions], region_s=[round(float(at[0]), 3), round(float(at[-1]), 3)],
               pulse_s=P, lattice_step=step, beats_per_bar=bpb, level=level, candidates=cands,
               first_downbeat_s=round(t_db, 4), bpm_mean=round(bpm_mean, 3),
               stats=dict(anchors=len(at), local_bpm_min=round(float(min(loc)), 1) if loc else None,
                          local_bpm_max=round(float(max(loc)), 1) if loc else None,
                          off_steady_grid_ms=dict(median=round(float(np.median(np.abs(dev))), 1),
                                                  p90=round(float(np.percentile(np.abs(dev), 90)), 1),
                                                  max=round(float(np.abs(dev).max()), 1)),
                          feel_ms=feel_rep),
               gaps=gaps, warnings=warnings,
               hits=[{kk: (round(v, 6) if isinstance(v, float) else v) for kk, v in h.items()} for h in hits])
    json.dump(res, open(a.out, 'w'), indent=1, default=lambda o: o.item() if hasattr(o, 'item') else str(o))

    print(f'{os.path.basename(a.audio)}: {dur:.2f} s, {len(hits)} hits ({len(strong)} strong)')
    print(f"rhythmic regions: {', '.join(f'{rg[0]:.1f}-{rg[1]:.1f}s' for rg in regions)}")
    print(f'beat tracked from {at[0]:.3f}s to {at[-1]:.3f}s, {len(at)} anchor hits, pulse {P * 1000:.1f} ms')
    print('ways to count it:')
    for c in cands:
        mark = '->' if c is level else '  '
        print(f"  {mark} {c['bpm']:.2f} BPM  {c['feel']:9s} score {c['score']:.2f}  "
              f"(backbeat evidence {c['backbeat']}, half-time evidence {c['half_time']})")
    print(f"chosen: {bpm_mean:.2f} BPM average ({level['why']}); first downbeat at {t_db:.3f}s")
    if loc:
        print(f'local tempo {min(loc):.1f}-{max(loc):.1f} BPM; hits vs one steady tempo: median '
              f"{res['stats']['off_steady_grid_ms']['median']} ms, max {res['stats']['off_steady_grid_ms']['max']} ms")
    if feel_rep:
        print('feel (avg ms vs local tempo, + = late): ' + ', '.join(f'{k_} {v:+.0f}' for k_, v in feel_rep.items()))
    for g in gaps:
        print(f"dropout {g['start']:.2f}-{g['end']:.2f}s bridged over {g['pulses']:g} pulses at {g['bridge_bpm']} BPM (either side: {g['bpm_around']})")
    for w in warnings:
        print('WARNING:', w)
    print('wrote', a.out)

    if a.plot:
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(14, 6), sharex=True)
        tt = np.arange(len(env)) * HOP / sr
        ax[0].plot(tt, env / env.max(), lw=0.5, color='0.5')
        ax[0].axvspan(at[0], at[-1], color='y', alpha=0.15)
        col = {'kick': 'r', 'snare': 'b', 'hat': 'g'}
        for h in anc:
            ax[0].axvline(h['t'], color=col[h['cls']], lw=0.6, alpha=0.7)
        ax[0].set_title('onset envelope, tracked beat (yellow), anchor hits (red kick, blue snare, green hat)')
        if loc:
            lt = [at[i] for i in range(len(at)) if (np.abs(at - at[i]) < 2.0).sum() >= 4]
            ax[1].plot(lt, loc, '.-', lw=0.8)
        ax[1].axhline(bpm_mean, color='k', lw=0.5)
        ax[1].set_ylabel('local BPM'); ax[1].set_xlabel('s')
        fig.tight_layout(); fig.savefig(a.plot, dpi=110)
        print('wrote', a.plot)


if __name__ == '__main__':
    main()
