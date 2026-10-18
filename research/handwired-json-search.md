# Borrowing handwired's JSON-search interaction, not its pipeline

*Read against `github.com/chenglou/handwired` at commit `257856e3b221140acb4cc4460626b850c0bd4ff1`
(main, 2026-10-17), directory `src/json-search`: `demo.ts` (856 lines), `demo-map.ts` (157),
`demo-text.ts` (200), `demo-worker.ts` (137), `plain-search.ts` (110), `table-loop.ts` (92),
`README.md` (249), plus `assets/json-search.html`, `assets/json-search.css`, `assets/tool.css`,
`assets/site.css`, `highlighter/kinds.ts`, `tests/json-search/demo.test.ts` (428) and `LICENSE`.
Live page: <https://chenglou.me/handwired/json-search/>. Every line number below is that commit.
Answers ticket 04 of `.scratch/handwired-demo/`; feeds tickets 06 (a keystroke), 02 (a cell) and 14
(the build).*

## Verdict in three lines

1. **Borrowable, and small**: `demo-map.ts` (157 lines of pure geometry and picking, no DOM) plus the
   ~60 lines of event handling and frame logic in `demo.ts:559-586, 796-824`, and the sentence builder
   `resultLine` in `demo-text.ts:149-163`. That is the whole presentation; nothing GPU-specific is in it.
2. **Licence**: MIT, `Copyright (c) 2026 Cheng Lou`. Copying *code* obliges us to ship that notice plus
   the permission text; borrowing only the *presentation* is not a licence question, but this repo's
   own ruling (`docs/spec/pdf-workflow-demo.md:216-218`) and its research-note convention already
   require a written pointer. Do both.
3. **The pipeline is not borrowable here**: their whole premise is 100,000 uniform 512-code-point
   documents and 410 MB of GPU buffers to beat 13 ms. Our corpus is 590 Chunks and one FTS5 `bm25`
   query over it is 0.11 ms in process. Their CPU path alone (0.14 ms per document, `README.md:74`)
   would be ~83 ms for our corpus — ~750× our measured query, spent to replace a keystroke with a
   worker round trip.

---

## 1. The grid interaction contract

The contract is five pure functions and one frame. `demo-map.ts` holds the arithmetic with no DOM
(`demo-map.ts:1-6`); `demo.ts` holds the state that the arithmetic reads.

### 1.1 One cell per item, and the gap belongs to the cell

- The layout is *computed to fit*: `mapLayout(widthCss, scale, documentCount)` (`demo-map.ts:30-42`)
  walks pitch down from `12 CSS px × scale` to `scale + 1`, picks `cell = pitch − gapOfPitch(pitch)`
  with `gapOfPitch = max(1, round(pitch/5))` (`demo-map.ts:24-26`), and stops at the first pitch whose
  height fits `tallestCss = 420` CSS px (`demo-map.ts:24, 38-39`).
- Pinned results, asserted in `tests/json-search/demo.test.ts:65-78`: for 100,000 items in 630 CSS px,
  `pitch/cell` is `[2,1]` at scale 1, `[3,2]` at scale 2, `[4,3]` at scale 3; 65,000 items get
  `[4,3]`; 20,000 in 324 CSS px get `[7,6]`; 500 items get the largest, `[24,19]` — i.e. **a denser
  corpus makes the cell smaller, not the map taller**, up to a hard ceiling.
- Canvas sizes are in device pixels (`scale = max(1, min(4, round(devicePixelRatio)))`,
  `demo.ts:426`) so cell edges land on whole device pixels; CSS px exist only at the boundary.
- **`cellAt` answers for the cell *and the gap to its right and below it*** (`demo-map.ts:44-53`), so a
  pointer in the gap is never in a dead zone, and `cellAt` returns −1 only outside the map, before the
  first cell, or past the last item in the last row (`demo.test.ts:52-63`).
- `cellCenterCss` is the only other geometry a page needs, for the ring (`demo-map.ts:56-61`).
- The CSS height is *derived from the same function*, and a test pins the two together
  (`demo.test.ts:80-90`, `json-search.css:146-158`): the first paint already has the map's room, before
  JS runs.

### 1.2 The pointer picks the nearest **matching** cell, else the cell under it

`pickedDocument` (`demo-map.ts:90-99`) is the whole rule, in three lines of logic:

```
under = cellAt(layout, x, y)
if (under >= searched) return under            // search hasn't reached it yet
nearest = nearestMatch(...)                    // nearest item with a match, among the first `searched`
return nearest >= 0 ? nearest : under          // no match anywhere → the cell under the pointer
```

- `nearestMatch` (`demo-map.ts:67-88`) is a single linear pass over the counts — `for index < searched`,
  skipping zeros, comparing squared distance in *cell units* (`x = (xCss*scale − cell/2)/pitch`,
  `demo-map.ts:70-71`). Ties keep the **first** cell (`<`, `demo-map.ts:77`; asserted
  `demo.test.ts:106-107`). Comment at `demo-map.ts:65` measures one call at 0.1 ms for 100,000 items;
  for hundreds it is free, so it can run per pointer event with no spatial index.
- The `under >= searched` clause matters only while a progressive search is filling in: without it the
  nearest match would jump to the search front, far from the pointer, and change every frame
  (`demo-map.ts:90-93`). Our grid's matcher is synchronous, so this clause is dead code for us — keep
  the function shape, drop the parameter.
- The pointer is stored as *client* coordinates and converted with the map's rect at frame time
  (`demo.ts:559-562`), so a scroll between events does not skew the pick (`demo.ts:825-828`).

### 1.3 A click keeps a cell, and a second click on it lets it go

`demo.ts:563-574`:

- A click toggles: `st.kept = st.kept === clicked ? null : clicked` (`demo.ts:572`).
- **A mouse click picks by the pointer's own position, not the click's.** The comment at
  `demo.ts:566-570` is the reason and it is worth copying verbatim: `click` reports whole CSS px while
  `pointermove` reports fractions, and adjacent cells are 1.5 CSS px apart, so a click's coordinate
  could keep the *neighbour* of the document the panel shows. Use `hovered` when a pointer exists; use
  the click's coordinates only when there is none (a finger, `demo.ts:571`).
- Hover outranks keep for *showing*, keep outranks everything for *marking*: the target is
  `hovered ?? kept ?? firstMatch ?? shown.index ?? 0` (`demo.ts:586`), while the ring is drawn heavier
  when `shown.index === kept` (`demo.ts:683`, `json-search.css:178` — 2 px border instead of 1 px). So
  you can read other cells while the kept one stays visibly kept.
- A kept cell that the *current* search found nothing in is released (`demo.ts:581-582`) — otherwise a
  keep would survive into a filter that no longer contains it and point at nothing.

### 1.4 Enter / Shift+Enter step through matches

- One signed accumulator: `st.events.steps += event.shiftKey ? -1 : 1` (`demo.ts:798`), applied in the
  frame, one hop per press, in press order (`demo.ts:575-580`): `nextMatch(counts, searched, kept ??
  shown?.index ?? 0, step)`.
- `nextMatch` (`demo-map.ts:101-110`) walks `distance = 1..searched` with a modulo that is correct for
  negatives (`((from + step*distance) % searched + searched) % searched`), so it **wraps at both ends**,
  "as a browser's find bar does", and skips `from` itself — `from` is the answer only when it is the
  single match (`demo-map.ts:102-103, 104`; cases pinned in `demo.test.ts:140-152`).
- No match anywhere → `-1`, the keep is unchanged (`demo.ts:578`). Stepping sets `kept`, so stepping
  and clicking share one state.

### 1.5 Which focused elements must not swallow those keys

The listener is on `window`, not on the field (`demo.ts:796`), because a click on the map or a toggle
takes focus out of the field (`demo.ts:793-795`). The **only** exclusion is:

```ts
if (event.key !== 'Enter' || (event.target instanceof Element && event.target.closest('a, button') !== null)) return
```

`demo.ts:797` — inside a link or a button, Enter *is* the click, so the page must not also step. Needed
because the page's Try list is a list of `<button>`s (`json-search.html:52-55`).

What must therefore *not* capture Enter in our page: any `<button>`, `<a>`, or anything that re-emits
Enter as a click. The document panel is a `<pre tabindex="0">` (`json-search.html:92`) and is
deliberately *not* excluded — it takes focus for text selection/scrolling and still lets Enter step.
Do not add `role="button"`, `keydown` handlers that `preventDefault()`, or a `<form>` around the field
(there is no form in their HTML, which is why Enter in the field never submits — `json-search.html:60-73`).

### 1.6 Touch

- `pointermove` is ignored outright for touch: `if (event.pointerType === 'touch') return`
  (`demo.ts:811-816`). A finger has no hover, so nothing is shown by moving.
- The tap then picks **by its own coordinates**, because `st.pointer` is null (`demo.ts:571`), and the
  same toggle-to-release rule applies.
- `.map { touch-action: manipulation }` (`json-search.css:150-155`) removes the double-tap-zoom delay
  so a tap reads immediately; `cursor: crosshair` is the mouse affordance.
- The instruction text swaps on coarse pointers via pure CSS — `.with-mouse` / `.with-touch`
  (`json-search.css:24-29`, `json-search.html:96-99`) — no JS, so it is right at first paint.
- Focus is not stolen on a phone: the page only autofocuses the field when `(pointer: fine)`
  (`demo.ts:838-842`), and only moves focus after a Try click on such a device, because on a phone the
  keyboard would open *over* the map (`demo.ts:428-430, 711-714`).

### 1.7 Revealing the picked cell — the one thing handwired does **not** have

This is the honest gap, and it matters for our page. **In handwired no cell is ever off-screen**: the
layout is chosen so that all N cells fit a fixed 420 CSS px (`demo-map.ts:21-24, 30-42`), the canvas is
exactly that size, and the map is not a scroll container (`json-search.css:150-158`; the height is set
from the layout, `demo.ts:651-653`). So there is no scroll-into-view code to borrow, and picking never
needs one.

What "revealing" means there is two other things:

- **A ring on the cell**, an `<i>` positioned by `transform: translate(center.x, center.y)` with
  `left/top: −7px` and a 14×14 circle (`demo.ts:675-684`, `json-search.css:165-178`), `pointer-events:
  none`. It is moved, never re-created, so it does not flash. `display: none` before the first document
  (`json-search.html:84`).
- **The panel scrolls to the match, not the map to the cell**: after the document's runs are written,
  `documentText.scrollTop = firstHit === null ? 0 : max(0, firstHit.offsetTop − 72)` (`demo.ts:707-710`)
  — "so that the match stands three lines under the text's upper edge". The read happens after every
  DOM write in the frame, so layout is done once.

**For our page the question mostly dissolves.** Run the same walk (`demo-map.ts:30-42`) on our two
candidate grids at handwired's 630-CSS-px map width: 313 items give pitch 24 / cell 19 device px, 52
columns, 7 rows, **81.5 CSS px tall**; 590 items give 12 rows, **141.5 CSS px**; 590 items on a 324-CSS-px
phone at scale 3 give 22 rows, **261.7 CSS px**. All three sit well under the 420 CSS px ceiling and all
three keep the *largest* cell, so **no cell is off-screen for either candidate grid at any plausible
width** — and the reveal collapses into the same two things handwired does: the ring on the cell, and
`scrollTop` inside the shown item. Only a corpus an order of magnitude larger, or a deliberate zoom,
reintroduces scroll-into-view; then it is ours to write, not theirs to copy
(`ring.scrollIntoView({block: 'nearest', inline: 'nearest'})` after the ring is positioned, on keep and
step only — never on hover, which would make the page jump under the pointer).

### 1.8 Frame discipline (cheap to copy, easy to get wrong)

- Listeners and worker messages **only record and schedule**; one `render()` reads the DOM, consumes
  arrivals, decides, writes, then schedules due work (`demo.ts:1-10, 280-292, 413-421`). Hover, click and
  Enter all collapse into the next frame, so ten key presses in a frame are ten steps applied once.
- Only changed things are written: `written.*` caches per region (`demo.ts:187-193`), because
  re-writing the line makes a screen reader read it again, and re-writing the text collapses a text
  selection (`demo.ts:186-188`). The status line is compared as `JSON.stringify(line)` (`demo.ts:636-640`).
- The map's pixels are repainted only when the key `loaded + key + searched + rest` changed
  (`demo.ts:667-674`), on an `ImageData` whose buffer is reused as a `Uint32Array` (`demo.ts:661-663`).

---

## 2. The highlighting presentation

### 2.1 Cell colours come from design tokens, not from constants

`demo.ts:203-213` reads two CSS custom properties and two tokens at mount:

```css
--cell-plain-strength: 0.3;    /* an item without a match */
--cell-waiting-strength: 0.12; /* an item a search has not reached */
```
(`assets/json-search.css:9-15`)

`pixelOf(token, strength)` parses a `#rrggbb` token to one `ImageData` 32-bit word at the given alpha
(`demo.ts:206-211`), so the three cell colours are `{match: --copper @ 1, plain: --ink @ 0.3,
waiting: --ink @ 0.12}` (`demo.ts:213`, tokens at `assets/site.css:18-21`). **One hue carries meaning
(copper = has a match); everything else is the text colour at low strength.** There is no per-kind
colour in the map — kinds only colour the shown document (`assets/tool.css:4-12`).

That maps onto our page with no work: `explorer_page.py:18-22` already defines `:root { --ink … }` and a
dark-mode block, so the same trick gives us "match = accent, no match = ink at 0.3, not yet searched =
ink at 0.12" that cannot drift from the page's palette.

### 2.2 The match mark scales with density so one match stays visible

`paintCells` (`demo-map.ts:129-157`), mark geometry in two lines (`demo-map.ts:132-134`):

```
mark   = min(cell + 2*(pitch − cell), 2*cell)   // at most twice the cell's side
before = (mark − cell) >> 1                      // grow symmetrically into the gap
```

- The mark spans the cell **plus the gap on each side**, capped at twice the cell's side, so it never
  covers a neighbouring cell: with cell 2 / gap 1 it takes both gaps (4 px wide), and where the cap binds
  (cell 1 / gap 1) it becomes the cell plus the gap to its right and below, 2×2. Gaps belong to no cell's
  fill, so neighbouring marks *join* where two matches are adjacent (`demo-map.ts:123-124`), which reads
  as a run of matches rather than a blur.
- Pinned cases: at 100,000 items the cell is 2 device px and the mark 4 (≈2 CSS px, `README.md:36`);
  with the smallest cell (1 px, gap 1) the mark is exactly 2×2 — the cell plus the gap to its right and
  below (`demo.test.ts:185-196`), which is the density where a 1-px mark would otherwise vanish.
  At our density the *gap* is the binding constraint instead: 313 items give cell 19 / gap 5 device px,
  so `min(19 + 2×5, 38) = 29` — the cell plus the gap on each side, 14.5 CSS px at scale 2. Expect a
  chunky mark, not a hairline.
- `pixels.fill(0)` at the top of every paint (`demo-map.ts:136-137`): the previous paint's marks grew
  into gaps, so leaving them behind would under-report the new result (`demo.test.ts:180-183`).
- Hot-loop hygiene worth keeping: `colors` destructured and counts kept as a `Float32Array` *before* the
  loop, because reading a colour inside it left the JIT's compiled loop (`demo-map.ts:126-129`).
- The three states paint in one pass by index comparison, not three passes:
  `index < searched ? (count > 0 ? mark : plain) : rest` (`demo-map.ts:140-151`).

### 2.3 The one shown item, beside the map

- Two-column grid, map `minmax(0,1fr)` + `416px` panel, collapsing to one column under 960 px
  (`json-search.css:137-143, 230-234`).
- The item is rendered as **one `<span>` per run of characters that look the same**, not per character
  and not one `<pre>` of coloured text (`demo.ts:694-705`). `documentRuns` (`demo-text.ts:176-191`)
  merges consecutive code points with the same `(kind, match)` pair and returns `{text, kind, match}`;
  `runClassName` (`demo-text.ts:194-200`) turns that into `k<kind>` plus `hit even` / `hit odd`.
- Kinds are the highlighter's own order, `k0..k8` = whitespace, punctuation, string, escape, number,
  literal, error, pastEnd, key (`highlighter/kinds.ts:14`), coloured in `assets/tool.css:4-12` — keys
  deliberately stay in ink so "the structure reads first and the values carry the color".
- **Two matches in a row must stay apart**: marks alternate `1,2,1,2…` from match to match
  (`demo-text.ts:170-171, 182-183`), rendered as `even`/`odd` washes, each with a 1-px copper line under
  it and `box-decoration-break: clone` so a wrapped match keeps its look on both lines
  (`json-search.css:206-215`).
- The panel header is `Document N` (1-based, `demo.ts:687`), tabular numerals.
- The panel is a `<pre>` taken out of flow inside a fixed-height region, so a long item scrolls instead
  of stretching the page (`json-search.css:180-204`, `json-search.html:90-93`).
- Same rule as the map: the spans are rebuilt only when `index + key` changes, so a selection inside the
  text survives a repaint (`demo.ts:690-692`).

**Adaptation note for us**: the *axis* changes. Their colours are a JSON grammar; ours are whatever
ticket 03 decides shows a match on a printed page (an image with a `<mark>` box, or the extracted text
as runs). Keep the mechanism — a run-length encoder over `(kind, match)` with alternating match washes —
and drop the kind table.

---

## 3. The line of numbers

One `<p id="status" role="status">` between the field and the map (`json-search.html:76`), whose
children are replaced from an array of strings and `{strong}` parts (`demo.ts:404-411`), so bold is
structural, not markup in a string.

`resultLine` (`demo-text.ts:149-163`) is the whole grammar. Order is fixed: **count, of-total, verb,
"This program:", time, where, then the baseline.** Exact strings, pinned in
`tests/json-search/demo.test.ts:299-309`:

| State | Rendered text |
| --- | --- |
| GPU, some matches | `10,541 of 100,000 documents match. This program: 23 ms on the GPU. Native RegExp: 23 ms.` |
| one match | `1 of 20,000 documents matches. This program: 8.1 ms on the GPU. Native RegExp: 2.1 s.` (`verb` singular, `demo-text.ts:157`) |
| no match | `None of the 65,000 documents matches. This program: 871 ms on the GPU. Native RegExp: 29 ms.` |
| CPU | `10,541 of 100,000 documents match. This program: 16 s on the CPU. Native RegExp: 23 ms.` |
| **progressive** | `1,304 of the first 12,400 documents match. This program: 3.1 s on the CPU so far. Native RegExp: 23 ms.` |
| **timeout** | `47,577 of 100,000 documents match. This program: 17 ms on the GPU. Native RegExp: more than 10 s.` |

Rules that make it right:

- **The progressive form is a different sentence, not a suffix**: `of the first N documents` (the number
  answered so far), and ` so far` before the full stop (`demo-text.ts:158-160`). It is chosen by
  `searched < documentCount`, i.e. the count is always qualified by how much was searched — never "928
  of 8,773 match" without the qualifier. `0 of the first 2 documents match. This program: 0.4 ms on the
  CPU so far.` is a legal line (`demo.test.ts:306`).
- Both sides are named — `This program:` and `Native RegExp:` — because "16 s on the CPU" alone reads as
  the browser's own RegExp (`demo-text.ts:150-153`).
- **Time formatting is one function** (`demo-text.ts:126-131`): `≥ 995 ms` → seconds with 1 decimal
  (`≥ 9950 ms` → 0 decimals, so 10 s prints `10 s`); `9.95–994 ms` → whole ms; `< 9.95 ms` → one
  decimal; `< 0.05 ms` → `under 0.1 ms`. Ladder pinned at `demo.test.ts:294`. The space before the unit
  is a no-break space so a line never breaks between number and unit (`demo-text.ts:124-125`).
- The timeout is a *time value*, not a new sentence: `{kind: 'stopped', afterMs: 10_000}` renders
  `more than ${duration(afterMs)}` → `more than 10 s.` (`demo-text.ts:133-147`). The 10 s is the wait for
  the *on-screen* search; a search the page has since left gets 1 s (`demo.ts:115-121`), and the worker
  is terminated rather than interrupted (`demo.ts:725-729`) because a backtracking RegExp cannot be
  stopped any other way.
- The number of answered items is **not** in the sentence for a complete search, and `withMatch === 0`
  gets `None of the N documents matches.` — grammatical, and it names the total rather than saying "0".
- Other states reuse the same slot: `Loading 100,000 documents…` (`demo.ts:596`), `Type a pattern to
  search the 100,000 documents.` (`demo.ts:600`), `Searching 100,000 documents…` (`demo.ts:609`), a
  refused pattern as `Character 7 of the pattern: …` (`demo-text.ts:112`) or `This pattern needs 33
  states, and the largest search program holds 32.` (`demo-text.ts:86-95, 116`), and failures as full
  sentences (`demo-text.ts:68-75`).
- **A line that runs long says so through `aria-busy`, not through words**: `role="status"` plus
  `aria-busy="true"` while the state is `busy`, so a screen reader waits instead of reading every frame
  of a progressive count (`demo.ts:633-635`).
- The previous line and the previous map stay on screen while a search is in flight, so nothing flashes
  between keystrokes (`demo.ts:261-264, 606-609`).

**For us**: we have two lines (`map.md:40` — a query line and a run line) and no RegExp baseline, so the
third clause becomes the *labelled* comparison we actually have (the in-process index vs. the browser
matcher, or declared vs. observed). Keep the sentence shape and the `so far` / `more than` idioms; drop
`Native RegExp:`.

---

## 4. Licence

`LICENSE` at the repo root, SPDX `MIT` (`gh api repos/chenglou/handwired/license`), 1,066 bytes:

```
MIT License

Copyright (c) 2026 Cheng Lou

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- The only obligation is the two-line condition: **the copyright notice and this permission notice in
  all copies or substantial portions of the Software.** Attribution in a README is not what MIT asks
  for; the notice must travel with the copy.
- MIT covers *expression*. Porting `demo-map.ts` to plain JS, copying the `mark`/`before` formula, the
  `nextMatch` modulo loop, the colour-token read, or the exact sentences, are all "substantial portions"
  in practice for a 157-line file. Re-deriving the interaction contract from behaviour (as §1 does) is
  not, but see the next bullet.
- **This repo already ruled on it**: `docs/spec/pdf-workflow-demo.md:216-218` — "Borrow its document-map
  and highlighting presentation if useful … and its MIT notice preserved when reusing source". The map
  repeats it (`.scratch/handwired-demo/map.md:21-25`). And the repo's culture asks for a pointer even
  where no licence compels one: research notes open with a "*Verified against …*" line
  (`research/bruteforce-vs-index.md:3-4`), and `docs/demo/pdf-demo-corpus.md:311-316` has a
  "Licence and attribution" section for corpus material.

**Where the notice goes, concretely:**

1. **This file** (`research/handwired-json-search.md`) — the provenance record, with the commit SHA, so
   a later reader can tell what was read. Done.
2. **In the exported page, visibly**: one quiet credits line under the notes (where handwired puts its
   own instruction line, `json-search.html:96-99`), e.g. *"The map and the find-bar keys follow Cheng
   Lou's handwired JSON search (MIT, © 2026 Cheng Lou) — link."* This is what a stranger who opens the
   bundle receives, so it is the copy that must carry the notice.
3. **In every file that is a derivative**: a header comment naming `chenglou/handwired`,
   `src/json-search/<file>`, the commit, and "MIT, Copyright (c) 2026 Cheng Lou". Cheap, and it makes
   provenance legible per file instead of only per bundle.
4. **The full MIT text inside the export**, since ticket 09 fixes the bundle's tree: a
   `THIRD-PARTY-NOTICES.txt` (or `LICENSES/handwired-MIT.txt`) beside the HTML, referenced from the
   credits line. A page committed to this repo may instead put it in a `<details>` block; either way it
   must be in the artefact a reader downloads, not only in the repo.
5. **Do not name our page "handwired"**, and do not reuse its deck copy (`json-search.html:45`). MIT
   gives no trademark licence, and the sentence "The regex matching, JSON parsing & highlighting are all
   written in JS, then turned into WebGPU kernels" would be a false claim on our page anyway.

If we write our own sentences and our own palette and only take the *contract* (nearest-match picking,
click-to-keep, find-bar stepping, ring, density-scaled mark), the honest minimum is item 2 — one line —
plus item 1. That is cheaper than defending a decision not to credit.

---

## 5. What must not be copied, and why

Our query class measured **0.11 ms on one CPU core** over the 590-Chunk index (in-process FTS5 `bm25`;
`.scratch/handwired-demo/map.md:41-44`; the CLI's 0.25 s is interpreter startup). Everything below is
arithmetic against that number, not taste.

### 5.1 The cells / bit / loop programs — `program.ts`, `loop-program.ts`, `emit.ts`, `bit-emit.ts`, `loop-emit.ts`, `generated-js.ts`

- These exist to make *one fixed program run over 100,000 documents in one GPU pass*
  (`README.md:102-126`): a document is 513 float32 cells = 2,052 bytes, 100,000 documents are 205 MB of
  cells plus 205 MB of kinds (`README.md:106, 47`, `program.ts`), and a shader's length is the work of one
  position rather than one document (`README.md:126`).
- The premise is uniform, fixed-width records. Our Chunks are variable-length text already indexed; we
  do not read them all per query, so there is nothing to vectorise.
- Even their CPU path — the same loop programs as generated JS — costs **0.14 ms per 300-code-point
  document at 8 states** (`README.md:74`). For 590 Chunks that is ~83 ms, ~750× our measured 0.11 ms,
  and 2.4×/13× worse at 16/32 states (`README.md:74`). Copying it would *replace* a 0.11 ms lookup with
  a scan, in a worker, behind a message hop.
- `bit-emit.ts` (70 KB) is the cleverest part and the most useless to us: it proves which values are
  integers and bits so that the test-against-transition-table gate becomes one AND of two 32-bit words
  (`README.md:180-184`), packing 1,024 cells into 272 words. Its payoff is 503 → 147 statements and
  7.8 → 4.6 ms (`README.md:185-189`) — a factor on a workload we do not have.

### 5.2 The WebGPU host and shader writer — `gpu/host.ts` (25 KB), `loop-emit.ts`, `bit-emit.ts`

- The GPU is a *loss* at our size before any work starts: "a round trip to the GPU alone takes 0.6 ms or
  more" (`README.md:3`), and their own decision table says a GPU is "of no use for one document"
  (`README.md:3`). 0.6 ms is already ~5× our entire query; the answer would arrive a frame or more after
  the keystroke, i.e. visibly later than answering synchronously.
- The whole apparatus is about buffer limits, shader compile timing, `salt` to defeat the shader cache,
  and readback (`README.md:67-73, 195-201`, `demo.ts:330-402`). None of it has a meaning for a static
  page with a few hundred cells.
- Also ruled out by our own record: `docs/adr/0008-one-local-search-backend.md` ships **one** backend
  behind the search contract; the map's "Out of scope" lists GPU/WebGPU explicitly
  (`.scratch/handwired-demo/map.md:72-74`). A shader writer would be a second engine by another name.

### 5.3 The JSON highlighter — `json-step.ts`, `highlighter/tables.ts`, `highlighter/kinds.ts`, `table-loop.ts`, `plain-search.ts`

- It answers "what is each of the first 512 code points of a *JSON document* shown as, and where is the
  first error" (`README.md:84-92`): a 37-operation step at each of 512 positions plus a 17-operation end
  step, 18,961 operations per document (`README.md:139`). Our shown item is a printed page of a PDF; its
  "kinds" are not JSON grammar tokens, and ticket 03 is still deciding whether the match lands on a page
  image at all.
- `table-loop.ts` and `plain-search.ts` are interesting only as *their* baseline: a plain JS highlighter
  plus `RegExp` per document, on one thread, to time RegExp for comparison (`README.md:33, 82`). We have
  no RegExp baseline to publish — and their own README warns the two disagree on match semantics
  (`README.md:96`: `a|ab` finds `ab` where JS finds `a`), so a copied baseline would be a misleading
  number next to ours.
- `plain-search.ts` also encodes `RegExp`'s worst case as a *feature* of the demo (the 10 s timeout,
  `README.md:34`). Our matcher should not be able to backtrack at all.

### 5.4 The pattern language and its automaton — `regex/pattern.ts` (14 KB), `regex/data-program.ts`, `regex/bits.ts`

Not named in the ticket, but in the same directory and equally out of scope: it is a JavaScript-regex
subset compiled to an automaton with one state per character or class (`README.md:98`). Our page's
matching semantics are ticket 06's decision, and the honest default there is plainer than regex.

---

## 6. What to take, mapped to our page

| Take | From | Why it survives the scale change |
| --- | --- | --- |
| `cellAt` / `cellCenterCss` / layout walk with a height ceiling | `demo-map.ts:30-61` | Pure arithmetic; pins 313 or 590 cells to one screen with no scroll maths |
| Nearest-match picking with ties to the first, one linear pass | `demo-map.ts:67-99` | ~590 comparisons per pointer event is nothing; no spatial index needed |
| Click keeps / second click releases; mouse picks by pointer, finger by tap | `demo.ts:566-574` | The whole asymmetry is 8 lines and one real bug avoided |
| `nextMatch` with wrap and "`from` only if it is the only match" | `demo-map.ts:101-110` | Exactly the find-bar semantics; ~8 lines |
| `window` keydown, excluding `a, button` only | `demo.ts:796-800` | Keeps Enter working with focus anywhere |
| Ring moved by transform, heavier when kept, `pointer-events: none` | `demo.ts:675-684`, CSS `165-178` | Cheapest possible "where am I" affordance |
| Colours read from CSS tokens at mount | `demo.ts:203-213` | Reuses `explorer_page.py`'s existing `--ink` and dark mode |
| `mark`/`before` density formula, `fill(0)` before paint | `demo-map.ts:129-157` | The one thing that makes a single match findable in a dense grid |
| Runs of `(kind, match)` spans, alternating match washes | `demo-text.ts:176-200`, CSS `206-215` | Works for page text or an overlay; two adjacent matches stay distinct |
| `resultLine` shape + `duration` ladder + `so far` / `more than` | `demo-text.ts:126-163` | Our query line needs exactly this grammar |
| Frame discipline: record → one render → write only what changed | `demo.ts:1-10, 187-193, 413` | Protects `aria-live` and text selection |
| Reveal = the ring on the cell + `scrollTop` to the match inside the item | `demo.ts:675-684, 707-710` | At 313/590 cells no cell is off-screen (§1.7), so this *is* the whole reveal |

---

## Open questions this note hands on

- **Confirm the map's width, then the grid never scrolls.** The numbers in §1.7 are for handwired's
  630-CSS-px map; any width up to a full page keeps 313/590 cells at the largest pitch and under the
  ceiling. A grid that *does* scroll would need the one thing handwired has no code for
  (`scrollIntoView` on keep/step, never on hover).
- **The 0.11 ms figure** is recorded in the map (`map.md:41-44`), not in a committed artifact; ticket 05 owns
  making it reproducible. The §5 arithmetic is stable either way — even at 1 ms, the ratio holds.
- **The second clause of our line** — what replaces `Native RegExp:` — is ticket 06's wording decision.

## References

- Source, pinned: <https://github.com/chenglou/handwired/tree/257856e3b221140acb4cc4460626b850c0bd4ff1/src/json-search>
- The demo's own writeup: `src/json-search/README.md` (the page's behaviour at lines 27-53, the CPU
  path at 39-43, the module contract at 55-82, the two writers at 175-193).
- Live page: <https://chenglou.me/handwired/json-search/>
- Licence: <https://github.com/chenglou/handwired/blob/main/LICENSE> (MIT, © 2026 Cheng Lou).
- This repo: `docs/spec/pdf-workflow-demo.md:208-221` (the Handwired ruling),
  `.scratch/handwired-demo/map.md:21-25, 41-44, 70-78`, `docs/adr/0008-one-local-search-backend.md`,
  `research/bruteforce-vs-index.md` (what their pipeline is actually for).
