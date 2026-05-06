"""MTGA Collection Exporter GUI.

Cross-platform companion to mtg.py. Loads an mtga_collection.json export,
fetches color data from Scryfall (cached locally), and lets you filter the
collection by color before re-exporting to txt / json / Moxfield csv.

Runs on macOS, Linux, and Windows. Requires Python 3.8+ with tkinter and the
`requests` package.
"""

import csv
import json
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_COLLECTION = SCRIPT_DIR / "mtga_collection.json"
COLOR_CACHE = SCRIPT_DIR / "color_cache.json"

COLORS = [
    ("W", "White"),
    ("U", "Blue"),
    ("B", "Black"),
    ("R", "Red"),
    ("G", "Green"),
    ("C", "Colorless"),
]


def card_key(card):
    return f"{(card.get('set') or '').upper()}|{card.get('cn') or ''}|{card.get('name') or ''}"


def card_colors_from_scryfall(entry):
    """Pull a color list from a Scryfall card record, handling DFCs."""
    colors = entry.get("colors")
    if colors is None and "card_faces" in entry:
        faces = entry.get("card_faces") or []
        seen = []
        for face in faces:
            for c in face.get("colors", []) or []:
                if c not in seen:
                    seen.append(c)
        colors = seen
    if not colors:
        return ["C"]
    return list(colors)


class ExporterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MTGA Collection Exporter")
        self.root.geometry("780x460")

        self.collection = []
        self.collection_path = None
        self.color_cache = {}

        self._load_color_cache()
        self._build_ui()

        if DEFAULT_COLLECTION.exists():
            self._load_collection_file(DEFAULT_COLLECTION)

    def _load_color_cache(self):
        if COLOR_CACHE.exists():
            try:
                with COLOR_CACHE.open(encoding="utf-8") as f:
                    self.color_cache = json.load(f)
            except Exception:
                self.color_cache = {}

    def _save_color_cache(self):
        try:
            with COLOR_CACHE.open("w", encoding="utf-8") as f:
                json.dump(self.color_cache, f)
        except Exception:
            pass

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Load collection (JSON)…", command=self._on_load).pack(side="left")
        ttk.Button(top, text="Refresh color data", command=self._on_refresh_colors).pack(side="left", padx=6)
        self.file_label = ttk.Label(top, text="No collection loaded")
        self.file_label.pack(side="left", padx=10)

        clr = ttk.LabelFrame(self.root, text="Colors to include", padding=10)
        clr.pack(fill="x", padx=10, pady=5)
        self.color_vars = {}
        for code, name in COLORS:
            v = tk.BooleanVar(value=True)
            self.color_vars[code] = v
            ttk.Checkbutton(clr, text=f"{name} ({code})", variable=v, command=self._update_count).pack(side="left", padx=6)
        ttk.Button(clr, text="All", command=self._select_all, width=5).pack(side="left", padx=(12, 2))
        ttk.Button(clr, text="None", command=self._select_none, width=5).pack(side="left")

        mode = ttk.LabelFrame(self.root, text="Match mode", padding=10)
        mode.pack(fill="x", padx=10, pady=5)
        self.mode_var = tk.StringVar(value="any")
        for value, label in [
            ("any", "Any selected color  (W ticked → mono-white plus any multicolor with white)"),
            ("only", "Only selected colors  (cards never use a color you didn't tick)"),
            ("exact", "Exactly the ticked colors  (e.g. W+U → strictly Azorius)"),
        ]:
            ttk.Radiobutton(mode, text=label, value=value, variable=self.mode_var, command=self._update_count).pack(anchor="w")

        ex = ttk.LabelFrame(self.root, text="Export filtered list", padding=10)
        ex.pack(fill="x", padx=10, pady=5)
        ttk.Button(ex, text="TXT", command=lambda: self._export("txt")).pack(side="left", padx=4)
        ttk.Button(ex, text="JSON", command=lambda: self._export("json")).pack(side="left", padx=4)
        ttk.Button(ex, text="CSV (Moxfield)", command=lambda: self._export("csv")).pack(side="left", padx=4)

        self.status = ttk.Label(self.root, text="Ready. Load an mtga_collection.json to begin.", relief="sunken", anchor="w", padding=5)
        self.status.pack(fill="x", side="bottom")

    def _set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def _on_load(self):
        path = filedialog.askopenfilename(
            title="Select mtga_collection.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialdir=str(SCRIPT_DIR),
            initialfile="mtga_collection.json",
        )
        if path:
            self._load_collection_file(Path(path))

    def _load_collection_file(self, path):
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Expected a JSON list of cards")
        except Exception as e:
            messagebox.showerror("Load failed", f"Could not read {path.name}:\n{e}")
            return

        self.collection = data
        self.collection_path = path
        self.file_label.config(text=f"{path.name}  ({len(data)} entries)")
        self._maybe_fetch_colors()
        self._update_count()

    def _maybe_fetch_colors(self):
        missing = [c for c in self.collection if card_key(c) not in self.color_cache]
        if not missing:
            return
        if not messagebox.askyesno(
            "Color data needed",
            f"{len(missing)} of {len(self.collection)} cards have no cached color info.\n\n"
            "Download the Scryfall bulk catalog now? (one-time ~80 MB download, cached locally)",
        ):
            return
        threading.Thread(target=self._download_colors, args=(self.collection,), daemon=True).start()

    def _on_refresh_colors(self):
        if not self.collection:
            messagebox.showwarning("No collection", "Load a collection first.")
            return
        if not messagebox.askyesno("Refresh", "Re-download color data from Scryfall?"):
            return
        self.color_cache = {}
        threading.Thread(target=self._download_colors, args=(self.collection,), daemon=True).start()

    def _download_colors(self, cards):
        try:
            self._set_status("Fetching Scryfall bulk-data catalog…")
            meta = requests.get("https://api.scryfall.com/bulk-data/default-cards", timeout=30).json()
            url = meta.get("download_uri")
            if not url:
                raise RuntimeError("Scryfall response missing download_uri")
            self._set_status("Downloading Scryfall card data (~80 MB)…")
            payload = requests.get(url, timeout=300).json()
        except Exception as e:
            self._set_status(f"Color download failed: {e}")
            return

        self._set_status("Indexing Scryfall data…")
        by_set_cn = {}
        by_name = {}
        for entry in payload:
            colors = card_colors_from_scryfall(entry)
            set_code = (entry.get("set") or "").upper()
            cn = entry.get("collector_number") or ""
            name = entry.get("name") or ""
            if set_code and cn:
                by_set_cn[(set_code, cn)] = colors
            if name and name not in by_name:
                by_name[name] = colors
                # Also index DFC front-face name to help with split cards
                if "//" in name:
                    by_name.setdefault(name.split("//")[0].strip(), colors)

        matched = 0
        for card in cards:
            set_code = (card.get("set") or "").upper()
            cn = card.get("cn") or ""
            name = card.get("name") or ""
            colors = by_set_cn.get((set_code, cn)) or by_name.get(name) or []
            self.color_cache[card_key(card)] = colors
            if colors:
                matched += 1

        self._save_color_cache()
        self.root.after(0, self._update_count)
        self._set_status(f"Color data ready ({matched}/{len(cards)} cards matched).")

    def _select_all(self):
        for v in self.color_vars.values():
            v.set(True)
        self._update_count()

    def _select_none(self):
        for v in self.color_vars.values():
            v.set(False)
        self._update_count()

    def _selected_colors(self):
        return {code for code, v in self.color_vars.items() if v.get()}

    def _filter(self):
        selected = self._selected_colors()
        mode = self.mode_var.get()
        out = []
        for card in self.collection:
            colors = set(self.color_cache.get(card_key(card)) or [])
            if not colors:
                colors = {"C"}
            if mode == "any":
                if colors & selected:
                    out.append(card)
            elif mode == "only":
                if selected and colors.issubset(selected):
                    out.append(card)
            elif mode == "exact":
                if colors == selected:
                    out.append(card)
        return out

    def _update_count(self):
        if not self.collection:
            return
        filt = self._filter()
        unique = len(filt)
        total = sum(c.get("count", 0) for c in filt)
        self._set_status(f"{unique} unique cards / {total} copies match the current filter.")

    def _export(self, fmt):
        if not self.collection:
            messagebox.showwarning("No collection", "Load a collection first.")
            return
        rows = self._filter()
        if not rows:
            messagebox.showwarning("Nothing to export", "No cards match the current filter.")
            return

        ext = {"txt": ".txt", "json": ".json", "csv": ".csv"}[fmt]
        path = filedialog.asksaveasfilename(
            defaultextension=ext,
            initialdir=str(SCRIPT_DIR),
            initialfile=f"mtga_filtered{ext}",
            filetypes=[(fmt.upper(), f"*{ext}"), ("All files", "*.*")],
        )
        if not path:
            return

        rows = sorted(rows, key=lambda c: (c.get("name", ""), c.get("set", "")))
        try:
            if fmt == "txt":
                with open(path, "w", encoding="utf-8") as f:
                    for c in rows:
                        s = f" ({c['set']})" if c.get("set") else ""
                        f.write(f"{c.get('count', 0)} {c.get('name', '')}{s}\n")
            elif fmt == "json":
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=2)
            elif fmt == "csv":
                with open(path, "w", encoding="utf-8", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["Count", "Name", "Edition", "Condition", "Language", "Foil", "Tag"])
                    for c in rows:
                        w.writerow([c.get("count", 0), c.get("name", ""), c.get("set", ""), "Near Mint", "English", "", ""])
            messagebox.showinfo("Exported", f"Wrote {len(rows)} cards to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))


def main():
    root = tk.Tk()
    ExporterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
