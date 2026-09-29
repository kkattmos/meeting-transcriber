# DESIGN.md — the summary PDF

The design every summary PDF follows. `summarize/pdf.py` (`_css()` and the
markup passes around it) implements it, and the four prompts in
`summarize/prompts/` write the Markdown it expects. Change the three together.

The style reference is the operator's exercise sheet
`Signal_Exercise_2110203` (2026-09-29). It guides the **styling only**: the
title block, the navy section banners, the colour-coded boxes, the tables and
the type. Its content structure (practice questions, workspace boxes, hint
pages) is not copied: a summary is a study sheet, not an exercise set.

## Principles

1. **The summary first.** The first thing on page 1 is the title, and the page
   holds the notes and nothing else. Keyframes, reference slides and the
   transcript are opt-in appendices (`PDF_FRAMES`, `PDF_RESOURCES`,
   `PDF_TRANSCRIPT`).
2. **Structure you can see.** Every numbered section opens with a banner, and
   the boxes are what a reader scans for when revising: green for what to
   learn, amber for what to avoid, red for what must not be missed.
3. **One face per job.** Body text in the run's chosen font; maths always in
   Computer Modern; code always in JetBrains Mono on a dark editor panel.
4. **Equal sizes, not equal numbers.** The three body faces are scaled to the
   same x-height, so a sheet looks the same size in any of them.
5. **Nothing points into the recording.** No timestamps and no frame
   citations. The notes stand on their own.

## Page

| | |
|---|---|
| Size | A4 (`PDF_PAGE_SIZE`) |
| Margins | 16 mm top, 15 mm sides, 18 mm bottom |
| Footer | left: the document title; right: `page / pages`. 7 pt (size-matched), faint grey |

## Type

| Role | Face | Size |
|---|---|---|
| Body | The run's font: **Bai Jamjuree** or **Sarabun** (Thai); **CMU Serif**, Sarabun or Bai Jamjuree (English) | `PDF_FONT_SIZE`, default **9.5 pt**, as Computer Modern |
| Maths: typeset formulas | Computer Modern (matplotlib mathtext, `fontset=cm`), as SVG | nominal × `PDF_MATH_SCALE` (1.0) |
| Maths: symbols typed into prose (ω, ≤, ⇒, ∑, ², ℝ) | CMU Serif → Latin Modern Math → STIX Two Math → DejaVu Serif | nominal |
| Code: blocks and inline | JetBrains Mono → JetBrainsMono Nerd Font → DejaVu Sans Mono | the text's x-height |
| Title (H1) | body face, bold | 2.0 em |
| Section banner (H2) | body face, bold, white | 1.2 em |
| H3 / H4 | body face, bold | 1.1 em / 1.0 em |
| Subtitle, source | body face | 0.95 em / 0.85 em |
| Colophon | body face | 0.78 em |

**Size matching.** "9.5 pt" means *Computer Modern at 9.5 pt*. The x-heights
were measured from the font files: CMU Serif 0.431 em, Sarabun 0.500 em, Bai
Jamjuree 0.499 em, JetBrains Mono 0.550 em. A face is set at
`nominal × 0.431 / x-height`, so Sarabun at a nominal 9.5 pt is 8.19 pt and
its lower-case letters are exactly as tall as Computer Modern's. Maths stays
at the nominal size, which keeps it level with the text in any face. The
numbers live in `summarize/fontchoice.py`.

**Line height.** 1.62 for Thai (vowels and tone marks stack above and below
the line) and 1.42 for English.

**Font choice.** Per run from the web UI or `pipeline.sh --pdf-font`. The
defaults are `PDF_FONT_TH` (Bai Jamjuree) and `PDF_FONT_EN` (CMU Serif). The
choice is recorded in the document's provenance (`font:`), so a re-render
keeps it. Computer Modern is not offered for Thai because it has no Thai
glyphs. An English sheet in CMU sets any Thai name in Sarabun.

## Colour

| Token | Value | Used for |
|---|---|---|
| Ink | `#1b1f27` | body text |
| Heading ink | `#16233a` | H1, H3, table headers |
| Muted | `#5f6875` | subtitle, source line |
| Faint | `#8a93a0` | footer, colophon |
| Hairline | `#d3d9e1` | table rules, colophon rule |
| Navy | `#1f3a5f` | section banners |
| Link | `#2d5b8f` | links in the body |
| Table header | `#e9eef5` | header row fill |

### Callout boxes

A tinted panel with a 3 pt rule down the left and a bold title in the rule's
colour. The must-remember box is also framed all round, like the sheet's red
box.

| Tag | Meaning | Fill | Rule | Title |
|---|---|---|---|---|
| `[!CONCEPT]` | definitions, key formulas, core ideas, key takeaways | `#edf5ef` | `#2f7d4f` | `#1e6a3d` |
| `[!EXAMPLE]` | worked examples, demonstrations, past-exam questions | `#edf3fa` | `#2f6aa3` | `#1f578c` |
| `[!WARNING]` | common mistakes, pitfalls, exam traps, gotchas | `#fcf4e5` | `#c7811f` | `#8a5810` |
| `[!IMPORTANT]` | must-remember: exam scope and dates, deadlines, blockers, prerequisites | `#fcefef` | `#b3373b` (+ `#deaaac` frame) | `#9c2b30` |
| `[!NOTE]` | side remarks, discrepancies, context, "From <textbook>" additions | `#f4f6f8` | `#9aa4b1` | `#46505d` |
| plain `>` quote | a quotation | as NOTE, no title | | |

Aliases are accepted and folded: TIP, KEY, DEFINITION, SUMMARY → concept;
CAUTION, MISTAKE → warning; EXAM, DANGER, REMEMBER → important; INFO, QUOTE →
note; an unknown tag → note. An empty title gets the kind's default ("Key
concept", "Example", "Watch out", "Important", "Note").

### Code panel

| | |
|---|---|
| Panel | `#1e2229`, 5 pt corners, never split across pages |
| Window bar | `#2b313b`, with red/yellow/green dots (`#ff5f57` `#febc2e` `#28c840`) on the left and the fence's language on the right |
| Text | `#e3e7ee`, JetBrains Mono, line height 1.45, long lines wrap |
| Syntax colours | Pygments `one-dark` (without Pygments the text is plain) |
| Inline code | the same face on `#262b34`, 2.5 pt corners |

## Components, top to bottom

1. **Title**: the model's `# Title`, bold, 2.0 em. It also becomes the footer's
   running title and the PDF's metadata title.
2. **Subtitle**: `Lecture notes · 2026-09-29` (the kind comes from the prompt:
   Lecture notes, Tutorial guide, Meeting summary, Video summary), plus
   `clip …` or `N videos` where they apply. Muted, 0.95 em.
3. **Source lines**: the wrapper's link lines (`Youtube Link: …`,
   `Clip: …`, one per video in a combined document), lifted out of the body
   and printed here in muted grey, with backticks dropped and URLs linked.
4. **Opening paragraph**, then an optional `[!IMPORTANT]` box: what the
   instructor flagged, or what is urgent.
5. **Sections**: `## N. Title` as a navy banner (white bold, 4/9 pt padding, 3 pt
   corners, 16 pt above, 8 pt below, kept with what follows). Inside them:
   paragraphs (5 pt apart), bullets (15 pt indent, 1.5 pt apart), `### N.M`
   sub-headings, tables, callout boxes, maths and code panels.
6. **Colophon**: `Generated by meeting-transcriber · model … · prompt … · run …
   · font …`, faint, 0.78 em, above a hairline.
7. **Appendices**, only when asked for, each on a new page.

`---` rules are not drawn, because the banners already separate the sections.
Tables are full width with 0.6 pt hairlines, a tinted bold header row, and
rows that never split across pages.

## What the prompts must write

The design only works if the Markdown uses it. All four prompts
(`video`, `meeting`, `lecture`, `tutorial`) ask for:

- exactly one `# Title`, first;
- `## N. Section` headings, numbered from 1;
- callouts as `> [!TAG] Title` blockquotes, with every line of the box
  starting with `>`, a few per section at most;
- every formula and maths symbol inside `$…$` / `$$…$$`, never a bare ω or ≤
  (the PDF still catches the stray ones and sets them in Computer Modern);
- code in fenced blocks tagged with their language;
- tables with a single header row;
- no timestamps into the recording and no frame citations.

In any other Markdown reader the callouts still show as ordinary quotes, and
GitHub and Obsidian render NOTE / WARNING / IMPORTANT as their own alerts.
