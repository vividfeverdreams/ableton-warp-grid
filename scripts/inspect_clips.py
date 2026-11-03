#!/usr/bin/env python3
"""List a Live set's audio clips and their warp state (read-only).

Shows the set tempo (and whether it is automated) and time signature, then
every audio clip: track, arrangement/session, file (and whether it exists),
warp mode, marker count, Leader/Follow, where it sits in bars, and how much
of the file it actually plays. Clips Live sized at the tempo it was dropped
at are flagged "cut short". Files with identical length are grouped as one
take (stems split from one recording always match to the sample).

With --map it checks the matched clips carry exactly that warp map: run it
on the set after Live has saved it to prove Live loaded the map unchanged.

usage: inspect_clips.py <set.als> [--match TEXT ...] [--map map.json]
"""
import argparse, collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from als_tools import read_als, clips, val, time_signature, beat_to_sec, bar_beat, match, WARP_MODES


def tempo_info(root):
    for tag in ('MainTrack', 'MasterTrack'):
        t = root.find(f'.//{tag}/DeviceChain/Mixer/Tempo')
        if t is not None:
            tid = t.find('AutomationTarget').get('Id')
            n = 0
            for env in root.iter('AutomationEnvelope'):
                if val(env, 'EnvelopeTarget/PointeeId') == tid:
                    n = len(env.find('Automation/Events'))
            return float(val(t, 'Manual')), n
    return None, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('als')
    ap.add_argument('--match', action='append', default=[])
    ap.add_argument('--map')
    a = ap.parse_args()
    root, cs = clips(read_als(a.als))
    num, den = time_signature(root)
    tempo, events = tempo_info(root)
    print(f"{os.path.basename(a.als)}: {root.get('Creator')}, tempo {tempo:g} BPM"
          + (f' (AUTOMATED: {events} tempo events)' if events > 1 else '') + f', {num}/{den}')
    if a.match:
        cs = [c for c in cs if match(c, a.match)]
    takes = collections.defaultdict(set)
    per_track = collections.Counter((c['track'], c['path']) for c in cs if c['view'] == 'arrangement')
    for c in cs:
        info = ''
        if c['path'] and os.path.exists(c['path']):
            try:
                import soundfile as sf
                fi = sf.info(c['path'])
                c['frames'], c['sr'] = fi.frames, fi.samplerate
            except Exception:
                pass
        elif c['path']:
            info = ' FILE MISSING'
        dur = c['frames'] / c['sr'] if c['sr'] else 0
        s0 = beat_to_sec(c['markers'], c['loop_start'] + c['start_rel'])
        s1 = beat_to_sec(c['markers'], c['loop_end'])
        z = lambda x: 0.0 if abs(x) < 0.005 else x
        plays = f'plays {z(s0):.2f}-{z(s1):.2f} s of {dur:.2f} s' if s0 is not None else 'one warp marker'
        if (s1 is not None and dur and not c['loop_on'] and s1 < dur - 0.5 and c['view'] == 'arrangement'
                and per_track[(c['track'], c['path'])] == 1):
            plays += ' (CUT SHORT? Live sizes dropped files at the tempo of the moment)'
        where = (f"bars {bar_beat(c['start'], num)}-{bar_beat(c['end'], num)}" if c['view'] == 'arrangement'
                 else 'session slot')
        print(f"[{c['index']}] {c['track']!r} {c['view']}: {os.path.basename(c['path'])}{info}\n"
              f"     {where}, {'warped' if c['warped'] else 'NOT warped'} {WARP_MODES.get(c['mode'], c['mode'])}, "
              f"{len(c['markers'])} markers, {'LEADER' if c['leader'] else 'Follow'}"
              f"{', loop on' if c['loop_on'] else ''}{', DISABLED' if c['disabled'] else ''}; {plays}")
        if c['path']:
            takes[(c['frames'], c['sr'])].add(c['path'])
    for (fr, sr), paths in takes.items():
        if len(paths) > 1 and fr:
            print(f'same take? {len(paths)} files of exactly {fr} samples at {sr} Hz: '
                  + ', '.join(os.path.basename(p) for p in sorted(paths)))
    if a.map:
        W = json.load(open(a.map))
        M = W['markers']
        print(f"map check ({len(M)} markers, set tempo should be {W['bpm']:g}: it is {tempo:g})")
        for c in cs:
            mk = c['markers']
            if len(mk) != len(M):
                print(f"  [{c['index']}] {c['track']}: {len(mk)} markers, map has {len(M)}: NOT the map")
                continue
            d = max(max(abs(x[0] - y[0]), abs(x[1] - y[1])) for x, y in zip(mk, M))
            print(f"  [{c['index']}] {c['track']}: markers match, max difference {d:.2e}")


if __name__ == '__main__':
    main()
