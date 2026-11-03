---
name: ableton-warp-grid
description: Grid up audio in Ableton Live 12 on macOS. Finds the real BPM, downbeat and how to count a played or sampled recording (91 vs 182, 140 vs 70), pins every hit to the grid with warp markers so live tempo drift ("flux") is warped out, and puts the same warp map on every stem of the take so they stay locked together. Use this skill whenever the user wants audio "on beat", "on the grid", "gridded up", "warped", "quantized", "in time" or "locked" in Live; says a sample's or stem's BPM is wrong or unknown, or Live's auto-warp guessed double or half time; has stems (Kits.ai, Demucs, LALAL, RX, Moises) of a live performance, jam, rip, freestyle, beatbox or vocal that drift; mentions warp markers, Tempo Leader/Follower, "make it the leader", Complex Pro or Beats mode; or wants to flip, remix or sample a recording that wasn't made to a click. Also use it just to find the tempo and downbeat of an audio file, even if they don't say "warp".
---

# Ableton warp grid

Takes a recording that was played, not programmed, and makes it sit on Live's
grid: finds the true tempo, the downbeat and how the groove counts, then
writes a warp map with a marker on every hit, each moved to its exact grid
spot. With stems, the map comes from the drums (the "leader") and goes on
every stem of the take, so they stay sample-locked to each other.

The idea in one line: **analyse the drums offline, build one warp map, write
it into the set's `.als` as text, let Live reload it, prove Live kept it.**
Live's own auto-warp is not used: it guesses one tempo, often double or half,
and doesn't follow a player's drift.

## Requirements

- macOS, Ableton Live 12, Accessibility permission for the app running `osascript`.
- Python 3 with `numpy`, `scipy`, `soundfile` (and `matplotlib` for `--plot`).
  macOS's own `python3` usually lacks them, so the skill uses a venv, made once:
  ```bash
  PY=~/.venvs/warp/bin/python
  [ -x "$PY" ] || { python3 -m venv ~/.venvs/warp && "$PY" -m pip install -q -r <this skill>/requirements.txt; }
  ```
  Any other Python that imports `numpy, scipy, soundfile` works too.
- Scripts are in `scripts/` next to this file; `$S` below means that folder and
  `$PY` that Python. Run them with `-I` (they find their own helpers).
- Work in a scratch folder (`$W`): analysis, map and QC files go there, never
  into the user's project.

## Ground rules

- **Never lose the user's work.** Save the open set through Live's menu before
  touching its file, keep a copy of that saved `.als` as the backup, and never
  click "Don't Save" on the user's set. If Live asks to save when you reopen,
  the set changed after your save: Cancel, and ask the user.
- **Only the take's clips and the tempo change.** The patcher edits the `.als` as
  text, so every other byte stays the same; it refuses sets with real tempo
  automation, never touches session clips, consolidated copies or looped clips.
- **Don't trust the file's name or Live's BPM.** Kits/Demucs names carry no tempo,
  and Live's guess is often double or half. Measure.
- **You can't listen.** Say so, give the numbers, and ask the user to check by ear
  with the metronome on.

## Workflow

### 1. Find the set and save it
```bash
osascript -e 'tell application "System Events" to tell process "Live" to get value of attribute "AXDocument" of window 1'
osascript -e 'tell application "System Events" to tell process "Live" to click menu item "Save Live Set" of menu "File" of menu bar 1'
```
No `AXDocument` (or a window titled "Untitled") means the set was never saved.
Save it with the user's naming habits (for a flip: their flips folder) via
**Save Live Set As...**: it opens a native panel, window `"Save"`. Press
Cmd+Shift+G, type the folder, Return; set `value of text field 2 of splitter
group 1 of window "Save"` to the name; click `button "Save" of splitter group 1
of window "Save"`. Live creates `<name> Project/<name>.als`. Tell the user.

Then copy the saved `.als` to `$W/before-warp.als`. It is the backup and the
patcher's input.

### 2. Find the take's clips
```bash
$PY -I $S/inspect_clips.py "$W/before-warp.als"
```
It lists every audio clip: track, file, warp mode, markers, Leader/Follow,
bars, and how much of the file it plays. Look for:
- **The take:** files with *exactly* the same length in samples are stems of one
  recording ("same take?" line). Pick a `--match` text common to them (a shared
  part of the file name).
- **The reference:** the drum stem. With no drum stem, the most percussive part
  (run step 3 on candidates; the one with the clearest backbeat and most anchor
  hits wins). One sample on its own is its own reference.
- **"CUT SHORT?"**: Live sizes a dropped file at the tempo of that moment, so a
  file dropped at 120 and auto-warped to 172 silently loses its end. The patcher
  opens such clips back up to the whole file.
- **Split or moved pieces:** the patcher keeps exactly the audio each piece plays;
  pieces still in place move with the take, moved pieces keep their start.
- Files in `~/Downloads`: offer File → Collect All and Save afterwards, so
  cleaning Downloads can't break the set.

### 3. Analyse the reference
```bash
$PY -I $S/analyze_take.py "<drums.wav>" --out $W/analysis.json --plot $W/analysis.png
```
Read the printout (and look at the plot):
- **ways to count it**: the candidates with backbeat / half-time evidence. The
  default picks snare-on-2-and-4 inside 80-180 BPM, else snare-on-3 (half-time).
  That handles boom-bap 91 (not 182), DnB 174 (not 87), dubstep/trap 140 (not
  70), house 126. Overrule it with `--bpm-near` when the genre says otherwise
  (a 75 BPM boom-bap comes out 150 half-time by default; `--bpm-near 75`).
  When the choice is genuinely open, tell the user both and which you used.
- **first downbeat**: should be beat 1 of the first full bar of the beat, usually a kick.
- **beat tracked from/to**: the stretch that sits on a grid. Talking and
  free-time intros and outros are left out on purpose (they get stretched as
  little as possible). If it misses part of the beat, or a WARNING says another
  rhythmic part is outside, rerun with `--start/--end` around it.
- **local tempo / off one steady tempo**: how much flux there is.
- **feel**: average push/pull per position (e.g. `4& +35` = the "and" of 4 is
  played 35 ms late every bar). Worth telling the user: gridding removes it.
- **dropouts** and **WARNING: a beat may have slipped**: check the plot there and
  the kick pattern either side; fix with `--start/--end` per section if needed.

### 4. Build the warp map
```bash
$PY -I $S/build_warp_map.py $W/analysis.json --out $W/map.json [--snap 8th] [--first-bar N] [--bpm 91]
```
`--snap` is the musical decision:
- `8th` (default) or `16th`: a marker on every hit on that grid. Removes the flux
  *and* the player's push/pull. Tightest, stacks with programmed drums. This is
  what "get it on the grid" / "warp out the flux" means.
- `beat`: markers on the beats only. Drift goes, the swing inside each beat stays.
- `bar`: downbeats only. Only the drift goes, the whole groove stays.

It prints the set tempo (the average, rounded), the clip bar of the first
downbeat, and how much the intro and outro get sped up or slowed (normally
under 1 %, so talking sounds natural).

### 5. Check the map
```bash
$PY -I $S/qc_grid.py "<drums.wav>" $W/map.json $W/analysis.json --out $W/qc.json
```
Grid notes before (one steady tempo, and the set tempo from beat 1) vs after;
in-between notes (16ths, ghosts: they move with the grid, not pinned);
secondary transients (buzz inside a beatboxed kick, flams) that are ignored;
and where markers sit on the attack (a few ms either way is right; tens of ms
means they're on the wrong part of the transient).

### 6. Write it into the set
```bash
$PY -I $S/patch_warp.py "$W/before-warp.als" $W/map.json "<set>.als" \
    --match "<take id>" --reference drums [--others-mode complex-pro] [--tempo 91] [--arr-bar N] [--leader]
```
Defaults: reference clip in **Beats** (transients stay crisp), the other stems in
**Complex Pro** (voice and bass don't chop), every clip **Follow**, set tempo =
the map's. The take keeps its place: clip bar 1 goes on the bar nearest to where
it sat (`--arr-bar` to move it). It prints every clip's new bars and what it
skipped and why.

**"Make it the leader"** usually means "make the drums the reference everything
follows", which is what the shared map does. Live's Tempo **Leader** switch is
something else: the set tempo then follows the clip's own segment tempos, which
with a tight map swing ±15 % around every late or early note. Use `--leader`
only when the user wants the set to breathe with the performance instead of
gridding it, and explain the difference either way.

### 7. Reload in Live and prove it took
```bash
LOG=~/Library/Preferences/Ableton/"Live <version>"/Log.txt   # ls ~/Library/Preferences/Ableton
L0=$(wc -l < "$LOG" | tr -d ' ')
open -a "Ableton Live 12 Suite" "<set>.als"                  # the edition the user has: ls /Applications | grep -i ableton
# wait until the log after line L0 shows "End ExchangeDocument"
```
Live had this set open and saved, so it reloads the patched file. If a save
dialog appears instead, read it (`osascript -e 'tell application "System Events"
to tell process "Live" to get name of every window'`) and follow the ground
rules. Then save through the menu (step 1) and check Live kept the map:
```bash
$PY -I $S/inspect_clips.py "<set>.als" --match "<take id>" --map $W/map.json
```
Every clip of the take must say "markers match" (differences ~1e-15) and the
tempo must be the map's.

### 8. Tell the user and clean up
Say it plainly:
- the BPM (the average and what the set is at), how it counts and why
  ("snare on 2 and 4 at 91"), and the alternative (182 double-time) if it's open;
- what moved: N markers on every 8th-note hit, same map on all stems, warp modes;
- where the beat sits (drums enter on bar X) and anything reopened (clips that
  were cut short);
- the flux before and after (median/max ms off the grid) and the feel you
  gridded out;
- Follow vs Leader in one line;
- that you haven't heard it.

Keep `before-warp.als` and `map.json` where the user can get them back (their
usual backup or archive folder; ask if you don't know it), check the copies,
and delete the rest of `$W`. Offer Collect All and Save if the files live in Downloads.

## Gotchas
- Stems from one take always match to the sample; a consolidated or bounced
  copy doesn't, so the patcher leaves it alone (it lists it).
- Beatboxed kicks and live drums make several transients per hit (buzz, flam,
  room). The analyzer treats hits within ~100 ms as one note and marks the first.
- With the drums only in part of the file, the intro and outro are extended at
  the edge tempo; the markers at file start and end exist for that.
- Triplet or shuffle grooves: the 8th grid would straighten the swing; use `--snap beat`.
- More than one tempo section (a breakdown at a new tempo, a second beat after
  a long talk): analyse each with `--start/--end`; the patcher writes one map,
  so the sections need to be one map (build them separately and join the marker
  lists, keeping beats increasing) or separate clips.
- Live's display: a split piece may start at e.g. bar 31.4.99 after re-warping;
  that is the same audio moment under the new map, not a mistake.

## Reference
- `references/als-format.md`: the warp, loop and tempo XML, warp mode numbers,
  how split clips are stored, and why the patcher sets each field.
- `tests/run_tests.py`: synthetic takes with known answers (boom-bap with talking,
  DnB, half-time 140, house, a rushing drummer); `--seeds 8` for 40 variations.
  Run it after changing any script.
