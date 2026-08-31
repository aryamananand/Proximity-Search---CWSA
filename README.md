# Collected Works of Sri Aurobindo — Proximity Search

A desktop app for searching the Collected Works of Sri Aurobindo (or any
folder of PDFs) for words or phrases that occur near each other — "love"
within 15 words of "limit," for example — without the match ever crossing a
page, chapter, or line boundary.

Works the same way on **Windows, macOS, and Linux**.

## What changed in this version (compared to previous versions not included in this repo)

- **No more hard-coded path.** The app used to look for
  `~/Desktop/MISC/Collected Works of Sri Aurobindo`, which only made sense on
  the one machine it was written on. It now remembers whatever folder you
  choose (via `Library → Change Library Folder…`) on a per-machine basis, so
  each device just opens straight into your library.
- **The search index moved out of your PDF folder.** It used to write a
  hidden `.proximity_search.sqlite3` file next to your PDFs, which breaks on
  read-only, network, or synced folders. It's now stored in the normal
  per-OS app-data location, so any folder of PDFs works, and your documents
  folder stays untouched. (First launch of this version will re-index once,
  since the old index files aren't reused.)
- **Faster search.** The old code ran a separate database query for every
  single candidate match to fetch its words and surrounding lines. That's
  now batched into a handful of queries regardless of how many candidates
  there are, plus a couple of missing indexes were added — noticeably
  quicker on large collections.
- **Cleaner, consistent look everywhere.** Text now uses each platform's own
  system font (Segoe UI on Windows, San Francisco on macOS, Noto/Ubuntu/
  Cantarell on Linux) instead of fonts that only exist on macOS, so the app
  no longer falls back to a generic/ugly font on Windows or Linux. The
  reading pane also switched from a serif book font to the same clean
  regular sans-serif used throughout, with more line spacing for easier
  reading.
- **Small usability additions:** a menu bar (`Library`, `Help`), a busy
  indicator while indexing, a friendlier empty/no-results state, the app
  remembers your last word-gap setting, and opening the source PDF uses a
  safer, more reliable method on every OS.

## Requirements

- Python 3.10 or newer
- The packages in `requirements.txt`: PySide6 (the GUI) and PyMuPDF (PDF
  reading)

## Setup

Open a terminal / command prompt in this folder, then:

### Windows
```
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python Sri_Aurobindo_Proximity_Search.py
```

### macOS
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python Sri_Aurobindo_Proximity_Search.py
```

### Linux
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python Sri_Aurobindo_Proximity_Search.py
```
On Linux, the "Open PDF" button relies on `xdg-open`, which almost every
desktop distro ships with already.

## Using it

1. On first launch, pick the folder that contains your PDFs. It's indexed
   once (this can take a few minutes for a large collection) and remembered
   from then on.
2. Type one or more words in the search box and press Enter (or click
   Search).
3. Use the **Word gap** control to widen or narrow how far apart the words
   are allowed to be within the same line.
4. Click any result to read it in context in the right-hand pane, with the
   matched words highlighted.
5. `Library → Change Library Folder…` to point the app at a different set
   of PDFs at any time; `Library → Re-index PDFs` to rebuild the index (for
   example after adding new files).

## Repeating this setup on another device

Copy this folder (the `.py` file, `requirements.txt`, and this `README.md`)
to the other machine, follow the Setup steps above for that OS, and point it
at that machine's copy of the PDF folder the first time it runs. The search
index is rebuilt locally on each device — nothing about it needs to be
copied over.


---
*Use the program file entitled "PROXIMITY SEARCH - CWSA - FINAL.py".
