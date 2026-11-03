#!/usr/bin/env python3
"""Write one warp map onto every arrangement clip of a take, and set the tempo.

The .als is edited as text: only the matched clips' warp markers, start/end,
warp mode, Leader switch and the set tempo change; every other byte stays.

Which clips: arrangement audio clips whose file path, track or clip name
contains a --match text AND whose file has exactly the map's length in samples
(stems of one take always do; a consolidated or bounced copy does not, so it
is left alone and listed). Session clips are listed, never changed.

Where they go (clip beat 0 = one arrangement position for the whole take):
  default   the bar nearest to where the take's clip beat 0 sits now
  --arr-bar N   clip bar 1 on arrangement bar N
A clip that still starts at the very start of its file, alone on its track,
is opened up to the whole file (Live sizes dropped samples at the tempo of
the moment, which silently cuts the end off). Split pieces keep exactly the
audio they play now: still in their original place they move with the take;
a piece that was moved somewhere else keeps its start and only gets the new
map. --extent keep treats every clip like a split piece.

Warp modes: the --reference clip (the drums) gets --reference-mode (Beats),
the others --others-mode (Complex Pro); "keep" leaves a mode alone. All clips
are set to Follow; --leader makes the reference clip Tempo Leader instead.
--tempo sets the set tempo (default: the map's); "keep" leaves it. A set with
real tempo automation is refused unless --tempo keep.

usage: patch_warp.py <in.als> <map.json> <out.als> --match TEXT [--match TEXT ...]
                     [--reference TEXT] [--reference-mode beats] [--others-mode complex-pro]
                     [--tempo BPM|keep] [--arr-bar N] [--extent auto|keep] [--leader]
"""
import argparse, collections, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from als_tools import (read_als, write_als, clips, match, time_signature, beat_to_sec, sec_to_beat,
                       bar_beat, MODE_IDS, WARP_MODES)

fmt = lambda v: repr(float(v))


def set_val(block, tag, value):
    block, n = re.subn(rf'(<{tag} Value=")[^"]*(" />)', lambda m: m.group(1) + value + m.group(2), block, count=1)
    assert n == 1, f'{tag} not found in clip'
    return block


def patch_tempo(text, bpm):
    """Set the Main track's tempo and its default automation event; refuse if the tempo is automated."""
    m = re.search(r'<(MainTrack|MasterTrack)[ >]', text)
    t = re.compile(r'<Tempo>\s*<LomId Value="\d+" />\s*<Manual Value="([^"]*)" />.*?<AutomationTarget Id="(\d+)">', re.S)
    tm = t.search(text, m.start())
    old, tid = tm.group(1), tm.group(2)
    text = text[:tm.start(1)] + fmt(bpm) + text[tm.end(1):]
    env = re.search(rf'<EnvelopeTarget>\s*<PointeeId Value="{tid}" />\s*</EnvelopeTarget>\s*<Automation>\s*<Events>(.*?)</Events>',
                    text, re.S)
    if env:
        events = re.findall(r'<FloatEvent [^>]*/>', env.group(1))
        if len(events) > 1:
            raise SystemExit(f'the set tempo is automated ({len(events)} events): not touching it. '
                             'Use --tempo keep, or ask the user to clear the tempo automation first.')
        if events:
            ev = re.compile(r'(<FloatEvent Id="\d+" Time="-63072000" Value=")[^"]*(" />)')
            seg = ev.sub(lambda x: x.group(1) + fmt(bpm) + x.group(2), env.group(1), count=1)
            text = text[:env.start(1)] + seg + text[env.end(1):]
    return text, float(old)


def warp_block(indent, M):
    lines = [f'{indent}<WarpMarkers>']
    lines += [f'{indent}\t<WarpMarker Id="{i}" SecTime="{fmt(s)}" BeatTime="{fmt(b)}" />' for i, (s, b) in enumerate(M)]
    return '\n'.join(lines + [f'{indent}</WarpMarkers>'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('map'); ap.add_argument('dst')
    ap.add_argument('--match', action='append', required=True)
    ap.add_argument('--reference', help='text that picks the clip the map came from (e.g. drums)')
    ap.add_argument('--reference-mode', default='beats')
    ap.add_argument('--others-mode', default='complex-pro')
    ap.add_argument('--tempo', default=None)
    ap.add_argument('--arr-bar', type=int)
    ap.add_argument('--extent', default='auto', choices=['auto', 'keep'])
    ap.add_argument('--leader', action='store_true')
    a = ap.parse_args()
    if os.path.abspath(a.src) == os.path.abspath(a.dst):
        raise SystemExit('write to a different file than the source (keep the source as the backup)')
    W = json.load(open(a.map))
    M = [tuple(m) for m in W['markers']]
    text = read_als(a.src)
    root, cs = clips(text)
    num, _ = time_signature(root)
    if num != W['beats_per_bar']:
        print(f"WARNING: the set is in {num}/x but the map counts {W['beats_per_bar']} beats per bar")

    picked, skipped = [], []
    for c in cs:
        if not match(c, a.match):
            continue
        if c['view'] != 'arrangement':
            skipped.append((c, 'session clip (not changed)')); continue
        if c['frames'] != W['frames'] or c['sr'] != W['sample_rate']:
            skipped.append((c, f"different file length ({c['frames']} samples, map {W['frames']})")); continue
        if c['loop_on'] or c['start_rel'] != 0:
            skipped.append((c, 'looped clip (warp it by hand)')); continue
        picked.append(c)
    if not picked:
        raise SystemExit('no arrangement clip matched with the map\'s file length')

    # the take's clip-beat-0 position now: the most common one among the clips
    anchor = lambda c: round(c['start'] - c['loop_start'], 3)
    take0 = collections.Counter(anchor(c) for c in picked).most_common(1)[0][0]
    arr0 = (a.arr_bar - 1) * num if a.arr_bar else num * round(take0 / num)
    per_track = collections.Counter((c['track'], c['path']) for c in picked)
    b_first, b_last = M[0][1], M[-1][1]

    out, pos, report = [], 0, []
    for c in picked:
        blk = text[c['span'][0]:c['span'][1]]
        unmoved = abs(anchor(c) - take0) < 1e-3
        s0 = beat_to_sec(c['markers'], c['loop_start'])
        s1 = beat_to_sec(c['markers'], c['loop_end'])
        whole = (a.extent == 'auto' and per_track[(c['track'], c['path'])] == 1 and unmoved
                 and s0 is not None and abs(s0) < 0.05)
        if whole or s0 is None:
            ls, le, how = b_first, b_last, 'whole file'
        else:
            ls, le = sec_to_beat(M, max(0.0, s0)), sec_to_beat(M, min(W['duration_s'], s1))
            how = f'keeps {max(0, s0):.2f}-{min(W["duration_s"], s1):.2f} s'
        start = arr0 + ls if unmoved else c['start']
        if not unmoved:
            how += ', moved piece: start kept'
        end = start + (le - ls)
        blk = re.sub(r'(<AudioClip Id="\d+" Time=")[^"]*', lambda x: x.group(1) + fmt(start), blk, count=1)
        for tag, v in (('CurrentStart', start), ('CurrentEnd', end), ('LoopStart', ls), ('LoopEnd', le),
                       ('OutMarker', b_last), ('HiddenLoopStart', b_first), ('HiddenLoopEnd', b_last),
                       ('LeftTime', ls), ('RightTime', le)):
            blk = set_val(blk, tag, fmt(v))
        blk, n = re.subn(r'(\t*)<WarpMarkers>.*?</WarpMarkers>', lambda x: warp_block(x.group(1), M), blk, count=1, flags=re.S)
        assert n == 1
        blk = set_val(blk, 'IsWarped', 'true')
        is_ref = bool(a.reference) and match(c, [a.reference])
        mode = a.reference_mode if is_ref else a.others_mode
        if mode != 'keep':
            blk = set_val(blk, 'WarpMode', str(MODE_IDS[mode.lower()]))
        blk = set_val(blk, 'IsSongTempoLeader', 'true' if (a.leader and is_ref) else 'false')
        out += [text[pos:c['span'][0]], blk]; pos = c['span'][1]
        report.append((c, start, end, how, is_ref))
    text2 = ''.join(out) + text[pos:]

    bpm = W['bpm'] if a.tempo is None else (None if a.tempo == 'keep' else float(a.tempo))
    old_bpm = None
    if bpm is not None:
        text2, old_bpm = patch_tempo(text2, bpm)

    _, check = clips(text2)                            # the result must parse and carry the map
    for c, *_ in report:
        nc = check[c['index']]
        assert nc['markers'] == [(float(fmt(s)), float(fmt(b))) for s, b in M], c['track']
    write_als(a.dst, text2)

    if old_bpm is not None:
        print(f'tempo {old_bpm:g} -> {bpm:g} BPM')
    print(f"take's clip bar 1 on arrangement bar {arr0 // num + 1}; first downbeat of the beat on bar "
          f"{bar_beat(arr0 + W['first_downbeat_clip_beat'], num)}")
    for c, s, e, how, ref in report:
        nc = check[c['index']]
        print(f"  [{c['index']}] {c['track']}: bars {bar_beat(s, num)}-{bar_beat(e, num)}, "
              f"{WARP_MODES[nc['mode']]}{', LEADER' if nc['leader'] else ''}{' (reference)' if ref else ''}; {how}")
    for c, why in skipped:
        print(f"  skipped [{c['index']}] {c['track']}: {os.path.basename(c['path'])}: {why}")
    print('wrote', a.dst)


if __name__ == '__main__':
    main()
