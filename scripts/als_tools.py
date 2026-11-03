"""Read a Live set's audio clips, keeping the raw text so patches leave every other byte alone."""
import bisect, gzip, re
import xml.etree.ElementTree as ET

WARP_MODES = {0: 'Beats', 1: 'Tones', 2: 'Texture', 3: 'Re-Pitch', 4: 'Complex', 5: 'REX', 6: 'Complex Pro'}
MODE_IDS = {'beats': 0, 'tones': 1, 'texture': 2, 're-pitch': 3, 'repitch': 3, 'complex': 4, 'complex-pro': 6, 'complexpro': 6}
CLIP_RE = re.compile(r'(\t*)<AudioClip Id="\d+" Time="[^"]*">.*?</AudioClip>', re.S)


def read_als(path):
    with open(path, 'rb') as f:
        raw = f.read()
    return (gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw).decode('utf-8')


def write_als(path, text):
    with gzip.open(path, 'wb') as f:
        f.write(text.encode('utf-8'))


def val(el, path, d=None):
    x = el.find(path)
    return x.get('Value') if x is not None else d


def time_signature(root):
    """(numerator, denominator) of the set: Live stores (num - 1) + 99 * log2(den)."""
    for tag in ('MainTrack', 'MasterTrack'):
        t = root.find(f'.//{tag}/DeviceChain/Mixer/TimeSignature/Manual')
        if t is not None:
            v = int(float(t.get('Value')))
            return v % 99 + 1, 2 ** (v // 99)
    return 4, 4


def clips(text):
    """Every audio clip in document order: dict with the text span, view, track and warp state."""
    root = ET.fromstring(text)
    parent = {c: p for p in root.iter() for c in p}
    spans = list(CLIP_RE.finditer(text))
    els = list(root.iter('AudioClip'))
    assert len(spans) == len(els), (len(spans), len(els))
    out = []
    for i, (m, c) in enumerate(zip(spans, els)):
        e, view, track = c, 'session', '?'
        while e in parent:
            e = parent[e]
            if e.tag == 'ArrangerAutomation':
                view = 'arrangement'
            if e.tag in ('AudioTrack', 'ReturnTrack', 'MainTrack', 'MasterTrack'):
                track = val(e, 'Name/EffectiveName', '?')
                break
        out.append(dict(
            index=i, span=(m.start(), m.end()), indent=m.group(1), view=view, track=track,
            path=val(c, 'SampleRef/FileRef/Path', '') or '', name=val(c, 'Name', ''),
            frames=int(float(val(c, 'SampleRef/DefaultDuration', '0'))),
            sr=int(float(val(c, 'SampleRef/DefaultSampleRate', '0'))),
            time=float(c.get('Time')), start=float(val(c, 'CurrentStart')), end=float(val(c, 'CurrentEnd')),
            loop_start=float(val(c, 'Loop/LoopStart')), loop_end=float(val(c, 'Loop/LoopEnd')),
            loop_on=val(c, 'Loop/LoopOn') == 'true', start_rel=float(val(c, 'Loop/StartRelative', '0')),
            warped=val(c, 'IsWarped') == 'true', mode=int(val(c, 'WarpMode', '0')),
            leader=val(c, 'IsSongTempoLeader') == 'true', disabled=val(c, 'Disabled') == 'true',
            markers=[(float(w.get('SecTime')), float(w.get('BeatTime'))) for w in c.find('WarpMarkers')]))
    return root, out


def _interp(xs, ys, x):
    """Piecewise linear through the points with the end segments extended, as Live plays a warp map."""
    if len(xs) < 2:
        return None
    i = min(max(bisect.bisect_right(xs, x) - 1, 0), len(xs) - 2)
    return ys[i] + (x - xs[i]) * (ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i])


def beat_to_sec(markers, b):
    return _interp([m[1] for m in markers], [m[0] for m in markers], b)


def sec_to_beat(markers, s):
    return _interp([m[0] for m in markers], [m[1] for m in markers], s)


def bar_beat(b, num=4):
    """Arrangement beat -> 'bar.beat' as Live shows it (1-based)."""
    bar, beat = divmod(round(b, 2), num)
    return f'{int(bar) + 1}.{beat + 1:.2f}'.rstrip('0').rstrip('.')


def match(c, needles):
    hay = (c['path'] + '\n' + c['track'] + '\n' + c['name']).lower()
    return any(n.lower() in hay for n in needles)
