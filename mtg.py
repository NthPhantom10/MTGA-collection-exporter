import json
import struct
import difflib
import requests
import sys
import csv
import sqlite3
import os
import subprocess
import ctypes
from pathlib import Path

if sys.platform != 'darwin':
    import pymem
    import pymem.pattern

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


class MacOSMem:
    """macOS process memory reader using Mach VM API (replaces pymem on macOS)."""

    _KERN_SUCCESS = 0
    _VM_REGION_BASIC_INFO_64 = 9
    _VM_REGION_BASIC_INFO_COUNT_64 = 9
    _VM_PROT_READ = 0x01

    class _RegionInfo(ctypes.Structure):
        _layout_ = 'ms'
        _pack_ = 4
        _fields_ = [
            ('protection', ctypes.c_int32),
            ('max_protection', ctypes.c_int32),
            ('inheritance', ctypes.c_uint32),
            ('shared', ctypes.c_uint32),
            ('reserved', ctypes.c_uint32),
            ('offset', ctypes.c_uint64),
            ('behavior', ctypes.c_int32),
            ('user_wired_count', ctypes.c_uint16),
            ('_pad', ctypes.c_uint16),
        ]

    def __init__(self, process_name):
        import psutil
        self._lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        self._setup_funcs()

        self.process_id = None
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] == process_name:
                self.process_id = proc.info['pid']
                break
        if not self.process_id:
            raise RuntimeError(f"Process not found: {process_name}")

        self_task = ctypes.c_uint.in_dll(self._lib, 'mach_task_self_').value
        self._task = ctypes.c_uint(0)
        kr = self._lib.task_for_pid(self_task, self.process_id, ctypes.byref(self._task))
        if kr != self._KERN_SUCCESS:
            raise PermissionError(
                f"task_for_pid failed (err={kr}). Try running with sudo."
            )

    def _setup_funcs(self):
        lib = self._lib
        lib.task_for_pid.restype = ctypes.c_int
        lib.task_for_pid.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        lib.mach_vm_read.restype = ctypes.c_int
        lib.mach_vm_read.argtypes = [
            ctypes.c_uint, ctypes.c_uint64, ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint),
        ]
        lib.mach_vm_region.restype = ctypes.c_int
        lib.mach_vm_region.argtypes = [
            ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ]

    def read_bytes(self, address, length):
        if address < 0 or length <= 0:
            return b''
        data_ptr = ctypes.c_uint64(0)
        data_cnt = ctypes.c_uint(0)
        kr = self._lib.mach_vm_read(
            self._task.value, address, length,
            ctypes.byref(data_ptr), ctypes.byref(data_cnt),
        )
        if kr != self._KERN_SUCCESS:
            raise OSError(f"mach_vm_read failed: {kr}")
        return ctypes.string_at(data_ptr.value, data_cnt.value)

    def _readable_regions(self):
        addr = ctypes.c_uint64(0)
        while True:
            size = ctypes.c_uint64(0)
            info = self._RegionInfo()
            cnt = ctypes.c_uint(self._VM_REGION_BASIC_INFO_COUNT_64)
            obj = ctypes.c_uint(0)
            kr = self._lib.mach_vm_region(
                self._task.value, ctypes.byref(addr), ctypes.byref(size),
                self._VM_REGION_BASIC_INFO_64, ctypes.byref(info),
                ctypes.byref(cnt), ctypes.byref(obj),
            )
            if kr != self._KERN_SUCCESS:
                break
            if info.protection & self._VM_PROT_READ:
                yield addr.value, size.value
            addr.value += size.value

    def pattern_scan_all(self, pattern, return_multiple=False):
        results = []
        for addr, size in self._readable_regions():
            if size > 256 * 1024 * 1024:
                continue
            try:
                data = self.read_bytes(addr, size)
            except OSError:
                continue
            offset = 0
            while True:
                idx = data.find(pattern, offset)
                if idx == -1:
                    break
                results.append(addr + idx)
                if not return_multiple:
                    return results
                offset = idx + 1
        return results


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

SCRYFALL_HEADERS = {"User-Agent": "MTGA-Collection-Exporter/2.0"}

def fetch_scryfall_database():
    """Downloads card data from Scryfall API."""
    print("Fetching card data from Scryfall API...")
    try:
        bulk_meta = requests.get("https://api.scryfall.com/bulk-data/default-cards", headers=SCRYFALL_HEADERS, timeout=30).json()
        cards_data = requests.get(bulk_meta["download_uri"], headers=SCRYFALL_HEADERS, timeout=120).json()
        
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
    print(f"MTGA Collection Exporter | v1.3")
    print(f"Output Folder: {SCRIPT_DIR}\n")

    db = load_card_database()
    if not db:
        input("Database init failed. Press Enter to exit.")
        return

    print("Connecting to MTGA...")
    try:
        if sys.platform == 'darwin':
            pm = MacOSMem("MTGA")
        else:
            pm = pymem.Pymem("MTGA.exe")
        print(f"Connected (PID: {pm.process_id})")
    except PermissionError as e:
        print(e)
        input("Press Enter to exit.")
        return
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
        if sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', str(OUTPUT_TXT)])
        else:
            subprocess.Popen(f'explorer /select,"{OUTPUT_TXT}"')
    except Exception: pass
    
    input("Press Enter to exit...")

if __name__ == "__main__":
    main()