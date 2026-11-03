"""Shared audio helpers: load a file, detect hits, find each hit's attack start.

Hit detection is a log-mel spectral flux (lag 2, max-filtered across bands,
the "superflux" idea) on a 2.9 ms hop. Each detected hit is then refined to
its attack start on the sample grid: the Hilbert envelope of the 40 Hz
high-passed signal, walked back from the hit's peak to 25 % of the way up
from the floor. That point sits on the start of the transient for kicks,
snares and hats alike (checked against 10 % and 50 % thresholds), which is
where a warp marker belongs.
"""
import numpy as np
import scipy.signal as ss
import soundfile as sf

HOP = 128
N_FFT = 1024
N_MELS = 64


def load_mono(path):
    y, sr = sf.read(path, dtype='float32', always_2d=True)
    return y.mean(axis=1), sr


def _mel_filters(sr, n_fft, n_mels, fmin=30.0, fmax=None):
    fmax = fmax or sr / 2
    mel = lambda f: 2595 * np.log10(1 + f / 700)
    imel = lambda m: 700 * (10 ** (m / 2595) - 1)
    edges = imel(np.linspace(mel(fmin), mel(fmax), n_mels + 2))
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    fb = np.zeros((n_mels, len(freqs)), dtype=np.float32)
    for i in range(n_mels):
        lo, c, hi = edges[i:i + 3]
        up = (freqs - lo) / max(c - lo, 1e-9)
        down = (hi - freqs) / max(hi - c, 1e-9)
        fb[i] = np.clip(np.minimum(up, down), 0, None)
    return fb


def onset_envelope(y, sr, hop=HOP):
    """Spectral-flux onset strength, one value per hop (frame i is centred on sample i*hop)."""
    pad = np.pad(y, (N_FFT // 2, N_FFT // 2))
    frames = np.lib.stride_tricks.sliding_window_view(pad, N_FFT)[::hop]
    win = np.hanning(N_FFT).astype(np.float32)
    fb = _mel_filters(sr, N_FFT, N_MELS)
    mel = np.empty((len(frames), N_MELS), dtype=np.float32)
    for i in range(0, len(frames), 4096):           # chunked so long files stay light
        X = np.abs(np.fft.rfft(frames[i:i + 4096] * win, axis=1)) ** 2
        mel[i:i + 4096] = X @ fb.T
    db = 10 * np.log10(np.maximum(mel, 1e-10))
    db = np.maximum(db, db.max() - 80)
    ref = np.maximum(np.maximum(db, np.roll(db, 1, axis=1)), np.roll(db, -1, axis=1))
    flux = np.zeros(len(db), dtype=np.float32)
    flux[2:] = np.maximum(db[2:] - ref[:-2], 0).mean(axis=1)
    return flux


def pick_peaks(env, sr, hop=HOP, delta=0.03, wait_s=0.04):
    """Local maxima of the normalised envelope that clear a moving average by delta."""
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    e = env / (np.percentile(env, 99.9) + 1e-9)
    fr = sr / hop
    mx = maximum_filter1d(e, size=max(3, int(0.04 * fr)) | 1)
    av = uniform_filter1d(e, size=max(3, int(0.2 * fr)) | 1)
    cand = np.where((e == mx) & (e >= av + delta) & (e > 0))[0]
    out, last = [], -10 ** 9
    wait = int(wait_s * fr)
    for i in cand:
        if i - last >= wait:
            out.append(i); last = i
    return np.array(out, dtype=int)


class AttackFinder:
    """Refines detections to attack starts and measures a few features per hit."""

    def __init__(self, y, sr):
        self.sr = sr
        self.y = y
        self.yh = ss.sosfiltfilt(ss.butter(2, 40, btype='high', fs=sr, output='sos'), y).astype(np.float32)
        self.lo_sos = ss.butter(4, 150, btype='low', fs=sr, output='sos')
        self.smooth = np.ones(max(1, int(0.001 * sr))) / max(1, int(0.001 * sr))

    def _env(self, a, b):
        pad = int(0.02 * self.sr)
        a0, b0 = max(0, a - pad), min(len(self.yh), b + pad)
        e = np.abs(ss.hilbert(self.yh[a0:b0]))
        e = np.convolve(e, self.smooth, 'same')
        return e[a - a0:(a - a0) + (b - a)]

    def attack(self, centre, before=0.03, after=0.04, frac=0.25):
        """Attack start (s) and envelope peak of the hit detected near sample `centre`."""
        a = max(0, centre - int(before * self.sr)); b = min(len(self.y), centre + int(after * self.sr))
        if b - a < 8:
            return centre / self.sr, 0.0
        seg = self._env(a, b)
        i = int(np.argmax(seg)); peak = float(seg[i])
        floor = float(seg[:i + 1].min())
        thr = floor + frac * (peak - floor)
        while i > 0 and seg[i] > thr:
            i -= 1
        return (a + i) / self.sr, peak

    def features(self, t):
        """Low (<150 Hz), mid (1-5 kHz, snare/clap body) and high (>5 kHz) energy fractions of the 60 ms after t."""
        a = int(t * self.sr); seg = self.y[a:a + int(0.06 * self.sr)]
        if len(seg) < 64:
            return 0.0, 0.0, 0.0
        tot = float(np.sum(seg ** 2)) + 1e-12
        lo = float(np.sum(ss.sosfilt(self.lo_sos, seg) ** 2)) / tot
        X = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.rfftfreq(len(seg), 1 / self.sr)
        tx = X.sum() + 1e-12
        return min(lo, 1.0), float(X[(f >= 1000) & (f < 5000)].sum() / tx), float(X[f >= 5000].sum() / tx)


def detect_hits(y, sr, delta=0.03):
    """All hits: list of dicts with attack time, peak, low/high fractions."""
    env = onset_envelope(y, sr)
    peaks = pick_peaks(env, sr, delta=delta)
    af = AttackFinder(y, sr)
    hits = []
    for f in peaks:
        t, pk = af.attack(f * HOP)
        lo, mid, hi = af.features(t)
        hits.append({'t': t, 'peak': pk, 'lo': lo, 'mid': mid, 'hi': hi})
    hits.sort(key=lambda h: h['t'])
    # flams/double detections: keep one hit per 25 ms, the louder one
    out = []
    for h in hits:
        if out and h['t'] - out[-1]['t'] < 0.025:
            if h['peak'] > out[-1]['peak']:
                out[-1] = h
        else:
            out.append(h)
    return out, env


def classify(hits):
    """kick / snare / hat by spectrum (rough, for reading the pattern only); strong = 12 % of the loud hits."""
    if not hits:
        return
    ref = np.percentile([h['peak'] for h in hits], 95)
    for h in hits:
        h['strong'] = bool(h['peak'] > 0.12 * ref)
        if h['lo'] > 0.3:
            h['cls'] = 'kick'
        elif h['hi'] > 0.5:
            h['cls'] = 'hat'
        else:
            h['cls'] = 'snare'
