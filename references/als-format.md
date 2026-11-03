# Warp, loop and tempo in a Live 12 `.als`

Confirmed on Live 12.4.16 (Nov 2026), set schema `MinorVersion 12.0_12402`.
An `.als` is gzip-compressed XML. The patcher rewrites only the fields below,
as text, so everything else in the file stays byte-identical.

## Where clips live
- Arrangement audio clips: `…/AudioTrack/DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip`.
- Session clips: `…/MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/AudioClip`.
- Track name: `AudioTrack/Name/EffectiveName`. Sample: `AudioClip/SampleRef/FileRef/Path`
  (absolute) and `RelativePath`; `SampleRef/DefaultDuration` = length in samples,
  `DefaultSampleRate`. Stems of one take share `DefaultDuration` exactly.
- Main track (tempo, time signature): `MainTrack` (`MasterTrack` in older sets).

## Warp map
```xml
<WarpMarkers>
    <WarpMarker Id="0" SecTime="0" BeatTime="0.42" />
    <WarpMarker Id="1" SecTime="18.2948" BeatTime="28" />
    …
</WarpMarkers>
```
`SecTime` is seconds in the file, `BeatTime` is clip beats. Live plays linearly
between markers and extends the first and last segment. Live accepted 117
markers and a first marker at a non-zero beat; it saves them back unchanged.
`IsWarped` must be `true`.

## Clip region (`<Loop>` and friends), loop switched off
| field | meaning |
|---|---|
| `AudioClip Time=`, `CurrentStart` | arrangement beat where the clip starts |
| `CurrentEnd` | arrangement beat where it ends (`CurrentStart + LoopEnd − LoopStart`) |
| `LoopStart` | clip beat of the start marker |
| `LoopEnd` | clip beat of the end marker |
| `StartRelative` | 0 when the loop is off |
| `OutMarker`, `HiddenLoopEnd` | clip beat of the end of the whole file |
| `HiddenLoopStart` | clip beat of the start of the file |
| `ScrollerTimePreserver/LeftTime, RightTime` | the clip view's zoom; set to the region |

Arrangement beat of any clip beat = `CurrentStart + (clipBeat − LoopStart)`.
So `CurrentStart − LoopStart` is where clip beat 0 sits in the arrangement.
**Splitting a clip in Live keeps that value identical in every piece** (each
piece just gets its own `LoopStart`/`LoopEnd`, `Hidden*`/`OutMarker` keep the
whole file). The patcher uses it to tell pieces still in place (same value as
the take) from pieces the user moved (different value).

Live sizes a dropped file at the set tempo of that moment: a 85.8 s file
dropped at 120 BPM got `LoopEnd` 171.6 beats; auto-warp to 172 BPM then made
that 59.9 s of audio. Compare the played seconds with the file length.

## Warp modes (`WarpMode Value`)
0 Beats, 1 Tones, 2 Texture, 3 Re-Pitch, 4 Complex, 5 REX, 6 Complex Pro.

## Leader / Follow
`IsSongTempoLeader` `true` = the clip's Tempo Leader switch. With a Leader
clip playing, the set tempo follows the clip's segment tempos.

## Set tempo
```xml
<Tempo>
    <LomId Value="0" />
    <Manual Value="120" />
    …
    <AutomationTarget Id="8">
```
plus, somewhere under the main track's automation envelopes, an envelope whose
`EnvelopeTarget/PointeeId` is that `AutomationTarget Id`, holding a single
default `<FloatEvent Id="…" Time="-63072000" Value="120" />`. Change both.
More than one event means the user automated the tempo: leave it alone.

## Time signature
`MainTrack/…/Mixer/TimeSignature/Manual`: value = (numerator − 1) + 99 × log2(denominator).
201 = 4/4.

## Live round trip
Write the patched file over the saved set (Live had it open, unchanged since
the save), `open -a` it, wait for `End ExchangeDocument` in
`~/Library/Preferences/Ableton/Live <version>/Log.txt`, save through the menu,
then read the markers back (`inspect_clips.py --map`). Live keeps the patched
markers exactly; it writes numbers in its own format (e.g. `60` for `60.0`).
