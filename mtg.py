#!/usr/bin/env python3
"""
v3.3 Changelog:
- Fixed Scryfall API fetching (New format).
- Added full macOS support.
- Expanded MTGA install path detection (Epic Games, Mac, Linux, Steam VDF).
- Fixed "unbalanced parenthesis" memory scan crash on Windows.

Improvements from v3.2:
- Multi-stride memory scanning (8/12/16-byte)
- all anchors score candidate blocks
- conditional Scryfall requests 
- Export formats: Deckbox, MTGGoldfish, Cardsphere, JSON, TXT
- Collection statistics
- Proper logging to file & CLI argument support
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import logging
import os
import re
import struct
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import io
import gzip
import ctypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        from requests.packages.urllib3.util.retry import Retry
except ImportError:
    print("Error: 'requests' required.  pip install requests")
    sys.exit(1)

if sys.platform == 'darwin':
    try:
        import psutil
    except ImportError:
        print("Error: 'psutil' required on macOS.  pip install psutil")
        sys.exit(1)
else:
    try:
        import pymem
    except ImportError:
        print("Error: 'pymem' required on Windows.  pip install pymem")
        sys.exit(1)

def enable_windows_ansi() -> None:
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

if sys.platform == 'darwin':
    class MacOSMem:
        """macOS process memory reader using Mach VM API."""
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
                raise PermissionError(f"task_for_pid failed (err={kr}). Try running with sudo.")

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
            if address < 0 or length <= 0: return b''
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
                if kr != self._KERN_SUCCESS: break
                if info.protection & self._VM_PROT_READ:
                    yield addr.value, size.value
                addr.value += size.value

        def pattern_scan_all(self, pattern, return_multiple=False):
            results = []
            for addr, size in self._readable_regions():
                if size > 256 * 1024 * 1024: continue
                try:
                    data = self.read_bytes(addr, size)
                except OSError: continue
                offset = 0
                while True:
                    idx = data.find(pattern, offset)
                    if idx == -1: break
                    results.append(addr + idx)
                    if not return_multiple: return results
                    offset = idx + 1
            return results

@dataclass
class Config:
    output_dir: Path = Path(".")
    lookup_file: Path = Path("arena_id_lookup.json")
    cache_meta_file: Path = Path("arena_id_lookup.meta.json")
    anchor_file: Path = Path("last_anchors.json")
    output_json: Path = Path("mtga_collection.json")
    output_txt: Path = Path("mtga_collection.txt")
    output_csv_deckbox: Path = Path("mtga_collection_deckbox.csv")
    output_csv_goldfish: Path = Path("mtga_collection_goldfish.csv")
    output_csv_cardsphere: Path = Path("mtga_collection_cardsphere.csv")
    output_stats: Path = Path("mtga_collection_stats.txt")
    include_descriptions: Optional[bool] = None
    force_refresh: bool = False
    auto_open_explorer: bool = True
    verbose: bool = False
    scan_range_mb: int = 8
    min_arena_id: int = 1000
    max_arena_id: int = 900_000
    min_qty: int = 1
    max_qty: int = 400
    min_block_size: int = 50
    max_gap: int = 64
    cache_max_age_days: int = 7

    @classmethod
    def from_base_dir(cls, base: Path) -> "Config":
        c = cls(output_dir=base)
        for attr in ("lookup_file", "cache_meta_file", "anchor_file",
                     "output_json", "output_txt", "output_csv_deckbox",
                     "output_csv_goldfish", "output_csv_cardsphere",
                     "output_stats"):
            setattr(c, attr, base / getattr(c, attr).name)
        return c

def setup_logging(cfg: Config) -> None:
    log_path = cfg.output_dir / "mtga_exporter.log"
    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info("Log file: %s", log_path)

class ProgressBar:
    def __init__(self, total: int, prefix: str = "", length: int = 30):
        self.total = max(total, 1)
        self.prefix = prefix
        self.length = length
        self._t0 = time.time()

    def update(self, iteration: int, suffix: str = "") -> None:
        pct = 100 * iteration / self.total
        filled = int(self.length * iteration / self.total)
        bar = "█" * filled + "─" * (self.length - filled)
        elapsed = time.time() - self._t0
        eta = ""
        if iteration > 0 and elapsed > 0:
            remaining = (self.total - iteration) / (iteration / elapsed)
            eta = f" ({remaining:.0f}s left)"
        print(f"\r{self.prefix} |{bar}| {pct:5.1f}% {suffix}{eta}", end="")

    def finish(self, suffix: str = "Done") -> None:
        self.update(self.total, suffix)
        print()

class ScanProgressBar:
    def __init__(self, prefix: str = "", length: int = 30):
        self.prefix = prefix
        self.length = length
        self._anim_idx = 0
        self._rendered = False

    def update(self, status: str) -> None:
        pos = self._anim_idx % (self.length * 2)
        if pos >= self.length:
            pos = (self.length * 2) - pos - 1
        bar = "[" + " " * pos + "█" + " " * (self.length - pos - 1) + "]"
        line1 = f"{self.prefix} {bar}"
        line2 = f"  ↳ {status}"
        if self._rendered:
            sys.stdout.write("\033[2F")
        sys.stdout.write("\033[K" + line1 + "\n")
        sys.stdout.write("\033[K" + line2 + "\n")
        sys.stdout.flush()
        self._rendered = True
        self._anim_idx += 1

    def finish(self, final_status: str = "Done") -> None:
        if not self._rendered:
            print(f"{self.prefix} {final_status}")
            return
        bar = "[" + "=" * self.length + "]"
        line1 = f"{self.prefix} {bar}"
        line2 = f"  ↳ {final_status}"
        sys.stdout.write("\033[2F")
        sys.stdout.write("\033[K" + line1 + "\n")
        sys.stdout.write("\033[K" + line2 + "\n")
        sys.stdout.flush()
        print()

@dataclass
class CardInfo:
    arena_id: int
    name: str
    set: str = ""
    collector_number: str = ""
    rarity: str = ""
    desc: str = ""
    mana_cost: str = ""
    type_line: str = ""
    release_date: str = ""

class DatabaseLoader:
    SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data/default-cards"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = logging.getLogger(self.__class__.__name__)

    def load(self) -> Dict[int, CardInfo]:
        if not self.cfg.force_refresh:
            cached = self._load_cache()
            if cached:
                self.log.info("Loaded %d cards from cache", len(cached))
                return cached

        lookup = self._load_local_mtga()
        if not lookup or len(lookup) < 1000:
            self.log.info("Local DB insufficient — fetching Scryfall…")
            sf = self._fetch_scryfall()
            if sf:
                lookup.update(sf)
            elif lookup:
                self.log.warning("Scryfall failed — using local data only")
            else:
                self.log.error("All database sources failed")

        if lookup:
            self._save_cache(lookup)
            self.log.info("Database ready: %d cards", len(lookup))
        return lookup

    def _load_cache(self) -> Optional[Dict[int, CardInfo]]:
        if not self.cfg.lookup_file.exists(): return None
        age_s = time.time() - self.cfg.lookup_file.stat().st_mtime
        if age_s > self.cfg.cache_max_age_days * 86_400:
            self.log.info("Cache is %.1f days old — refreshing", age_s / 86_400)
            return None
        try:
            with self.cfg.lookup_file.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            self.log.warning("Cache read error: %s", exc)
            return None

        result: Dict[int, CardInfo] = {}
        for k, v in raw.items():
            if not isinstance(v, dict): continue
            try:
                result[int(k)] = CardInfo(
                    arena_id=int(k), name=v.get("name", "Unknown"), set=v.get("set", ""),
                    collector_number=v.get("collector_number", ""), rarity=v.get("rarity", ""),
                    desc=v.get("desc", ""), mana_cost=v.get("mana_cost", ""),
                    type_line=v.get("type_line", ""), release_date=v.get("release_date", ""),
                )
            except (ValueError, TypeError): continue
        return result if len(result) > 100 else None

    def _save_cache(self, lookup: Dict[int, CardInfo]) -> None:
        try:
            data = {
                str(k): {
                    "name": v.name, "set": v.set, "collector_number": v.collector_number,
                    "rarity": v.rarity, "desc": v.desc, "mana_cost": v.mana_cost,
                    "type_line": v.type_line, "release_date": v.release_date,
                } for k, v in lookup.items()
            }
            with self.cfg.lookup_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            self.log.info("Cache saved → %s", self.cfg.lookup_file)
        except Exception as exc:
            self.log.warning("Cache save failed: %s", exc)

    def _find_mtga_raw_path(self) -> Optional[Path]:
        paths_to_check = []
        if sys.platform == 'win32':
            paths_to_check.extend([
                Path(r"C:\Program Files (x86)\Steam\steamapps\common\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"C:\Program Files\Steam\steamapps\common\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"C:\Program Files\Epic Games\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"C:\Program Files (x86)\Epic Games\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"C:\Program Files\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"C:\Program Files (x86)\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"D:\Steam\steamapps\common\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"D:\Games\MTGA\MTGA_Data\Downloads\Raw"),
                Path(r"E:\Steam\steamapps\common\MTGA\MTGA_Data\Downloads\Raw"),
            ])
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")
                steam_path, _ = winreg.QueryValueEx(key, "InstallPath")
                paths_to_check.append(Path(steam_path) / "steamapps" / "common" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw")
            except Exception: pass
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", 0, winreg.KEY_READ)
                steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
                winreg.CloseKey(key)
                vdf = Path(steam_path) / "steamapps" / "libraryfolders.vdf"
                if vdf.exists():
                    text = vdf.read_text(encoding="utf-8", errors="ignore")
                    for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                        lib = m.group(1).replace("\\\\", "\\")
                        paths_to_check.append(Path(lib) / "steamapps" / "common" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw")
            except Exception: pass
        elif sys.platform == 'darwin':
            paths_to_check.extend([
                Path.home() / "Library/Application Support/Steam/steamapps/common/MTGA/MTGA_Data/Downloads/Raw",
                Path.home() / "Library/Application Support/com.wizards.mtga/Downloads/RAW",
                Path.home() / "Library/Application Support/com.wizards.mtga/Downloads/Raw",
                Path.home() / "Library/Application Support/Epic/MTGA/MTGA_Data/Downloads/Raw",
            ])
        elif sys.platform.startswith('linux'):
            paths_to_check.extend([
                Path.home() / ".steam/steam/steamapps/common/MTGA/MTGA_Data/Downloads/Raw",
                Path.home() / ".local/share/Steam/steamapps/common/MTGA/MTGA_Data/Downloads/Raw",
                Path.home() / ".var/app/com.valvesoftware.Steam/.steam/steam/steamapps/common/MTGA/MTGA_Data/Downloads/Raw",
            ])

        for p in paths_to_check:
            if p.exists(): return p
        return None

    def _load_local_mtga(self) -> Dict[int, CardInfo]:
        raw_path = self._find_mtga_raw_path()
        if not raw_path:
            self.log.info("Local MTGA installation not found")
            return {}
        self.log.info("Scanning %s", raw_path)
        lookup: Dict[int, CardInfo] = {}
        files = sorted(raw_path.glob("*.mtga"), key=lambda f: f.stat().st_size, reverse=True)
        if not files:
            self.log.warning("No .mtga files in %s", raw_path)
            return {}

        bar = ProgressBar(len(files), prefix="Local DB:")
        for i, f in enumerate(files):
            bar.update(i, f.name[:20])
            if f.stat().st_size < 500 * 1024: continue
            cards = self._parse_sqlite(f)
            if cards: lookup.update(cards)
        bar.finish(f"{len(lookup)} cards loaded")
        return lookup

    def _parse_sqlite(self, path: Path) -> Dict[int, CardInfo]:
        result: Dict[int, CardInfo] = {}
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            cur = conn.cursor()
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "Cards" not in tables:
                conn.close()
                return result
            loc = self._load_localizations(cur, tables)
            if not loc:
                conn.close()
                return result
            cols = {r[1] for r in cur.execute("PRAGMA table_info(Cards)")}
            q = (
                "SELECT GrpId, TitleId, "
                f"{'ExpansionCode' if 'ExpansionCode' in cols else 'NULL'}, "
                f"{'CollectorNumber' if 'CollectorNumber' in cols else 'NULL'}, "
                f"{'AbilityIds' if 'AbilityIds' in cols else 'NULL'}, "
                f"{'Rarity' if 'Rarity' in cols else 'NULL'} "
                "FROM Cards"
            )
            for row in cur.execute(q):
                grp, title, set_code, cn, abil, rarity = row
                name = loc.get(title, f"Unknown_{grp}")
                desc = self._build_desc(str(abil) if abil else "", loc)
                result[grp] = CardInfo(
                    arena_id=grp, name=name, set=str(set_code) if set_code else "",
                    collector_number=str(cn) if cn else "", rarity=str(rarity) if rarity else "", desc=desc,
                )
            conn.close()
        except sqlite3.Error as exc:
            self.log.debug("SQLite error in %s: %s", path.name, exc)
        except Exception as exc:
            self.log.debug("Parse error in %s: %s", path.name, exc)
        return result

    @staticmethod
    def _load_localizations(cur: sqlite3.Cursor, tables: Set[str]) -> Dict[int, str]:
        loc: Dict[int, str] = {}
        if "Localizations_enUS" in tables:
            try:
                for lid, text in cur.execute("SELECT LocId, Loc FROM Localizations_enUS"):
                    if text: loc[lid] = text
            except sqlite3.Error: pass
            if loc: return loc
        if "Localizations" in tables:
            try:
                cols = {r[1] for r in cur.execute("PRAGMA table_info(Localizations)")}
                if {"Id", "Text"} <= cols:
                    q = "SELECT Id, Text FROM Localizations WHERE Format LIKE '%en-US%' OR Format IS NULL"
                    for lid, text in cur.execute(q):
                        if text: loc[lid] = text
                elif {"LocId", "Loc"} <= cols:
                    for lid, text in cur.execute("SELECT LocId, Loc FROM Localizations"):
                        if text: loc[lid] = text
            except sqlite3.Error: pass
        return loc

    @staticmethod
    def _build_desc(ability_ids: str, loc: Dict[int, str]) -> str:
        if not ability_ids: return ""
        parts: List[str] = []
        for chunk in ability_ids.split(","):
            if ":" in chunk:
                try:
                    tid = int(chunk.split(":")[-1])
                    if tid in loc: parts.append(loc[tid])
                except ValueError: continue
        return "\n".join(parts) if parts else ""

    def _fetch_scryfall(self) -> Dict[int, CardInfo]:
        self.log.info("Fetching Scryfall bulk data…")
        try:
            session = self._http_session()
            meta = session.get(self.SCRYFALL_BULK_URL, timeout=30)
            meta.raise_for_status()
            bulk = meta.json()
            self.log.info("Scryfall bulk updated: %s", bulk.get("updated_at", "?"))
            
            jsonl_uri = bulk.get("jsonl_download_uri")
            download_uri = bulk.get("download_uri")
            uri = jsonl_uri or download_uri
            
            if not uri:
                raise ValueError("No download URI found in bulk metadata")

            headers: Dict[str, str] = {}
            etag = self._load_etag()
            if etag: headers["If-None-Match"] = etag
            
            resp = session.get(uri, timeout=300, headers=headers)
            if resp.status_code == 304:
                self.log.info("Scryfall unchanged (304) — using cache")
                return self._load_cache() or {}
            resp.raise_for_status()
            
            new_etag = resp.headers.get("ETag")
            if new_etag: self._save_etag(new_etag)
            
            lookup: Dict[int, CardInfo] = {}
            
            if jsonl_uri:
                self.log.info("Decompressing Scryfall JSONL...")
                with gzip.open(io.BytesIO(resp.content), 'rt', encoding='utf-8') as f:
                    for line in f:
                        if not line.strip(): continue
                        c = json.loads(line)
                        aid = c.get("arena_id")
                        if not aid: continue
                        lookup[aid] = CardInfo(
                            arena_id=aid, name=c.get("name", "Unknown"), set=c.get("set", "").upper(),
                            collector_number=c.get("collector_number", ""), rarity=c.get("rarity", ""),
                            desc=c.get("oracle_text", ""), mana_cost=c.get("mana_cost", ""),
                            type_line=c.get("type_line", ""), release_date=c.get("released_at", ""),
                        )
            else:
                for c in resp.json():
                    aid = c.get("arena_id")
                    if not aid: continue
                    lookup[aid] = CardInfo(
                        arena_id=aid, name=c.get("name", "Unknown"), set=c.get("set", "").upper(),
                        collector_number=c.get("collector_number", ""), rarity=c.get("rarity", ""),
                        desc=c.get("oracle_text", ""), mana_cost=c.get("mana_cost", ""),
                        type_line=c.get("type_line", ""), release_date=c.get("released_at", ""),
                    )
                    
            self.log.info("Scryfall: %d cards with arena_id", len(lookup))
            return lookup
        except Exception as exc:
            self.log.error("Scryfall fetch failed: %s", exc)
            return {}

    @staticmethod
    def _http_session() -> requests.Session:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers["User-Agent"] = "MTGA-Collection-Exporter/3.3"
        return s

    def _load_etag(self) -> Optional[str]:
        try:
            if self.cfg.cache_meta_file.exists():
                with self.cfg.cache_meta_file.open("r") as f:
                    return json.load(f).get("scryfall_etag")
        except Exception: return None
        return None

    def _save_etag(self, etag: str) -> None:
        try:
            with self.cfg.cache_meta_file.open("w") as f:
                json.dump({"scryfall_etag": etag, "saved_at": datetime.now().isoformat()}, f)
        except Exception: pass

@dataclass
class Anchor:
    arena_id: int
    quantity: int
    name: str

class AnchorManager:
    MIN_ANCHORS = 3
    RECOMMENDED = 5
    MAX_ANCHORS = 10

    def __init__(self, cfg: Config, db: Dict[int, CardInfo]):
        self.cfg = cfg
        self.db = db
        self.log = logging.getLogger(self.__class__.__name__)
        self._name_index: Dict[str, List[int]] = {}
        self._build_index()

    def _build_index(self) -> None:
        for aid, info in self.db.items():
            key = info.name.lower()
            self._name_index.setdefault(key, []).append(aid)

    def get_anchors(self) -> List[Anchor]:
        saved = self._load_saved()
        if saved and self._confirm(saved): return saved
        return self._interactive()

    def _load_saved(self) -> Optional[List[Anchor]]:
        if not self.cfg.anchor_file.exists(): return None
        try:
            with self.cfg.anchor_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list): return None
            anchors = [Anchor(a[0], a[1], a[2]) for a in data if isinstance(a, (list, tuple)) and len(a) >= 3]
            return anchors or None
        except Exception: return None

    def _confirm(self, anchors: List[Anchor]) -> bool:
        print("\n[Previous Anchors]")
        for i, a in enumerate(anchors, 1):
            print(f"  {i}. {a.name}  (x{a.quantity})")
        return input("\nUse these? [Y/n]: ").strip().lower() not in ("n", "no")

    def _interactive(self) -> List[Anchor]:
        print(f"\n[Setup] Enter up to {self.RECOMMENDED} cards you own.")
        print("  Rares/Mythics with exact quantities work best.")
        print("  Enter an empty name to finish early (min 3).\n")
        anchors: List[Anchor] = []
        used: Set[int] = set()
        while len(anchors) < self.MAX_ANCHORS:
            print(f"Card #{len(anchors) + 1}:")
            name_in = input("  Name: ").strip()
            if not name_in:
                if len(anchors) >= self.MIN_ANCHORS: break
                print(f"  Need at least {self.MIN_ANCHORS} anchors.")
                continue
            anchor = self._find_card(name_in, used)
            if anchor is None: continue
            try:
                qty = int(input(f"  Quantity of '{anchor.name}': "))
                if not 1 <= qty <= 400:
                    print("  Quantity must be 1–400.")
                    continue
            except ValueError:
                print("  Invalid number.")
                continue
            anchor.quantity = qty
            used.add(anchor.arena_id)
            anchors.append(anchor)
            print(f"  ✓ {anchor.name}  (ID {anchor.arena_id}, x{qty})\n")
        if anchors: self._save(anchors)
        return anchors

    def _find_card(self, query: str, used: Set[int]) -> Optional[Anchor]:
        q = query.lower()
        if q in self._name_index:
            for aid in self._name_index[q]:
                if aid not in used: return Anchor(aid, 0, self.db[aid].name)
        matches = difflib.get_close_matches(q, self._name_index.keys(), n=5, cutoff=0.5)
        if not matches:
            print("  ✗ Not found. Check spelling or refresh the database.")
            return None
        if len(matches) == 1:
            name = matches[0]
            print(f"  → {name}")
        else:
            print("  Did you mean?")
            for i, m in enumerate(matches, 1): print(f"    {i}. {m}")
            sel = input("  Select # (Enter to cancel): ").strip()
            if not sel.isdigit() or not (1 <= int(sel) <= len(matches)): return None
            name = matches[int(sel) - 1]
        for aid in self._name_index[name]:
            if aid not in used: return Anchor(aid, 0, self.db[aid].name)
        print("  ✗ Already added. Try a different card.")
        return None

    def _save(self, anchors: List[Anchor]) -> None:
        try:
            data = [[a.arena_id, a.quantity, a.name] for a in anchors]
            with self.cfg.anchor_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            self.log.warning("Anchor save failed: %s", exc)

class MemoryScanner:
    STRIDES_WORDS = (2, 3, 4)

    def __init__(self, cfg: Config, db: Dict[int, CardInfo]):
        self.cfg = cfg
        self.db = db
        self.known_ids: Set[int] = set(db.keys())
        self.log = logging.getLogger(self.__class__.__name__)
        self.pm: Optional[Any] = None

    def connect(self) -> bool:
        try:
            if sys.platform == 'darwin':
                self.pm = MacOSMem("MTGA")
            else:
                self.pm = pymem.Pymem("MTGA.exe")
            self.log.info("Connected to MTGA  PID=%d", self.pm.process_id)
            return True
        except Exception as exc:
            self.log.error("Cannot attach to MTGA: %s", exc)
            print("\n✗ MTG Arena is not running or cannot be accessed.")
            print("  1. Launch the game.")
            print("  2. Navigate to the **Decks** tab so your collection")
            print("     is loaded into memory.")
            print("  3. Run this tool again. (Mac users may need sudo)")
            return False

    def _run_with_ui(self, func, ui, status, *args):
        root = logging.getLogger()
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
        for h in stream_handlers: h.setLevel(logging.CRITICAL)
        result = []
        def target(): result.append(func(*args))
        t = threading.Thread(target=target)
        t.start()
        while t.is_alive():
            ui.update(status)
            time.sleep(0.08)
        t.join()
        for h in stream_handlers: h.setLevel(logging.DEBUG if self.cfg.verbose else logging.INFO)
        return result[0] if result else None

    def find_collection(self, anchors: List[Anchor]) -> Optional[Dict[int, int]]:
        if not self.pm or not anchors: return None
        ordered = sorted(anchors, key=lambda a: -a.quantity)
        total_anchors = len(ordered)
        ui = ScanProgressBar(f"Memory Scan (0/{total_anchors}):")
        for idx, primary in enumerate(ordered):
            ui.prefix = f"Memory Scan ({idx+1}/{total_anchors}):"
            self.log.info("Anchor %d/%d: %s (ID %d x%d)", idx + 1, total_anchors, primary.name, primary.arena_id, primary.quantity)
            addrs = self._run_with_ui(self._scan_anchor, ui, f"Searching for pattern: '{primary.name}' (x{primary.quantity})...", primary)
            if not addrs:
                ui.update(f"Anchor '{primary.name}' yielded no matches.")
                time.sleep(1.0)
                continue
            self.log.info("  %d pattern hit(s)", len(addrs))
            candidates = self._run_with_ui(self._find_blocks, ui, f"Found {len(addrs)} hits. Reading memory & extracting blocks...", addrs)
            if not candidates:
                ui.update(f"No valid data blocks found near hits.")
                time.sleep(1.0)
                continue
            ui.update(f"Scoring {len(candidates)} candidate blocks...")
            time.sleep(0.5)
            best = self._select_best(candidates, anchors)
            if best and self._validate(best):
                known = sum(1 for k in best if k in self.known_ids)
                pct = 100 * known / len(best)
                ui.finish(f"Success! Found collection block with {len(best)} entries.")
                self.log.info("Collection found: %d entries (%d known, %.1f%%)", len(best), known, pct)
                return best
            ui.update(f"Best block failed validation. Trying next anchor...")
            time.sleep(1.0)
        ui.finish("All anchors exhausted without finding a valid collection.")
        return None

    def _scan_anchor(self, anchor: Anchor) -> List[int]:
        pattern = struct.pack("<II", anchor.arena_id, anchor.quantity)
        if sys.platform != 'darwin':
            pattern = re.escape(pattern)
        try:
            return self.pm.pattern_scan_all(pattern, return_multiple=True)
        except Exception as exc:
            self.log.warning("  Scan error: %s", exc)
            return []

    def _find_blocks(self, addresses: List[int]) -> List[Dict[int, int]]:
        scan_bytes = self.cfg.scan_range_mb * 1024 * 1024
        addresses = sorted(set(addresses))
        filtered: List[int] = []
        for a in addresses:
            if not filtered or a - filtered[-1] > 1024 * 1024: filtered.append(a)
        blocks: List[Dict[int, int]] = []
        for addr in filtered: blocks.extend(self._scan_region(addr, scan_bytes))
        return blocks

    def _scan_region(self, addr: int, size: int) -> List[Dict[int, int]]:
        start = max(0, addr - size // 2)
        try: data = self.pm.read_bytes(start, size)
        except Exception as exc:
            self.log.debug("Read failed @ %#x: %s", start, exc)
            return []
        results: List[Dict[int, int]] = []
        for stride_w in self.STRIDES_WORDS:
            for off_w in range(stride_w): results.extend(self._extract(data, stride_w, off_w))
        return results

    def _extract(self, data: bytes, stride_w: int, off_w: int) -> List[Dict[int, int]]:
        n_ints = len(data) // 4
        if n_ints < 2: return []
        try: ints = struct.unpack_from(f"<{n_ints}I", data)
        except struct.error: return []
        blocks: List[Dict[int, int]] = []
        current: Dict[int, int] = {}
        misses = 0
        i = off_w
        while i + 1 < n_ints:
            k, v = ints[i], ints[i + 1]
            if (self.cfg.min_arena_id <= k < self.cfg.max_arena_id and self.cfg.min_qty <= v <= self.cfg.max_qty):
                current[k] = current.get(k, 0) + v
                misses = 0
            else:
                misses += 1
                if misses > self.cfg.max_gap:
                    if len(current) >= self.cfg.min_block_size: blocks.append(current)
                    current = {}
                    misses = 0
            i += stride_w
        if len(current) >= self.cfg.min_block_size: blocks.append(current)
        return blocks

    def _select_best(self, candidates: List[Dict[int, int]], anchors: List[Anchor]) -> Optional[Dict[int, int]]:
        if not candidates: return None
        anchor_pairs = {(a.arena_id, a.quantity) for a in anchors}
        anchor_ids = {a.arena_id for a in anchors}
        n_anchors = len(anchors)
        scored: List[Tuple[Dict[int, int], float, int, int]] = []
        for blk in candidates:
            if not blk: continue
            known = sum(1 for k in blk if k in self.known_ids)
            known_ratio = known / len(blk)
            anchors_exact = sum(1 for k, v in blk.items() if (k, v) in anchor_pairs)
            anchors_id = sum(1 for k in blk if k in anchor_ids)
            size_score = min(len(blk) / 5000, 1.0)
            score = (known_ratio * 0.40 + (anchors_exact / n_anchors) * 0.30 + (anchors_id / n_anchors) * 0.15 + size_score * 0.15)
            scored.append((blk, score, anchors_exact, known))
        scored.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)
        best = scored[0]
        self.log.info("  Best block: %d entries, score %.3f, anchors %d/%d, known %d", len(best[0]), best[1], best[2], n_anchors, best[3])
        return best[0]

    def _validate(self, block: Dict[int, int]) -> bool:
        if not block: return False
        if len(block) < 10: return False
        if len(block) > 100_000: return False
        known = sum(1 for k in block if k in self.known_ids)
        ratio = known / len(block)
        if ratio < 0.30:
            self.log.warning("  Low known-ratio: %.1f%%", ratio * 100)
            return False
        total = sum(block.values())
        if total > 500_000:
            self.log.warning("  Total quantity too high: %d", total)
            return False
        return True

@dataclass
class CollectionEntry:
    count: int
    name: str
    set: str
    collector_number: str = ""
    rarity: str = ""
    desc: str = ""
    mana_cost: str = ""
    type_line: str = ""
    arena_ids: List[int] = field(default_factory=list)

class CollectionWriter:
    def __init__(self, cfg: Config, db: Dict[int, CardInfo]):
        self.cfg = cfg
        self.db = db
        self.log = logging.getLogger(self.__class__.__name__)

    def write_all(self, collection: Dict[int, int], include_desc: bool = False) -> List[Path]:
        entries = self._aggregate(collection)
        files: List[Path] = []
        for writer, args in (
            (self._write_json, (entries,)), (self._write_txt, (entries, include_desc)),
            (self._write_deckbox, (entries,)), (self._write_goldfish, (entries,)),
            (self._write_cardsphere, (entries,)), (self._write_stats, (entries,)),
        ):
            path = writer(*args)
            if path: files.append(path)
        return files

    def _aggregate(self, collection: Dict[int, int]) -> List[CollectionEntry]:
        agg: Dict[Tuple[str, str], CollectionEntry] = {}
        for cid, qty in collection.items():
            info = self.db.get(cid)
            if not info: continue
            key = (info.name, info.set)
            if key not in agg:
                agg[key] = CollectionEntry(
                    count=0, name=info.name, set=info.set, collector_number=info.collector_number,
                    rarity=info.rarity, desc=info.desc, mana_cost=info.mana_cost, type_line=info.type_line, arena_ids=[],
                )
            agg[key].count += qty
            if cid not in agg[key].arena_ids: agg[key].arena_ids.append(cid)
        return sorted(agg.values(), key=lambda e: (e.name, e.set))

    def _write_json(self, entries: List[CollectionEntry]) -> Optional[Path]:
        try:
            data = {
                "export_date": datetime.now().isoformat(), "total_unique": len(entries),
                "total_cards": sum(e.count for e in entries), "database_size": len(self.db),
                "cards": [{
                    "count": e.count, "name": e.name, "set": e.set, "collector_number": e.collector_number,
                    "rarity": e.rarity, "mana_cost": e.mana_cost, "type_line": e.type_line,
                    "oracle_text": e.desc, "arena_ids": e.arena_ids,
                } for e in entries],
            }
            with self.cfg.output_json.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return self.cfg.output_json
        except Exception as exc:
            self.log.error("JSON write failed: %s", exc)
            return None

    def _write_txt(self, entries: List[CollectionEntry], include_desc: bool) -> Optional[Path]:
        try:
            with self.cfg.output_txt.open("w", encoding="utf-8") as f:
                total = sum(e.count for e in entries)
                f.write("MTGA Collection Export\n")
                f.write(f"Exported: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write(f"Unique cards: {len(entries)}\n")
                f.write(f"Total cards:   {total}\n")
                f.write("=" * 60 + "\n")
                for e in entries:
                    parts = [f"{e.count}x {e.name}"]
                    if e.set: parts.append(f"({e.set})")
                    if e.collector_number: parts.append(f"#{e.collector_number}")
                    if e.rarity: parts.append(f"[{e.rarity}]")
                    f.write(" ".join(parts) + "\n")
                    if include_desc:
                        context_parts = []
                        if e.type_line: context_parts.append(e.type_line)
                        if e.mana_cost: context_parts.append(e.mana_cost)
                        if context_parts:
                            context_str = " | ".join(context_parts)
                            f.write(textwrap.fill(context_str, width=76, initial_indent="    ", subsequent_indent="    ") + "\n")
                        if e.desc:
                            wrapped_desc = textwrap.fill(e.desc, width=76, initial_indent="    ", subsequent_indent="    ", replace_whitespace=False)
                            f.write(wrapped_desc + "\n")
                    f.write("\n")
            self.log.info("TXT  → %s", self.cfg.output_txt)
            return self.cfg.output_txt
        except Exception as exc:
            self.log.error("TXT write failed: %s", exc)
            return None

    def _write_deckbox(self, entries: List[CollectionEntry]) -> Optional[Path]:
        try:
            with self.cfg.output_csv_deckbox.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Count", "Tradelist Count", "Name", "Edition", "Card Number", "Condition", "Language", "Foil", "Signed", "Artist Proof", "Altered Art", "Misprint", "Promo", "Textless", "My Price"])
                for e in entries: w.writerow([e.count, 0, e.name, e.set, e.collector_number, "Near Mint", "English", "", "", "", "", "", "", "", ""])
            return self.cfg.output_csv_deckbox
        except Exception as exc:
            self.log.error("Deckbox CSV failed: %s", exc)
            return None

    def _write_goldfish(self, entries: List[CollectionEntry]) -> Optional[Path]:
        try:
            with self.cfg.output_csv_goldfish.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Card", "Set", "Quantity"])
                for e in entries: w.writerow([e.name, e.set, e.count])
            return self.cfg.output_csv_goldfish
        except Exception as exc:
            self.log.error("Goldfish CSV failed: %s", exc)
            return None

    def _write_cardsphere(self, entries: List[CollectionEntry]) -> Optional[Path]:
        try:
            with self.cfg.output_csv_cardsphere.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Quantity", "Card Name", "Set", "Foil"])
                for e in entries: w.writerow([e.count, e.name, e.set, "No"])
            return self.cfg.output_csv_cardsphere
        except Exception as exc:
            self.log.error("Cardsphere CSV failed: %s", exc)
            return None

    def _write_stats(self, entries: List[CollectionEntry]) -> Optional[Path]:
        try:
            by_set: Dict[str, Tuple[int, int]] = {}
            by_rarity: Dict[str, Tuple[int, int]] = {}
            for e in entries:
                s = e.set or "Unknown"
                u, t = by_set.get(s, (0, 0))
                by_set[s] = (u + 1, t + e.count)
                r = e.rarity or "Unknown"
                u, t = by_rarity.get(r, (0, 0))
                by_rarity[r] = (u + 1, t + e.count)
            total_unique = len(entries)
            total_cards = sum(e.count for e in entries)
            avg = total_cards / total_unique if total_unique else 0
            with self.cfg.output_stats.open("w", encoding="utf-8") as f:
                f.write("MTGA Collection Statistics\n")
                f.write(f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write("=" * 50 + "\n")
                f.write(f"Total unique cards:  {total_unique}\n")
                f.write(f"Total cards (qty):   {total_cards}\n")
                f.write(f"Average copies/card: {avg:.1f}\n")
                f.write(f"Database size:       {len(self.db)}\n")
                f.write("\n" + "-" * 50 + "\nBy Rarity:\n" + "-" * 50 + "\n")
                for r in sorted(by_rarity):
                    u, t = by_rarity[r]
                    f.write(f"  {r:15s}  {u:5d} unique  {t:6d} total\n")
                f.write("\n" + "-" * 50 + "\nBy Set:\n" + "-" * 50 + "\n")
                for s in sorted(by_set):
                    u, t = by_set[s]
                    f.write(f"  {s:10s}  {u:5d} unique  {t:6d} total\n")
            return self.cfg.output_stats
        except Exception as exc:
            self.log.error("Stats write failed: %s", exc)
            return None

class MTGAExporter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = logging.getLogger(self.__class__.__name__)

    def run(self) -> bool:
        print("=" * 60)
        print("  MTGA Collection Exporter  v3.3")
        print("=" * 60)
        print(f"  Output: {self.cfg.output_dir}\n")
        print("[1/4] Loading card database…")
        loader = DatabaseLoader(self.cfg)
        db = loader.load()
        if not db:
            print("\n✗ Database initialisation failed.")
            input("Press Enter to exit…")
            return False
        print(f"  ✓ {len(db)} cards loaded")
        print("\n[2/4] Configuring anchor cards…")
        mgr = AnchorManager(self.cfg, db)
        anchors = mgr.get_anchors()
        if len(anchors) < AnchorManager.MIN_ANCHORS:
            print(f"\n✗ Need at least {AnchorManager.MIN_ANCHORS} anchors.")
            input("Press Enter to exit…")
            return False
        print(f"  ✓ {len(anchors)} anchors ready")
        if self.cfg.include_descriptions is None:
            print("\n[Setup] Include card descriptions in TXT output?")
            resp = input("  [y/N]: ").strip().lower()
            self.cfg.include_descriptions = resp in ('y', 'yes')
        print(f"  → Descriptions {'enabled' if self.cfg.include_descriptions else 'disabled'}")
        print("\n[3/4] Scanning MTGA memory…")
        scanner = MemoryScanner(self.cfg, db)
        if not scanner.connect():
            input("Press Enter to exit…")
            return False
        collection = scanner.find_collection(anchors)
        if not collection:
            print("\n✗ Could not locate the collection in memory.")
            print("  Troubleshooting:")
            print("  • Make sure you're on the 'Decks' tab in MTGA")
            print("  • Verify your anchor quantities are exact")
            print("  • Try different anchor cards (rares/mythics)")
            print("  • Increase --scan-range (default 8 MB)")
            input("\nPress Enter to exit…")
            return False
        print(f"  ✓ {len(collection)} unique entries found")
        print("\n[4/4] Writing output files…")
        writer = CollectionWriter(self.cfg, db)
        files = writer.write_all(collection, self.cfg.include_descriptions)
        print(f"\n{'=' * 60}")
        print(f"  ✓ Export complete — {len(files)} file(s):")
        for fp in files: print(f"    • {fp.name}")
        print(f"{'=' * 60}")
        if self.cfg.auto_open_explorer:
            try:
                if sys.platform == 'darwin':
                    subprocess.Popen(['open', '-R', str(self.cfg.output_txt)])
                else:
                    subprocess.Popen(f'explorer /select,"{self.cfg.output_txt}"')
            except Exception: pass
        return True

def main() -> None:
    enable_windows_ansi()
    parser = argparse.ArgumentParser(description="MTGA Collection Exporter v3.3", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-d", "--descriptions", action="store_true", default=None, help="Force include card oracle text in TXT output")
    parser.add_argument("--no-descriptions", action="store_false", dest="descriptions", help="Force exclude card oracle text in TXT output")
    parser.add_argument("-f", "--force-refresh", action="store_true", help="Force-rebuild the card database cache")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--no-explorer", action="store_true", help="Don't auto-open File Explorer after export")
    parser.add_argument("--scan-range", type=int, default=8, metavar="MB", help="Memory scan window per anchor hit (default 8)")
    args = parser.parse_args()

    if getattr(sys, "frozen", False): base = Path(sys.executable).parent
    else: base = Path(__file__).resolve().parent

    cfg = Config.from_base_dir(base)
    cfg.include_descriptions = args.descriptions
    cfg.force_refresh = args.force_refresh
    cfg.verbose = args.verbose
    cfg.auto_open_explorer = not args.no_explorer
    cfg.scan_range_mb = max(1, args.scan_range)

    setup_logging(cfg)
    logging.info("Base directory: %s", base)

    app = MTGAExporter(cfg)
    try:
        success = app.run()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        success = False
    except Exception as exc:
        logging.exception("Unhandled error")
        print(f"\n✗ Unexpected error: {exc}")
        print(f"  See log file: {cfg.output_dir / 'mtga_exporter.log'}")
        success = False

    input("\nPress Enter to exit…")
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
