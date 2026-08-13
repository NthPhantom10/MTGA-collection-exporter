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
It outputs two files:
- `mtga_collection.json`: Full data including card IDs and quantities.
- `mtga_collection.txt`: A readable list of your cards (Count + Name).

## How to use

### Run from Python Source (Windows & macOS)
1. Download and extract zip
2. Navigate inside folder
3. Install Python 3.10+
4. Install dependencies:
   ```bash
   pip install .
   ```
5. Run `python mtg.py`

> **macOS:** `pymem` is skipped automatically. If you get a permission error, run with `sudo python mtg.py`.

## Troubleshooting
- If the tool cannot find your collection, ensure you have visited the Collection/Decks tab.
- Try providing different anchor cards if the first attempt fails (rarer anchor cards such as [O:legendary] work better, as they are more unique to your collection).
- **Windows:** Run as Administrator if you encounter permission errors.
- **macOS:** Run with `sudo python mtg.py` if you get a permission error.
- **First run is slow:** The Scryfall card database (~250 MB) is downloaded once and cached as `arena_id_lookup.json`.

## Output files
- `mtga_collection.txt`: Readable list — `Count Name (SET)`
- `mtga_collection.csv`: Moxfield-compatible import
- `mtga_collection.json`: Full data with count, name, set, and collector number

## Files
- `MTGA_Exporter.exe`: Standalone Windows application.
- `mtg.py`: Source code (Windows + macOS).
- `pyproject.toml`: Python dependencies.
- `install.bat`: Setup script for Windows Python users.
