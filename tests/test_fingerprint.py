from kraken_tool import fingerprint as F


def rec(answers, temp=40.0):
    return F.make_record(answers, "1.2.3", temp, 0)


def test_patterns_light_exact_phase():
    img = F.phase_pattern((16, 16), "col", 1)
    lit = [x for x in range(16) if img.getpixel((x, 5)) != (0, 0, 0)]
    assert lit == [1, 5, 9, 13]
    img = F.phase_pattern((16, 16), "row", 2)
    assert [y for y in range(16) if img.getpixel((3, y)) != (0, 0, 0)] == [2, 6, 10, 14]
    assert F.tile_pattern((640, 640)).size == (640, 640)


def test_parsers():
    assert F.parse_look("Y") == F.OK and F.parse_look("stripes") == F.STRIPED
    assert F.parse_look("x") is None
    assert F.parse_tiles("") == [] and F.parse_tiles("5, 1 2") == [1, 2, 5]
    assert F.parse_tiles("17") is None and F.parse_tiles("a") is None


def test_classify_categories():
    assert F.classify({}).category == "clean"
    v = F.classify({"col1": "striped", "col3": "striped"})
    assert v.category == "column-lane" and "odd" in v.summary
    assert F.classify({"row0": "fail"}).category == "row-lane"
    assert F.classify({"col0": "fail", "row0": "fail"}).category == "broad"
    assert F.classify({"tiles": [1, 2]}).category == "regional"
    v = F.classify({"liquid": "striped", "white_after_black": "striped"})
    assert v.category == "write" and any("built-in" in d for d in v.details)


def test_cross_boot_fixed_vs_varying():
    fixed = [rec({"col1": "fail", "col3": "fail"}) for _ in range(3)]
    assert any("SAME lane" in l for l in F.cross_boot_analysis(fixed))
    varying = [rec({"col1": "fail", "col3": "fail"}), rec({"col0": "fail", "col2": "fail"})]
    assert any("CHANGES between boots" in l for l in F.cross_boot_analysis(varying))


def test_cross_boot_temperature_trend():
    runs = [rec({"col1": "fail"}, 48), rec({"col1": "fail"}, 47), rec({}, 31), rec({}, 30)]
    text = "\n".join(F.cross_boot_analysis(runs))
    assert "hotter" in text and "thermally" in text


def test_log_roundtrip(tmp_path):
    p = tmp_path / "x" / "log.jsonl"
    F.append_log(p, rec({}))
    p.open("a").write("garbage\n")
    assert len(F.load_log(p)) == 1
