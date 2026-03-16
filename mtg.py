import json
import struct
import difflib
import requests
import pymem
import pymem.pattern
import sys
from pathlib import Path

# Configuration
if getattr(sys, 'frozen', False):
    # If running as compiled exe, use the executable's directory
    SCRIPT_DIR = Path(sys.executable).parent
else:
    # If running as python script, use the script's directory
    SCRIPT_DIR = Path(__file__).resolve().parent

LOOKUP_FILE = SCRIPT_DIR / "arena_id_lookup.json"
ANCHOR_FILE = SCRIPT_DIR / "last_anchors.json"
OUTPUT_JSON = SCRIPT_DIR / "mtga_collection.json"
OUTPUT_TXT = SCRIPT_DIR / "mtga_collection.txt"

def load_card_database():
    """Loads card ID mapping from local cache or fetches from Scryfall."""
    if LOOKUP_FILE.exists():
        try:
            print("Loading local card database...")
            with LOOKUP_FILE.open("r", encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
        except (json.JSONDecodeError, ValueError):
            print("Cache corrupted, refreshing...")

    print("Fetching card data from Scryfall...")
    try:
        bulk_meta = requests.get("https://api.scryfall.com/bulk-data/default-cards", timeout=30).json()
        cards_data = requests.get(bulk_meta["download_uri"], timeout=120).json()

        lookup = {}
        for c in cards_data:
            if c.get("arena_id"):
                lookup[c["arena_id"]] = c.get("name", f"Unknown ({c['arena_id']})")

        with LOOKUP_FILE.open("w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in lookup.items()}, f)
            
        return lookup
    except Exception as e:
        print(f"Failed to download database: {e}")
        return {}

def get_user_anchors(name_to_id):
    """
    Asks the user for cards they own to find the collection in memory.
    Supports autocomplete/fuzzy matching and saving previous inputs.
    """
    if ANCHOR_FILE.exists():
        try:
            with ANCHOR_FILE.open("r", encoding="utf-8") as f:
                saved = json.load(f)
                if saved:
                    print("\n--- Last Used Anchors ---")
                    for i, (_, qty, name) in enumerate(saved, 1):
                        print(f"  {i}: {name} (Qty: {qty})")
                    
                    if input("Use these anchors? (Y/n): ").strip().lower() not in ('n', 'no'):
                        return saved
        except Exception:
            pass

    print("\n--- Collection Setup ---")
    print("Enter up to 5 unique cards (Rares/Mythics preferred) to locate your collection.")
    
    anchors = []
    while len(anchors) < 5:
        idx = len(anchors) + 1
        print(f"\nCard #{idx} (Press Enter to finish):")
        
        raw_name = input("  Name: ").strip()
        if not raw_name:
            if anchors: break
            print("  Please enter at least one card.")
            continue

        search = raw_name.lower()
        card_id = name_to_id.get(search)

        # Fuzzy matching if not found
        if not card_id:
            matches = [n for n in name_to_id if search in n]
            if not matches:
                matches = difflib.get_close_matches(search, name_to_id.keys(), n=5, cutoff=0.5)
            
            matches.sort(key=len)
            
            if not matches:
                print("  No match found.")
                continue
            
            final_name = None
            if len(matches) == 1:
                final_name = matches[0]
                print(f"  Assuming: {final_name.title()}")
            else:
                print(f"  Did you mean?")
                for i, m in enumerate(matches[:5], 1): 
                    print(f"    {i}: {m.title()}")
                
                sel = input("  Select #: ").strip()
                if sel.isdigit() and 1 <= int(sel) <= len(matches):
                     final_name = matches[int(sel)-1]
                else:
                    continue
            
            card_id = name_to_id[final_name]
            raw_name = final_name.title()

        try:
            qty_str = input(f"  Quantity of '{raw_name}': ").strip()
            if not qty_str: continue
            
            qty = int(qty_str)
            if qty < 1: raise ValueError
            
            anchors.append((card_id, qty, raw_name))
        except ValueError:
            print("  Invalid quantity.")
            continue

    if anchors:
        try:
            with ANCHOR_FILE.open("w", encoding="utf-8") as f:
                json.dump(anchors, f, indent=2)
        except Exception:
            pass
            
    return anchors

def find_candidate_blocks(pm, match_address):
    """Scans memory around a match for contiguous blocks of cards."""
    # Read 4MB chunk centered broadly around the match
    start = max(0, match_address - 1024 * 1024)
    try:
        data = pm.read_bytes(start, 4 * 1024 * 1024)
        ints = struct.unpack(f'<{len(data)//4}I', data)
        
        blocks = []
        # Scan both alignments (key/value pairs could be offset 0 or 1)
        for offset in (0, 1):
            current_block = {}
            misses = 0
            
            for i in range(offset, len(ints)-1, 2):
                cid, cqty = ints[i], ints[i+1]
                
                # Heuristic validation: IDs 1k-500k, Qty 1-400
                if 1000 <= cid < 500000 and 1 <= cqty <= 400:
                    current_block[cid] = cqty
                    misses = 0
                else:
                    misses += 1
                
                # Allow a small gap (e.g., hash table empty slots) before ending a run
                if misses > 50:
                    if len(current_block) > 50:
                        blocks.append(current_block)
                    current_block = {}
                    misses = 0
            
            if len(current_block) > 50:
                blocks.append(current_block)
        
        return blocks
            
    except Exception:
        return []

def main():
    lookup = load_card_database()
    if not lookup: return

    print("Attaching to MTGA.exe...")
    try:
        pm = pymem.Pymem("MTGA.exe")
        print(f"Attached to PID: {pm.process_id}")
    except Exception:
        print("MTG Arena not found. Please start the game.")
        return
    
    print("\n[TIP] Visit the 'Decks' or 'Collection' tab to ensure cards are loaded.")

    anchors = get_user_anchors({v.lower(): k for k, v in lookup.items()})
    if not anchors: return

    print(f"\nScanning memory...")

    matches = []
    for aid, aqty, aname in anchors:
        print(f"Scanning for {aname}...")
        results = pm.pattern_scan_all(struct.pack('<II', aid, aqty), return_multiple=True)
        if results:
            print(f"  Found {len(results)} matches.")
            matches.extend(results)
            if aqty > 1: break # Strong match found, stop looking
    
    if not matches:
        print("Collection not found. Try different cards.")
        return

    # Analyze found memory regions
    all_blocks = []
    for addr in matches:
        all_blocks.extend(find_candidate_blocks(pm, addr))

    if not all_blocks:
        print("No valid collection data found.")
        return

    # assume largest block is the collection
    collection = max(all_blocks, key=len)
    
    print(f"\nFound {len(collection)} unique cards.")
    
    export_list = []
    for cid, count in collection.items():
        if name := lookup.get(cid):
            export_list.append({"name": name, "id": cid, "count": count})
            
    export_list.sort(key=lambda x: x["name"])
    
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(export_list, f, indent=2, ensure_ascii=False)
        
    with OUTPUT_TXT.open("w", encoding="utf-8") as f:
        for item in export_list:
            f.write(f"{item['count']} {item['name']}\n")
            
    print(f"Exported to {OUTPUT_TXT.name}")
    print(f"Exported to {OUTPUT_JSON.name}")

if __name__ == "__main__":
    main()