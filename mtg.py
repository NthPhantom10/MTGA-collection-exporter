import json
import struct
import difflib
import requests
import pymem
import pymem.pattern
import sys
import csv
import sqlite3
import os
import subprocess
from pathlib import Path

# Configuration
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

LOOKUP_FILE = SCRIPT_DIR / "arena_id_lookup.json"
ANCHOR_FILE = SCRIPT_DIR / "last_anchors.json"
OUTPUT_JSON = SCRIPT_DIR / "mtga_collection.json"
OUTPUT_TXT = SCRIPT_DIR / "mtga_collection.txt"
OUTPUT_CSV = SCRIPT_DIR / "mtga_collection.csv"

import time
import sys
import threading

def print_progress(iteration, total, prefix='', suffix='', decimals=1, length=40, fill='█', printEnd="\r"):
    if total == 0: total = 1
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=printEnd)
    if iteration == total: 
        print()

def get_local_mtga_path():
    """Finds the MTGA Raw data folder."""
    base_paths = [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\MTGA\MTGA_Data\Downloads\Raw"),
        Path(r"C:\Program Files\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw"),
        Path(r"C:\Program Files (x86)\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw")
    ]
    for p in base_paths:
        if p.exists():
            return p
    return None

def load_local_mtga_database():
    """Scans local MTGA SQLite files for card definitions."""
    raw_path = get_local_mtga_path()
    if not raw_path:
        print("Local MTGA installation not found.")
        return {}
    
    print(f"Scanning local MTGA files in {raw_path}...")
    lookup = {}
    
    try:
        all_files = sorted(list(raw_path.glob("*.mtga")), key=lambda f: f.stat().st_size, reverse=True)
        total_files = len(all_files)
        
        print_progress(0, total_files, prefix='Local DB:', suffix='Complete', length=30)
        
        for i, f in enumerate(all_files):
            print_progress(i + 1, total_files, prefix='Local DB:', suffix=f'Checking {f.name[:10]}...', length=30)
            
            if f.stat().st_size < 500 * 1024: continue

            try:
                conn = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
                cursor = conn.cursor()
                
                tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                
                if "Cards" in tables and "Localizations" in tables:
                    loc_map = {}
                    try:
                        cursor.execute("SELECT Id, Text FROM Localizations WHERE Format LIKE '%en-US%' OR Format IS NULL")
                        for lid, text in cursor.fetchall():
                            if text: loc_map[lid] = text
                    except sqlite3.Error:
                        cursor.execute("SELECT Id, Text FROM Localizations")
                        for lid, text in cursor.fetchall():
                            if text: loc_map[lid] = text
                    
                    cols = [row[1] for row in cursor.execute("PRAGMA table_info(Cards)")]
                    has_set = "ExpansionCode" in cols
                    has_cn = "CollectorNumber" in cols
                    
                    # Columns: 0:GrpId, 1:TitleId, 2:ExpansionCode, 3:CollectorNumber
                    query = f"SELECT GrpId, TitleId, {'ExpansionCode' if has_set else 'NULL'}, {'CollectorNumber' if has_cn else 'NULL'} FROM Cards"
                    
                    cursor.execute(query)
                    rows = cursor.fetchall()
                    
                    for row in rows:
                        grp_id = row[0]
                        title_id = row[1]
                        set_code = row[2] if row[2] else ""
                        cn = str(row[3]) if row[3] else ""
                        
                        if title_id in loc_map:
                            lookup[grp_id] = {
                                "name": loc_map[title_id],
                                "set": set_code,
                                "collector_number": cn 
                            }
                    
                    if len(lookup) > 1000:
                        print_progress(total_files, total_files, prefix='Local DB:', suffix='Done', length=30)
                        print(f"  Loaded {len(lookup)} cards locally.")
                        conn.close()
                        return lookup
                        
                conn.close()
            except sqlite3.Error:
                continue
        
        print_progress(total_files, total_files, prefix='Local DB:', suffix='Done', length=30)
                
    except Exception as e:
        print(f"\nError scanning local files: {e}")
        
    return lookup

def fetch_scryfall_database():
    """Downloads card data from Scryfall API."""
    print("Fetching card data from Scryfall API...")
    try:
        bulk_meta = requests.get("https://api.scryfall.com/bulk-data/default-cards", timeout=30).json()
        cards_data = requests.get(bulk_meta["download_uri"], timeout=120).json()
        
        lookup = {}
        for c in cards_data:
            if c.get("arena_id"):
                lookup[c["arena_id"]] = {
                    "name": c.get("name", "Unknown"),
                    "set": c.get("set", "").upper(),
                    "collector_number": c.get("collector_number", "")
                }
        return lookup
    except Exception as e:
        print(f"Scryfall download failed: {e}")
        return {}

def load_card_database():
    """Orchestrates loading: Cache -> Local -> Scryfall."""
    if LOOKUP_FILE.exists():
        try:
            print("Loading cached database...")
            with LOOKUP_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            print("Cache corrupted.")

    lookup = load_local_mtga_database()
    
    if not lookup:
        print("\n[Warn] Local database not found. Downloading from Scryfall...")
        lookup = fetch_scryfall_database()
        
    if lookup:
        try:
            with LOOKUP_FILE.open("w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in lookup.items()}, f)
            print("Database cached.")
        except Exception: 
            pass
            
    return lookup

def get_user_anchors(name_to_id):
    """Interactive anchor setup with auto-save."""
    if ANCHOR_FILE.exists():
        try:
            with ANCHOR_FILE.open("r", encoding="utf-8") as f:
                saved = json.load(f)
                if saved and isinstance(saved, list):
                    print("\n[Previous Anchors Found]")
                    for i, (_, qty, name) in enumerate(saved, 1):
                        print(f"  {i}. {name} (x{qty})")
                    if input("  Use these? [Y/n]: ").lower() not in ('n', 'no'):
                        return saved
        except Exception: pass

    print("\n[Setup] Enter 5 unique owned cards (Rares/Mythics best) to calibrate scanner.")
    
    anchors = []
    while len(anchors) < 5:
        print(f"\nCard #{len(anchors) + 1} (Enter empty to finish):")
        name_input = input("  Name: ").strip()
        
        if not name_input:
            if anchors: break
            print("  Required.")
            continue

        search = name_input.lower()
        cid = name_to_id.get(search)
        
        if not cid:
            matches = difflib.get_close_matches(search, name_to_id.keys(), n=5, cutoff=0.5)
            if not matches:
                print("  Not found. Check spelling.")
                continue
                
            if len(matches) == 1:
                final_name = matches[0]
                print(f"  Assuming: {final_name.title()}")
            else:
                print("  Did you mean?")
                for i, m in enumerate(matches, 1): print(f"    {i}. {m.title()}")
                sel = input("  Select #: ")
                if not sel.isdigit() or not (1 <= int(sel) <= len(matches)): continue
                final_name = matches[int(sel)-1]
            
            cid = name_to_id[final_name]
            name_input = final_name.title()

        try:
            qty = int(input(f"  Quantity of '{name_input}': "))
            if qty < 1: raise ValueError
            anchors.append((cid, qty, name_input))
        except ValueError:
            print("  Invalid quantity.")
            continue

    if anchors:
        try:
            with ANCHOR_FILE.open("w", encoding="utf-8") as f:
                json.dump(anchors, f, indent=2)
        except Exception: pass
            
    return anchors

def find_blocks(pm, addr):
    """Reads memory around address to find card array."""
    try:
        data = pm.read_bytes(max(0, addr - 1024*1024), 4*1024*1024)
        ints = struct.unpack(f'<{len(data)//4}I', data)
        
        blocks = []
        for off in (0, 1):
            curr = {}
            misses = 0
            for i in range(off, len(ints)-1, 2):
                k, v = ints[i], ints[i+1]
                if 1000 <= k < 500000 and 1 <= v <= 400:
                    curr[k] = v
                    misses = 0
                else:
                    misses += 1
                
                if misses > 50:
                    if len(curr) > 50: blocks.append(curr)
                    curr = {}
                    misses = 0
            if len(curr) > 50: blocks.append(curr)
            
        return blocks
    except Exception: return []

def main():
    print(f"MTGA Collection Exporter | v2.0")
    print(f"Output Folder: {SCRIPT_DIR}\n")

    db = load_card_database()
    if not db:
        input("Database init failed. Press Enter to exit.")
        return

    print("Connecting to MTGA...")
    try:
        pm = pymem.Pymem("MTGA.exe")
        print(f"Connected (PID: {pm.process_id})")
    except Exception:
        print("MTG Arena not running. Open game and 'Decks' tab first.")
        input("Press Enter to exit.")
        return

    anchors = get_user_anchors({v["name"].lower(): k for k, v in db.items()})
    if not anchors: return

    print("\nScanning memory for collection data...")
    matches = []
    total_anchors = len(anchors)
    
    print_progress(0, total_anchors, prefix='Mem Scan:', suffix='Init...', length=25)
    
    for i, (aid, aqty, aname) in enumerate(anchors):
        display_name = (aname[:15] + '..') if len(aname) > 15 else aname
        print_progress(i, total_anchors, prefix='Mem Scan:', suffix=f'Find {display_name}', length=25)
        
        res = pm.pattern_scan_all(struct.pack('<II', aid, aqty), return_multiple=True)
        
        if res:
            matches.extend(res)
            print_progress(total_anchors, total_anchors, prefix='Mem Scan:', suffix='Found!', length=25)
            if aqty > 1: break 
            
        print_progress(i + 1, total_anchors, prefix='Mem Scan:', suffix='Done', length=25)

    if not matches:
        print("\nScanner failed to locate collection from anchors.")
        input("Press Enter to exit.")
        return

    candidates = []
    for m in matches:
        candidates.extend(find_blocks(pm, m))
    
    if not candidates:
        print("No valid data blocks found.")
        input("Press Enter to exit.")
        return

    collection = max(candidates, key=len)
    print(f"\n[Success] Found {len(collection)} unique entries.")

    processed = {}
    for cid, qty in collection.items():
        if info := db.get(cid):
            key = (info["name"], info["set"])
            if key not in processed:
                processed[key] = {
                    "count": 0,
                    "name": info["name"],
                    "set": info["set"],
                    "cn": info.get("collector_number", "")
                }
            processed[key]["count"] += qty

    final_list = sorted(processed.values(), key=lambda x: (x["name"], x["set"]))

    with OUTPUT_TXT.open("w", encoding="utf-8") as f:
        for i in final_list:
            set_str = f" ({i['set']})" if i['set'] else ""
            f.write(f"{i['count']} {i['name']}{set_str}\n")
    
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Count", "Name", "Edition", "Condition", "Language", "Foil", "Tag"])
        for i in final_list:
            writer.writerow([i["count"], i["name"], i["set"], "Near Mint", "English", "", ""])

    print(f"\nExport complete!")
    print(f"Files saved to: {SCRIPT_DIR}")
    
    try:
        subprocess.Popen(f'explorer /select,"{OUTPUT_TXT}"')
    except Exception: pass
    
    input("Press Enter to exit...")

if __name__ == "__main__":
    main()