"""Tests for card-database metadata extraction and anchor printing selection."""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mtg  # noqa: E402


# --------------------------------------------------------------------------
# rarity enum -> Scryfall-style name
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (0, ""),
    (1, "basic"),
    (2, "common"),
    (3, "uncommon"),
    (4, "rare"),
    (5, "mythic"),
    ("2", "common"),
    ("5", "mythic"),
    (None, ""),
    ("", ""),
])
def test_normalize_rarity_maps_mtga_enum(raw, expected):
    assert mtg.normalize_rarity(raw) == expected


@pytest.mark.parametrize("already_named", ["common", "uncommon", "rare", "mythic", "special", "bonus"])
def test_normalize_rarity_passes_through_names(already_named):
    """Scryfall already supplies names; normalising must be idempotent."""
    assert mtg.normalize_rarity(already_named) == already_named


def test_normalize_rarity_unknown_code_is_dropped():
    assert mtg.normalize_rarity(99) == ""


# --------------------------------------------------------------------------
# MTGA OldSchoolManaText -> brace notation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("", ""),
    (None, ""),
    ("oW", "{W}"),
    ("o1", "{1}"),
    ("oX", "{X}"),
    ("oC", "{C}"),
    ("o4oGoW", "{4}{G}{W}"),
    ("o8oCoC", "{8}{C}{C}"),
    ("o10", "{10}"),
    ("o15", "{15}"),
    ("o3o(G/W)o(G/W)", "{3}{G/W}{G/W}"),
    ("o1o(B/P)o(B/P)", "{1}{B/P}{B/P}"),
    ("o(2/R)", "{2/R}"),
    ("o(G/W/P)", "{G/W/P}"),
    ("o(U/P)", "{U/P}"),
])
def test_convert_mana_text(text, expected):
    assert mtg.convert_mana_text(text) == expected


# --------------------------------------------------------------------------
# type line assembly
# --------------------------------------------------------------------------
@pytest.mark.parametrize("supertype,subtype,expected", [
    ("Legendary Creature", "Elder Dragon", "Legendary Creature \u2014 Elder Dragon"),
    ("Creature", "Shapeshifter Rebel", "Creature \u2014 Shapeshifter Rebel"),
    ("Artifact", None, "Artifact"),
    ("Artifact", "", "Artifact"),
    ("Basic Land", "Plains", "Basic Land \u2014 Plains"),
    ("", "Dragon", ""),
    (None, None, ""),
])
def test_build_type_line(supertype, subtype, expected):
    assert mtg.build_type_line(supertype, subtype) == expected


# --------------------------------------------------------------------------
# end-to-end parse of an MTGA-shaped sqlite file
# --------------------------------------------------------------------------
def _make_mtga_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "Raw_CardDatabase_fixture.mtga"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE Localizations_enUS (LocId INT, Loc TEXT);
        INSERT INTO Localizations_enUS VALUES
            (1, 'Dragonlord Dromoka'), (2, 'Legendary Creature'), (3, 'Elder Dragon'),
            (4, 'Plains'),             (5, 'Basic Land'),
            (6, 'Flesh Duplicate'),    (7, 'Creature'),        (8, 'Shapeshifter Rebel'),
            (9, 'Plains'), (10, '+2 Mace'), (11, 'Artifact'), (12, 'Equipment'),
            (13, 'Equip {o3}');
        CREATE TABLE Cards (
            GrpId INT, TitleId INT, ExpansionCode TEXT, CollectorNumber TEXT,
            AbilityIds TEXT, Rarity INT, TypeTextId INT, SubtypeTextId INT,
            OldSchoolManaText TEXT
        );
        INSERT INTO Cards VALUES
            (101157, 1, '2X2', '376', NULL, 5, 2, 3,    'o4oGoW'),
            (91301,  4, 'ACR', '101', NULL, 1, 5, 9,    ''),
            (106568, 6, 'SPG', '0',   NULL, 4, 7, 8,    'oUoU'),
            (77106, 10, 'AFR', '1', '1:13', 2, 11, 12, 'o1oW');
    """)
    conn.commit()
    conn.close()
    return path


def test_parse_sqlite_extracts_metadata_local_db_provides(tmp_path):
    loader = mtg.DatabaseLoader(mtg.Config(output_dir=tmp_path))
    cards = loader._parse_sqlite(_make_mtga_fixture(tmp_path))

    dromoka = cards[101157]
    assert dromoka.name == "Dragonlord Dromoka"
    assert dromoka.set == "2X2"
    assert dromoka.collector_number == "376"
    assert dromoka.mana_cost == "{4}{G}{W}"
    assert dromoka.type_line == "Legendary Creature \u2014 Elder Dragon"
    assert dromoka.rarity == "mythic"

    plains = cards[91301]
    assert plains.rarity == "basic"
    assert plains.type_line == "Basic Land \u2014 Plains"
    assert plains.mana_cost == ""

    duplicate = cards[106568]
    assert duplicate.mana_cost == "{U}{U}"
    assert duplicate.type_line == "Creature \u2014 Shapeshifter Rebel"
    assert duplicate.rarity == "rare"
    # MTGA really does store '0' for this digital-only printing; must not be blanked
    assert duplicate.collector_number == "0"


def test_parse_sqlite_tolerates_schema_without_new_columns(tmp_path):
    """Older/partial game builds may lack the metadata columns entirely."""
    path = tmp_path / "Raw_CardDatabase_legacy.mtga"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE Localizations_enUS (LocId INT, Loc TEXT);
        INSERT INTO Localizations_enUS VALUES (1, 'Mana Vault'), (2, 'Artifact');
        CREATE TABLE Cards (GrpId INT, TitleId INT, ExpansionCode TEXT,
                            CollectorNumber TEXT, AbilityIds TEXT, Rarity INT);
        INSERT INTO Cards VALUES (87972, 1, '2X2', '308', NULL, 5);
    """)
    conn.commit()
    conn.close()

    cards = mtg.DatabaseLoader(mtg.Config(output_dir=tmp_path))._parse_sqlite(path)
    vault = cards[87972]
    assert vault.name == "Mana Vault"
    assert vault.rarity == "mythic"
    assert vault.mana_cost == ""
    assert vault.type_line == ""


# --------------------------------------------------------------------------
# cache schema versioning + rarity repair on read
# --------------------------------------------------------------------------
def _write_cache(tmp_path, version):
    cfg = mtg.Config(
        output_dir=tmp_path,
        lookup_file=tmp_path / "arena_id_lookup.json",
        cache_meta_file=tmp_path / "arena_id_lookup.meta.json",
    )
    cfg.lookup_file.write_text(json.dumps({
        str(i): {"name": f"Card{i}", "set": "XXX", "rarity": "2",
                 "mana_cost": "", "type_line": ""}
        for i in range(1000, 1200)
    }), encoding="utf-8")
    if version is not None:
        cfg.cache_meta_file.write_text(json.dumps({"version": version}), encoding="utf-8")
    return cfg


def test_load_cache_rejects_older_schema_version(tmp_path):
    cfg = _write_cache(tmp_path, mtg.CACHE_VERSION - 1)
    assert mtg.DatabaseLoader(cfg)._load_cache() is None


def test_load_cache_rejects_missing_version(tmp_path):
    cfg = _write_cache(tmp_path, None)
    assert mtg.DatabaseLoader(cfg)._load_cache() is None


def test_load_cache_accepts_current_version_and_repairs_numeric_rarity(tmp_path):
    cfg = _write_cache(tmp_path, mtg.CACHE_VERSION)
    loaded = mtg.DatabaseLoader(cfg)._load_cache()
    assert loaded is not None
    assert loaded[1000].rarity == "common"


def test_save_cache_writes_current_version(tmp_path):
    cfg = mtg.Config(
        output_dir=tmp_path,
        lookup_file=tmp_path / "arena_id_lookup.json",
        cache_meta_file=tmp_path / "arena_id_lookup.meta.json",
    )
    cards = {i: mtg.CardInfo(i, f"Card{i}", rarity="common") for i in range(1000, 1150)}
    mtg.DatabaseLoader(cfg)._save_cache(cards)
    meta = json.loads(cfg.cache_meta_file.read_text(encoding="utf-8"))
    assert meta["version"] == mtg.CACHE_VERSION
    assert meta["cards"] == 150
    assert mtg.DatabaseLoader(cfg)._load_cache()[1000].rarity == "common"


# --------------------------------------------------------------------------
# anchor printing selection
# --------------------------------------------------------------------------
def _anchor_mgr(tmp_path, monkeypatch, replies):
    db = {
        98677: mtg.CardInfo(98677, "Enlightened Tutor", "TLE", "305", "rare"),
        6947: mtg.CardInfo(6947, "Enlightened Tutor", "MIR", "14", "uncommon"),
        19893: mtg.CardInfo(19893, "Mass Hysteria", "MRD", "99", "rare"),
    }
    mgr = mtg.AnchorManager(mtg.Config(output_dir=tmp_path), db)
    remaining = iter(replies)
    monkeypatch.setattr("builtins.input", lambda *a: next(remaining))
    return mgr


def test_find_card_prompts_when_name_has_several_printings(tmp_path, monkeypatch):
    """Picking an arbitrary printing silently produced zero memory hits."""
    mgr = _anchor_mgr(tmp_path, monkeypatch, ["2"])
    anchor = mgr._find_card("Enlightened Tutor", set())
    assert anchor is not None
    assert anchor.arena_id == 6947  # candidates are newest-first, so #2 is MIR


def test_find_card_blank_entry_accepts_newest_printing(tmp_path, monkeypatch):
    mgr = _anchor_mgr(tmp_path, monkeypatch, [""])
    assert mgr._find_card("Enlightened Tutor", set()).arena_id == 98677


def test_find_card_invalid_choice_cancels(tmp_path, monkeypatch):
    mgr = _anchor_mgr(tmp_path, monkeypatch, ["9"])
    assert mgr._find_card("Enlightened Tutor", set()) is None


def test_find_card_single_printing_never_prompts(tmp_path, monkeypatch):
    # no canned replies: any input() call raises StopIteration and fails the test
    mgr = _anchor_mgr(tmp_path, monkeypatch, [])
    assert mgr._find_card("Mass Hysteria", set()).arena_id == 19893


def test_find_card_only_one_unused_printing_never_prompts(tmp_path, monkeypatch):
    mgr = _anchor_mgr(tmp_path, monkeypatch, [])
    assert mgr._find_card("Enlightened Tutor", {98677}).arena_id == 6947


def test_find_card_all_printings_used_reports_conflict(tmp_path, monkeypatch, capsys):
    mgr = _anchor_mgr(tmp_path, monkeypatch, [])
    assert mgr._find_card("Enlightened Tutor", {98677, 6947}) is None
    assert "Already added" in capsys.readouterr().out


# --------------------------------------------------------------------------
# rules text: MTGA's inline '{oX}' mana placeholders -> brace notation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("interior,expected", [
    ("oT", "{T}"),
    ("oQ", "{Q}"),
    ("oE", "{E}"),
    ("oH", "{H}"),
    ("oSi", "{Si}"),
    ("o0", "{0}"),
    ("o15", "{15}"),
    ("oX", "{X}"),
    ("oC", "{C}"),
    ("o2oB", "{2}{B}"),
    ("oWoUoBoRoG", "{W}{U}{B}{R}{G}"),
    ("oRoRoRoR", "{R}{R}{R}{R}"),
    ("oEoEoE", "{E}{E}{E}"),
    ("o(G/W)", "{G/W}"),
    ("o(B/P)", "{B/P}"),
    ("o(2/R)", "{2/R}"),
    ("o(G/W/P)", "{G/W/P}"),
])
def test_normalize_rules_text_real_token_shapes(interior, expected):
    assert mtg.normalize_rules_text("{" + interior + "}") == expected


@pytest.mark.parametrize("text,expected", [
    ("", ""),
    (None, ""),
    ("Equip {o3}", "Equip {3}"),
    ("{oT}: Draw a card.", "{T}: Draw a card."),
    ("you may pay {o1}. If you do, create a 1/1 Servo.",
     "you may pay {1}. If you do, create a 1/1 Servo."),
    ("<i>Channel</i><nobr> - </nobr> {o1oG}, Discard this card:",
     "<i>Channel</i><nobr> - </nobr> {1}{G}, Discard this card:"),
    # already-normalised text is left alone
    ("Add {W}{U}.", "Add {W}{U}."),
    ("Equip {3}", "Equip {3}"),
    # ICU MessageFormat placeholders survive, including nested braces
    ("Accept one of {1, CardId}'s conditions.", "Accept one of {1, CardId}'s conditions."),
    ("{2, plural, =1 {Put {2, Number} counter on it}}",
     "{2, plural, =1 {Put {2, Number} counter on it}}"),
    # prose and keywords in braces survive
    ("{Discard a card.}", "{Discard a card.}"),
    ("{Cost}", "{Cost}"),
    ("{Choose a creature to keep.}", "{Choose a creature to keep.}"),
    ("{a Forest card}", "{a Forest card}"),
    ("{0}", "{0}"),
    ("{ because the opposing team ran out of time.}",
     "{ because the opposing team ran out of time.}"),
    # non-braced text is untouched
    ("CARDNAME gets +1/+1 until end of turn.", "CARDNAME gets +1/+1 until end of turn."),
    # degenerate braces are passed through verbatim
    ("pay {o1 to do it", "pay {o1 to do it"),
    ("{o}", "{o}"),
])
def test_normalize_rules_text_preserves_non_mana_braces(text, expected):
    assert mtg.normalize_rules_text(text) == expected


def test_build_desc_normalizes_mana_placeholders():
    loc = {7: "Equip {o3}", 8: "{oT}: Draw a card."}
    assert mtg.DatabaseLoader._build_desc("1:7,2:8", loc) == "Equip {3}\n{T}: Draw a card."


def test_parse_sqlite_normalizes_oracle_text(tmp_path):
    cards = mtg.DatabaseLoader(mtg.Config(output_dir=tmp_path))._parse_sqlite(
        _make_mtga_fixture(tmp_path)
    )
    mace = cards[77106]
    assert mace.name == "+2 Mace"
    assert mace.desc == "Equip {3}"
    assert mace.mana_cost == "{1}{W}"
    assert mace.type_line == "Artifact \u2014 Equipment"
    assert mace.rarity == "common"


# --------------------------------------------------------------------------
# packaging / runtime version consistency
# --------------------------------------------------------------------------
def test_module_version_matches_pyproject():
    """Banner, --help and the User-Agent all read one constant; it must agree
    with the packaged version so they cannot drift on a bump."""
    pyproject = Path(mtg.__file__).resolve().parent / "pyproject.toml"
    declared = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M
    ).group(1)
    assert mtg.__version__ == declared
