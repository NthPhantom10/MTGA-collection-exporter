# Changelog - V3.4.1

v3.4.1 Changelog:
- **macOS:** freed `mach_vm_read` buffers against the reader's own task port. They were never freed, so a full scan leaked gigabytes.
- **macOS:** reads now tolerate unmapped holes. `mach_vm_read` fails wholesale if any byte in the requested range is unmapped, so the multi-megabyte windows taken around each anchor hit were silently returning nothing and the collection was never located.
- Block scoring no longer lets a duplicate-free block outrank one that matches the anchors. The previous sort key preferred small, clean decoy arrays over the real collection.
- A candidate block must now match at least one anchor. Previously a block matching zero anchors was accepted and written out as a successful export.
- Card metadata read from the local Arena database now includes mana cost, type line, and a named rarity (`rare` rather than `4`). Only Scryfall supplied these before, and Scryfall was never reached on a machine with Arena installed.
- Rules text no longer carries Arena's raw mana placeholders: `{oT}`, `{o2oB}` and `{o(G/W)}` now render as `{T}`, `{2}{B}` and `{G/W}`. ICU message placeholders such as `{1, CardId}` and prose in braces are preserved verbatim.
- Anchor setup asks which printing you mean when a card name has several, instead of silently picking one. The wrong printing produces zero memory hits.
- Cached card databases are now versioned, so caches written by older versions are rebuilt automatically.
- Added a pytest suite covering rarity/mana/type-line extraction, rules-text normalisation, cache versioning, and printing selection.

# Changelog - V3.4

v3.4 Changelog:
- Fixed large broken quantities by no longer summing duplicate Arena IDs during memory extraction.
- Duplicate IDs are now tracked and used to penalize/reject dirty memory blocks.
- Anchor quantities are forced back to the user-provided values when present in the chosen block.
- Added Moxfield CSV export. (Whoops)
- Added optional A- prefix normalization for card names when the base name exists in the database.
- Reduced duplicate export rows by normalizing card names/set codes and merging blank-set duplicates when safe.
- Added --keep-a-prefix flag.


imported collection to moxfield:
<img width="1901" height="962" alt="image" src="https://github.com/user-attachments/assets/4f784272-e2fc-4521-8aa1-9137c1029aa4" />

Better text file
- before: 
<img width="1080" height="467" alt="image" src="https://github.com/user-attachments/assets/c0bb05cd-4996-4b2a-8c12-7b4bba20aabe" />

- after: 
<img width="1112" height="480" alt="image" src="https://github.com/user-attachments/assets/9609dd74-69c2-4c85-9ea1-8a9c35aa7d6e" />

Progress bars: 
<img width="388" height="96" alt="image" src="https://github.com/user-attachments/assets/ccc5c324-3f62-430b-bc74-366c4f9314d9" />

# MTG Arena Collection Exporter

This tool scans your game memory while MTG Arena is running to export your entire card collection.
It outputs seven files (listed under [Output files](#output-files)); the two main ones are:
- `mtga_collection.json`: Full data including card IDs and quantities.
- `mtga_collection.txt`: A readable list of your cards (Count + Name).

## How to use

### Run from Python Source (Windows, macOS & Linux)
1. Download and extract zip
2. Navigate inside folder
3. Install Python 3.10+
4. Install dependencies:
   ```bash
   pip install .
   ```
5. Run `python mtg.py`

> **macOS / Linux:** `pymem` is skipped automatically. If you get a permission error, run with `sudo python mtg.py`.
>
> **Linux:** Works with the Steam/Proton build of Arena — the tool reads the `MTGA.exe` process via `/proc/<pid>/mem`. If your system sets `kernel.yama.ptrace_scope` to 1 or higher, run with `sudo`.

## Troubleshooting
- If the tool cannot find your collection, ensure you have visited the Collection/Decks tab.
- Try providing different anchor cards if the first attempt fails (rarer anchor cards such as [O:legendary] work better, as they are more unique to your collection).
- **Windows:** Run as Administrator if you encounter permission errors.
- **macOS:** Run with `sudo python mtg.py` if you get a permission error.
- **Linux:** Run with `sudo python mtg.py` if attaching fails (see `/proc/sys/kernel/yama/ptrace_scope`).
- **First run:** card data is read from your local Arena files (`MTGA_Data/Downloads/Raw`) and cached as `arena_id_lookup.json`. The Scryfall bulk download (~250 MB) is only used when no local Arena install is found. Use `--force-refresh` to rebuild the cache.

## Output files
- `mtga_collection.txt`: Readable list — `Count x Name (SET) #Number [rarity]`
- `mtga_collection.json`: Full data with count, name, set, collector number, rarity, mana cost, type line and oracle text
- `mtga_collection_moxfield.csv`: Moxfield import
- `mtga_collection_deckbox.csv`: Deckbox import
- `mtga_collection_goldfish.csv`: MTGGoldfish import
- `mtga_collection_cardsphere.csv`: CardSphere import
- `mtga_collection_stats.txt`: Totals broken down by rarity and by set

## Files
- `MTGA_Exporter.exe`: Standalone Windows application.
- `mtg.py`: Source code (Windows + macOS + Linux).
- `pyproject.toml`: Python dependencies.
- `install.bat`: Setup script for Windows Python users.
