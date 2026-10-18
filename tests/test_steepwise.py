from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

import steepwise as sw

SENCHA = """\
[Sencha]
type = green
leaf_g = 4
water_ml = 200
temp_c = 75
steep_s = 60
max_steeps = 3
price_per_100g = 14.80
"""


def make_tea(**overrides):
    values = dict(
        name="Tea", line=1, kind="green", leaf_g=Fraction(4), water_ml=200,
        temp_c=75, steep_s=60, extend_s=15, max_steeps=3,
    )
    values.update(overrides)
    return sw.Tea(**values)


def test_parse_reads_required_settings_and_defaults():
    teas, problems = sw.parse_teas(SENCHA)
    assert problems == []
    tea = teas[0]
    assert tea.name == "Sencha"
    assert tea.line == 1
    assert tea.kind == "green"
    assert tea.leaf_g == Fraction(4)
    assert tea.extend_s == 15
    assert tea.max_steeps == 3
    assert tea.price_per_100g == Decimal("14.80")
    assert tea.stock_g is None


def test_missing_required_settings_reported_at_section_line():
    text = "[Oolong]\ntype = oolong\nleaf_g = 5\n"
    teas, problems = sw.parse_teas(text)
    assert teas == []
    assert len(problems) == 1
    assert problems[0].level == "error"
    assert problems[0].line == 1
    assert "water_ml" in problems[0].message
    assert "steep_s" in problems[0].message


def test_out_of_range_value_points_at_its_own_line():
    text = "[X]\ntype = green\nleaf_g = 4\nwater_ml = 200\ntemp_c = 150\nsteep_s = 60\n"
    teas, problems = sw.parse_teas(text)
    assert teas == []
    assert [(p.line, p.level) for p in problems] == [(5, "error")]
    assert "temp_c" in problems[0].message


def test_unknown_type_lists_the_choices():
    text = "[X]\ntype = mate\nleaf_g = 4\nwater_ml = 200\ntemp_c = 75\nsteep_s = 60\n"
    _, problems = sw.parse_teas(text)
    assert "unknown type" in problems[0].message
    assert "green" in problems[0].message


def test_duplicate_key_and_duplicate_tea_are_both_reported():
    text = (
        "[A]\n"          # 1
        "type = green\n"  # 2
        "leaf_g = 4\n"    # 3
        "leaf_g = 5\n"    # 4
        "water_ml = 200\n"  # 5
        "temp_c = 80\n"   # 6
        "steep_s = 60\n"  # 7
        "[A]\n"           # 8
        "type = green\n"  # 9
    )
    teas, problems = sw.parse_teas(text)
    assert len(teas) == 1
    assert sorted(p.line for p in problems) == [4, 8]
    messages = {p.line: p.message for p in problems}
    assert "first set at line 3" in messages[4]
    assert "first defined at line 1" in messages[8]


def test_setting_outside_any_section_is_an_error():
    text = "type = green\n" + SENCHA
    teas, problems = sw.parse_teas(text)
    assert len(teas) == 1
    assert problems[0].line == 1
    assert "outside any [tea] section" in problems[0].message


def test_stock_without_counted_date_is_an_error():
    text = SENCHA + "stock_g = 50\n"
    teas, problems = sw.parse_teas(text)
    assert teas == []
    assert "counted" in problems[0].message


def test_unknown_setting_is_a_warning_and_the_tea_still_loads():
    text = SENCHA + "colour = green\n"
    teas, problems = sw.parse_teas(text)
    assert len(teas) == 1
    assert [(p.level, p.line) for p in problems] == [("warning", 9)]


def test_one_broken_section_does_not_hide_the_others():
    text = "[Broken]\ntype = green\n\n" + SENCHA.replace("Sencha", "Good")
    teas, problems = sw.parse_teas(text)
    assert [tea.name for tea in teas] == ["Good"]
    assert any(p.level == "error" for p in problems)


def test_infusion_schedule_lengthens_each_steep():
    tea = make_tea(steep_s=60, extend_s=15, max_steeps=3)
    schedule = sw.infusion_schedule(tea)
    assert [s.steep_s for s in schedule] == [60, 75, 90]
    assert [s.pour_offset_s for s in schedule] == [0, 60, 135]
    assert sw.total_seconds(tea) == 225


def test_dose_ratio_is_exact_fraction():
    assert sw.dose_ratio(make_tea(leaf_g=Fraction(4), water_ml=300)) == Fraction(4, 3)


def test_brew_cost_rounds_half_up_to_the_cent():
    tea = make_tea(leaf_g=Fraction(1, 4), price_per_100g=Decimal("10.00"))
    assert sw.brew_cost(tea) == Decimal("0.03")


def test_brew_cost_is_none_without_a_price():
    assert sw.brew_cost(make_tea()) is None


def test_lint_warns_when_temperature_or_dose_is_outside_guidance():
    tea = make_tea(name="Hot Green", line=7, temp_c=100, leaf_g=Fraction(10), water_ml=100)
    messages = [p.message for p in sw.lint_teas([tea])]
    assert any("temp_c 100" in m for m in messages)
    assert any("dose is 10 g/100 ml" in m for m in messages)
    assert all(p.line == 7 for p in sw.lint_teas([tea]))


def test_stock_left_counts_only_sessions_on_or_after_the_counted_date():
    tea = make_tea(stock_g=Fraction(50), counted=date(2026, 9, 1))
    sessions = [
        {"tea": "tea", "date": "2026-08-30", "leaf_g": "5", "infusions": 3},
        {"tea": "TEA", "date": "2026-09-02", "leaf_g": "4", "infusions": 3},
        {"tea": "tea", "date": "2026-09-05", "leaf_g": "2.5", "infusions": 3},
    ]
    assert sw.stock_left(tea, sessions) == Fraction(87, 2)


def test_stock_left_is_none_without_stock():
    assert sw.stock_left(make_tea(), []) is None


def test_months_between_respects_day_of_month():
    assert sw.months_between(date(2026, 1, 15), date(2026, 2, 14)) == 0
    assert sw.months_between(date(2026, 1, 15), date(2026, 2, 15)) == 1


def test_shelf_state_thresholds_for_green_tea():
    tea = make_tea(kind="green", opened=date(2026, 1, 1))  # limit 12 months
    assert sw.shelf_state(tea, date(2026, 6, 30))[0] == "fresh"
    assert sw.shelf_state(tea, date(2026, 11, 1)) == ("use soon", 10)
    assert sw.shelf_state(tea, date(2027, 1, 1)) == ("past shelf life", 12)
    assert sw.shelf_state(make_tea(), date(2026, 1, 1)) == ("not opened", None)


def test_make_session_defaults_and_bounds():
    tea = make_tea(max_steeps=3, leaf_g=Fraction(4))
    session = sw.make_session(tea, day=date(2026, 10, 18))
    assert session["infusions"] == 3
    assert session["leaf_g"] == "4"
    assert session["date"] == "2026-10-18"
    with pytest.raises(ValueError, match="infusions"):
        sw.make_session(tea, day=date(2026, 10, 18), infusions=4)
    with pytest.raises(ValueError, match="rating"):
        sw.make_session(tea, day=date(2026, 10, 18), rating=6)


def test_journal_round_trip_and_bad_json_is_rejected(tmp_path):
    path = tmp_path / "journal.json"
    session = {"tea": "Sencha", "date": "2026-10-10", "leaf_g": "4",
               "infusions": 3, "rating": 4, "notes": ""}
    sw.save_journal(path, [session])
    assert sw.load_journal(path) == [session]
    assert sw.load_journal(tmp_path / "missing.json") == []

    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(sw.JournalError, match="invalid JSON"):
        sw.load_journal(path)


def test_journal_summary_totals_averages_and_spend():
    tea = make_tea(name="Sencha", price_per_100g=Decimal("10.00"))
    sessions = [
        {"tea": "sencha", "date": "2026-10-01", "leaf_g": "4", "infusions": 3, "rating": 4},
        {"tea": "Sencha", "date": "2026-10-05", "leaf_g": "4", "infusions": 2, "rating": 5},
    ]
    (row,) = sw.journal_summary([tea], sessions)
    assert row["sessions"] == 2
    assert row["infusions"] == 5
    assert row["grams"] == 8
    assert row["avg_rating"] == Fraction(9, 2)
    assert row["spend"] == Decimal("0.80")


def test_suggest_next_skips_cooldown_empty_and_expired_teas():
    today = date(2026, 10, 18)
    fresh = make_tea(name="Fresh", line=1, opened=date(2026, 10, 1))
    empty = make_tea(name="Empty", line=2, stock_g=Fraction(0), counted=date(2026, 10, 1))
    expired = make_tea(name="Expired", line=3, opened=date(2025, 1, 1))  # green limit 12
    never = make_tea(name="Never", line=4)
    rated = make_tea(name="Rated", line=5)
    sessions = [
        {"tea": "Fresh", "date": "2026-10-17", "leaf_g": "4", "infusions": 3, "rating": 5},
        {"tea": "Rated", "date": "2026-10-08", "leaf_g": "4", "infusions": 3, "rating": 5},
    ]
    picks = sw.suggest_next([fresh, empty, expired, never, rated], sessions, today)
    assert [tea.name for _, tea, _ in picks] == ["Rated", "Never"]
    scores = {tea.name: score for score, tea, _ in picks}
    assert scores["Rated"] == pytest.approx(5.0 + 10 / 30)
    assert scores["Never"] == pytest.approx(4.0)


def test_render_show_prints_clock_times_when_a_start_is_given():
    tea = make_tea(steep_s=60, extend_s=15, max_steeps=3)
    text = sw.render_show(tea, [], date(2026, 10, 18), start_s=sw._parse_start("07:30"))
    assert "pour 07:30:00" in text
    assert "pour 07:31:00" in text
    assert "Total brewing time: 3:45" in text


def test_render_show_uses_offsets_without_a_start():
    tea = make_tea(steep_s=60, extend_s=15, max_steeps=2)
    text = sw.render_show(tea, [], date(2026, 10, 18), start_s=None)
    assert "pour +0:00" in text
    assert "pour +1:00" in text


def test_parse_start_rejects_bad_times():
    assert sw._parse_start("07:30") == 27000
    with pytest.raises(ValueError):
        sw._parse_start("25:00")


def test_render_csv_has_header_and_handles_missing_optional_fields():
    sessions = [{"tea": "Sencha", "date": "2026-10-10", "leaf_g": "4", "infusions": 3}]
    lines = sw.render_csv(sessions).splitlines()
    assert lines[0] == "tea,date,leaf_g,infusions,rating,notes"
    assert lines[1] == "Sencha,2026-10-10,4,3,,"


def test_cli_check_exit_codes_and_line_numbered_output(tmp_path, capsys):
    good = tmp_path / "good.txt"
    good.write_text(SENCHA, encoding="utf-8")
    assert sw.main(["check", str(good)]) == 0

    bad = tmp_path / "bad.txt"
    bad.write_text("[X]\ntype = green\nleaf_g = 4\nwater_ml = 200\ntemp_c = 150\nsteep_s = 60\n",
                   encoding="utf-8")
    assert sw.main(["check", str(bad)]) == 1
    assert f"{bad}:5: error:" in capsys.readouterr().out


def test_cli_strict_turns_warnings_into_failures(tmp_path):
    warm = tmp_path / "warm.txt"
    warm.write_text(SENCHA.replace("temp_c = 75", "temp_c = 100"), encoding="utf-8")
    assert sw.main(["check", str(warm)]) == 0
    assert sw.main(["check", "--strict", str(warm)]) == 1


def test_cli_log_then_stats_end_to_end(tmp_path, capsys):
    teas = tmp_path / "teas.txt"
    teas.write_text(SENCHA, encoding="utf-8")
    journal = tmp_path / "journal.json"

    assert sw.main(["log", str(teas), "Sencha", "--journal", str(journal),
                    "--date", "2026-10-10", "--rating", "4"]) == 0
    out = capsys.readouterr().out
    assert "logged Sencha on 2026-10-10: 3 infusion(s), 4 g" in out

    assert sw.main(["stats", str(teas), "--journal", str(journal)]) == 0
    out = capsys.readouterr().out
    assert "Sencha" in out
    assert "Totals: 1 sessions, 4 g, spend 0.59" in out


def test_cli_unknown_tea_name_exits_2(tmp_path, capsys):
    teas = tmp_path / "teas.txt"
    teas.write_text(SENCHA, encoding="utf-8")
    assert sw.main(["show", str(teas), "Matcha", "--journal", str(tmp_path / "j.json")]) == 2
    assert "no tea named 'Matcha'" in capsys.readouterr().err
