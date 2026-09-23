# HTML report design

Read this reference only when HTML output is requested, together with `report-design.md` for the
shared section and evidence contract. Markdown-only reports do not need this reference. The checks
below run with `research-validate <project-dir> --report-format html` (or `both`; legacy `--report`
also checks both files).

## The HTML: what it may load

**Nothing, with one exception.** The report is opened from disk, often long after the work, so a
dead CDN link is a report that renders wrong for reasons nobody will diagnose. All CSS is inline in
`<style>`; all charts are inline SVG; no external script; no `@import`.

The single exception is a web-font stylesheet from `https://fonts.googleapis.com/`, and every family
it loads needs a real fallback stack so the page is right when the font does not arrive.
`research-validate --report-format html` enforces exactly this: an external script, any other
stylesheet host, or an `@import` is an error.

## The CSS baseline

Colour lives in custom properties and nowhere else. That is what lets the three theme blocks
redefine the palette without touching a rule, and `--report-format html` treats a colour literal in
any colour-carrying property outside a `--*` definition as an error.

Three theme blocks, all three required, in this order:

```css
/* 1. The light palette, on bare :root. Every token is defined here and only redefined below. */
:root {
  --ground: #f7f8fa;        /* page */
  --surface: #ffffff;       /* cards, tables */
  --surface-sunk: #eef0f4;  /* table stripes, code */
  --ink: #101418;           /* body text */
  --ink-soft: #3a444f;      /* secondary text */
  --slate: #5c6673;         /* labels */
  --slate-faint: #8b95a1;   /* axis ticks */
  --rule: #dce0e7;          /* hairlines, gridlines */
  --rule-strong: #c3c9d2;   /* axes */
  --accent: #0f6e6a;        /* the quantity that matters */
  --accent-soft: #e2f0ef;
  --pass: #1a7f4f;
  --pass-soft: #e3f2e9;
  --note: #a86518;          /* caution, not failure */
  --note-soft: #f8eddd;
  --esc: #b03a2e;           /* escalation, failure */
  --esc-soft: #fae9e6;
  --sans: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  --serif: "Source Serif 4", Georgia, "Times New Roman", serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}

/* 2. The viewer's system preference, guarded so an explicit light choice still wins. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0d1014;  --surface: #151a20;  --surface-sunk: #1c2229;
    --ink: #e6eaef;     --ink-soft: #c3cbd4;
    --slate: #9aa4b0;   --slate-faint: #6d7783;
    --rule: #262d36;    --rule-strong: #38414c;
    --accent: #45b8ad;  --accent-soft: #10312f;
    --pass: #4cc287;    --pass-soft: #102a1e;
    --note: #d99a45;    --note-soft: #2e2314;
    --esc: #e2705f;     --esc-soft: #2f1714;
  }
}

/* 3. An explicit dark choice, which must win in both directions. Same values as block 2. */
:root[data-theme="dark"] {
  --ground: #0d1014;  --surface: #151a20;  --surface-sunk: #1c2229;
  --ink: #e6eaef;     --ink-soft: #c3cbd4;
  --slate: #9aa4b0;   --slate-faint: #6d7783;
  --rule: #262d36;    --rule-strong: #38414c;
  --accent: #45b8ad;  --accent-soft: #10312f;
  --pass: #4cc287;    --pass-soft: #102a1e;
  --note: #d99a45;    --note-soft: #2e2314;
  --esc: #e2705f;     --esc-soft: #2f1714;
}
```

Two blocks are not enough and the reason is not obvious. Block 2 alone leaves a viewer who has
chosen dark on a light system stuck in light; block 3 alone ignores the system preference of every
viewer who has chosen nothing. Both, with block 2 guarded, cover all four combinations.

Give `body` an explicit `background: var(--ground)`. A transparent body borrows whatever ground the
viewer's browser paints, which is how a dark-mode page ends up with black text on black.

The palette above is a starting point, not a house style. Change the hues; keep the token names, the
role each token plays, and all three blocks.

## Typography and layout

```css
* { box-sizing: border-box; }
body {
  background: var(--ground);
  color: var(--ink);
  font-family: var(--serif);
  font-size: 16.5px;
  line-height: 1.62;
  margin: 0;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 0 28px 96px; }
.col  { max-width: 660px; }   /* prose only; tables and charts use the full wrap */
h1, h2, h3 { font-family: var(--sans); }
.num, code, pre { font-family: var(--mono); font-variant-numeric: tabular-nums; }
```

- **Serif body, sans headings, mono numerals.** The serif is what makes several hundred words of
  prose readable; the mono with `tabular-nums` is what makes a column of figures comparable down the
  page.
- **Prose is measured, tables and charts are not.** Prose in a `max-width: 660px` column — roughly
  70 characters — while a table or a chart gets the full 1080px. A full-width paragraph is the
  single most common way a technical page becomes unreadable.
- **`<h2>` earns its space.** The five contract sections are the page's skeleton; set them apart
  (a rule above, generous margin) so the reader can scan to one.
- **Tables**: right-align numbers, left-align labels, hairline rules in `var(--rule)`, no vertical
  borders. Wrap a wide table in its own `overflow-x: auto` container so the page body never scrolls
  sideways.
- **Phone width (~400px)** has to work: relative units, a side gutter of at least 16px, and nothing
  with a `min-width` wider than the screen.

## Charts

**When.** Prefer a chart when the report compares a quantity across three or more categories, or
over a series. Prefer a table when exact values are what the reader needs — and a chart plus a table
is often right, the chart carrying the shape and the table the figures. Use neither when nothing is
being compared: a chart of one number is decoration, and decoration in a technical report costs
credibility.

**How — these rules are absolute, and `--report-format html` checks every one of them:**

- **Hand-authored inline SVG.** No charting library, no image file, no `<canvas>`.
- **Every colour a `var(--…)` token**, on `fill` and `stroke` alike, so the chart follows the theme.
- **`role="img"` and a non-empty `aria-label`** on every `<svg>`. The label carries the finding in
  words, not the chart's title: a reader on a screen reader, in a terminal, or looking at a printout
  should learn the same thing a viewer does.
- **Every chart inside a `<figure>` with a non-empty `<figcaption>`.** The caption carries the
  *interpretation* — what the shape means — rather than restating the heading. A `<figcaption>`
  holding only whitespace or only markup counts as absent.

An `<svg>` outside a `<figure>` is an error even when its labels are perfect, because there is
nowhere for its caption to go.

### Verify the geometry arithmetically

Check the scale and coordinates against the viewBox. When visual inspection tools are available,
also inspect the rendered chart; arithmetic alone cannot establish readability.

State the scale in an SVG comment, then verify each mark:

```html
<svg viewBox="0 0 640 250" role="img"
     aria-label="Relative difference in mean prediction, DQS versus production: bha exactly zero, the three steps spread from minus 5.0 to plus 6.1 percent, the composed prediction minus 0.97 percent.">
  <!-- scale: 1% = 18px, zero at x=400 -->
  <line x1="400" y1="20" x2="400" y2="200" stroke="var(--rule-strong)" stroke-width="1.5"/>
  <rect x="310"   y="72"  width="90"   height="16" fill="var(--note)"/>   <!-- −5.0% -->
  <rect x="400"   y="106" width="50.4" height="16" fill="var(--note)"/>   <!-- +2.8% -->
  <rect x="382.5" y="174" width="17.5" height="16" fill="var(--accent)"/> <!-- −0.97% -->
</svg>
```

Every one of those is checkable without rendering anything: `5.0 × 18 = 90` and a negative bar
starts at `400 − 90 = 310`; `2.8 × 18 = 50.4` from `x = 400`; `0.97 × 18 = 17.46 ≈ 17.5` from `400 −
17.5 = 382.5`. Do this for bars, tick positions, and label anchors. Two failures this catches that
nothing else will: a bar whose length does not match its printed value, and a bar that runs off the
`viewBox`.

Keep every mark inside the `viewBox` with room for labels — the reference chart plots to `y=200` and
puts axis ticks at `y=216` and a caption line at `y=240` inside a 250-high box. Use
`text-anchor="end"` for labels left of an axis and `middle` for ticks under one, and check that a
label at `x=150` with `text-anchor="end"` has 150px of room for its longest string.

**Bar, dot, or line, and nothing else.** A horizontal bar chart for categories, a dot for an exact
point on a scale, a line for a series. No pie charts: the report's readers compare magnitudes, and
angles are the worst encoding for that.

## Task-graph charts

Under the task-graph subsection, add a dependency-level profile and an execution timeline when they
help explain the work. A profile shows one horizontal bar per dependency level, with length equal to
the number of tasks at that level. A timeline shows measured tasks within the recorded evidence
window. Keep the exact task values in the table described in `report-design.md`.

### The profile's geometry

```text
row pitch      28px, bar height 16px, first bar at y=40
label gutter   x=0..112, level labels text-anchor="end" at x=104
plot area      x=120..600, so 480px wide
scale          480 / (tasks at the widest level) px per task
height         28 x levels + 54
```

Worked, for the 14-task graph of this contract's own project — 7 levels, widest level 3 tasks:

- `height = 28 x 7 + 54 = 250`, so `viewBox="0 0 640 250"`.
- `scale = 480 / 3 = 160` px per task.
- The widest level, 3 tasks: `x=120`, `width = 3 x 160 = 480`, right edge `120 + 480 = 600`, exactly
  the plot edge and inside the `viewBox`.
- A level holding 1 task: `width = 160`, right edge `280`.
- Row tops are `40, 68, 96, 124, 152, 180, 208`; the last bar's bottom is `208 + 16 = 224`, leaving
  `250 - 224 = 26px` for the caption line.

No bar can leave the `viewBox`, because the widest level is what set the scale.

### The timeline's geometry

```text
row pitch      28px, bar height 16px, first bar at y=40
label gutter   x=0..112, task ids text-anchor="end" at x=104
plot area      x=120..600 spanning the whole measured window
scale          480 / window minutes px per minute
bar            x = 120 + (task start - window start) x scale
               width = max(3, span x scale), then x = min(x, 597)
height         28 x measured tasks + 54
```

Worked, for the predecessor project `2026-09-10-002` — window 11:12 to 12:27, 75 minutes, 15 tasks
all measured:

- `scale = 480 / 75 = 6.4` px per minute; `height = 28 x 15 + 54 = 474`.
- A task spanning 11:12 to 11:17 — minute 0 to 5: `x = 120 + 0 x 6.4 = 120`,
  `width = 5 x 6.4 = 32`.
- A task spanning 12:22 to 12:27 — minute 70 to 75: `x = 120 + 70 x 6.4 = 568`, `width = 32`, right
  edge `600`.
- A task with a single recorded command at 11:40 — minute 28, span 0: `x = 120 + 28 x 6.4 = 299.2`,
  `width = max(3, 0) = 3`. Without that floor a single-entry task draws nothing at all, which reads
  as "did not run" rather than "ran once".
- The floor is the only way a bar can overrun: a single entry at the very end of the window would
  start at `x=600` and draw to `603`. Clamping the start to `x=597` keeps it inside, which is why
  the clamp is written down rather than left to whoever notices.

The window itself is the first and last readable stamp in the whole project, so the leftmost bar
starts at `x=120` and the rightmost ends at `x=600` by construction. State the window's real clock
times in the `aria-label` and the count of unmeasured tasks in the `<figcaption>`: a timeline that
silently plots 11 of 15 tasks is a chart that lies by omission.
