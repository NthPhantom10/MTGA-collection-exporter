# MTG Arena Collection Exporter

This tool scans your game memory while MTG Arena is running to export your entire card collection.
It outputs two files:
- `mtga_collection.json`: Full data including card IDs and quantities.
- `mtga_collection.txt`: A readable list of your cards (Count + Name).

## How to use

### Option 1: Run the Executable (Simplest)
1. Ensure **MTG Arena is running**.
2. Go to the **Decks** or **Collection** tab in-game, scroll for 30 secs through your collection (important so your collection loads into memory).
3. Run `MTGA_Exporter.exe`.
4. Follow the prompts to allow the tool do find and export your collection.

### Option 2: Run from Python Source
1. Install Python 3.x.
2. Run `install.bat` to install dependencies (`pymem`, `requests`).
3. Run `python mtg.py`.

## Troubleshooting
- If the tool cannot find your collection, ensure you have visited the Collection/Decks tab.
- Try providing different anchor cards if the first attempt fails (rarer anchor cards such as [O:legendary] work better, as they are more unique to your collection).
- Run as Administrator if you encounter permission errors.

## Files
- `MTGA_Exporter.exe`: The standalone application.
- `mtg.py`: The source code.
- `requirements.txt`: Python dependencies.
- `install.bat`: Setup script for Python users.
