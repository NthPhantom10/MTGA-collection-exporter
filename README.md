# Changelog - V1.2

- added priority for local card sql database, scryfall used as backup

- added progress bar for mem searching

- removed redundant comments on source code

- fixed issue where the .txt would list items multiple times

- added csv exports for Moxfield

- added card set identifiers to the .txt 

- and more small changes

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

### Option 1: Run the Executable (Simplest)
1. Navigate to **Releases**
2. Download and extract the **zip**
4. Navigate inside the extradted folder
5. Ensure **MTG Arena is running**.
6. Go to the **Decks** or **Collection** tab in-game, scroll for 30 secs through your collection (important so your collection loads into memory).
7. Run `MTGA_Exporter.exe`.
8. Follow the prompts to allow the tool do find and export your collection.

### Option 2: Run from Python Source
1. Download and extract zip
3. navigate inside folder
4. Install Python 3.x.
5. Run `install.bat` to install dependencies (`pymem`, `requests`).
6. Run `python mtg.py`.

## Troubleshooting
- If the tool cannot find your collection, ensure you have visited the Collection/Decks tab.
- Try providing different anchor cards if the first attempt fails (rarer anchor cards such as [O:legendary] work better, as they are more unique to your collection).
- Run as Administrator if you encounter permission errors.

## Files
- `MTGA_Exporter.exe`: The standalone application.
- `mtg.py`: The source code.
- `requirements.txt`: Python dependencies.
- `install.bat`: Setup script for Python users.
