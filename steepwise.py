#!/usr/bin/env python3
"""Steepwise: an offline tea-brewing planner.

Reads a plain-text tea file, checks it with line-numbered messages, plans
infusion schedules with clock times, works out exact dose ratio and cost,
tracks stock and shelf life, keeps a JSON brewing journal, and suggests what
to brew next. Standard library only; nothing here touches the network.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

JOURNAL_VERSION = 1
DEFAULT_EXTEND_S = 15
DEFAULT_MAX_STEEPS = 4
DAY_SECONDS = 86400


@dataclass(frozen=True)
class Profile:
    """Guidance ranges for one tea type. Used for lint warnings only."""

    temp_c: tuple[int, int]
    steep_s: tuple[int, int]
    g_per_100ml: tuple[Fraction, Fraction]
    shelf_months: int


PROFILES = {
    "green": Profile((70, 85), (30, 120), (Fraction(1), Fraction(4)), 12),
    "white": Profile((70, 85), (60, 180), (Fraction(1), Fraction(4)), 24),
    "oolong": Profile((85, 100), (20, 90), (Fraction(2), Fraction(8)), 18),
    "black": Profile((90, 100), (120, 300), (Fraction(1), Fraction(3)), 24),
    "puerh": Profile((90, 100), (10, 300), (Fraction(2), Fraction(8)), 60),
    "herbal": Profile((95, 100), (240, 600), (Fraction(2), Fraction(10)), 24),
}

KNOWN_KEYS = {
    "type", "leaf_g", "water_ml", "temp_c", "steep_s", "extend_s",
    "max_steeps", "price_per_100g", "stock_g", "counted", "opened",
}
REQUIRED_KEYS = ("type", "leaf_g", "water_ml", "temp_c", "steep_s")


# ---------------------------------------------------------------- data model


@dataclass(frozen=True)
class Problem:
    level: str  # "error" or "warning"
    line: int
    message: str


@dataclass(frozen=True)
class Tea:
    name: str
    line: int
    kind: str
    leaf_g: Fraction
    water_ml: int
    temp_c: int
    steep_s: int
    extend_s: int = DEFAULT_EXTEND_S
    max_steeps: int = DEFAULT_MAX_STEEPS
    price_per_100g: Decimal | None = None
    stock_g: Fraction | None = None
    counted: date | None = None
    opened: date | None = None


@dataclass(frozen=True)
class Infusion:
    number: int
    steep_s: int
    pour_offset_s: int


class JournalError(ValueError):
    """Raised when the journal file is unreadable or malformed."""


# ------------------------------------------------------------- value parsing


def _number(text: str) -> Fraction:
    try:
        return Fraction(text.strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{text!r} is not a number") from exc


def _ranged_number(lo: Fraction, hi: Fraction, whole: bool = False):
    def parse(text: str) -> Fraction:
        value = _number(text)
        if whole and value.denominator != 1:
            raise ValueError(f"{text!r} must be a whole number")
        if not lo <= value <= hi:
            raise ValueError(f"{text!r} must be between {lo} and {hi}")
        return value

    return parse


def _choice(text: str) -> str:
    kind = text.strip().lower()
    if kind not in PROFILES:
        raise ValueError(f"unknown type {text!r}; use one of {', '.join(PROFILES)}")
    return kind


def _money(text: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{text!r} is not an amount") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("price must be zero or more")
    return value


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a YYYY-MM-DD date") from exc


SETTING_PARSERS = {
    "type": _choice,
    "leaf_g": _ranged_number(Fraction(1, 10), Fraction(200)),
    "water_ml": _ranged_number(Fraction(50), Fraction(5000), whole=True),
    "temp_c": _ranged_number(Fraction(40), Fraction(100), whole=True),
    "steep_s": _ranged_number(Fraction(5), Fraction(3600), whole=True),
    "extend_s": _ranged_number(Fraction(0), Fraction(300), whole=True),
    "max_steeps": _ranged_number(Fraction(1), Fraction(20), whole=True),
    "price_per_100g": _money,
    "stock_g": _ranged_number(Fraction(0), Fraction(100000)),
    "counted": _iso_date,
    "opened": _iso_date,
}


# ------------------------------------------------------------- tea file input


def parse_teas(text: str) -> tuple[list[Tea], list[Problem]]:
    """Parse a tea file. Returns usable teas plus line-numbered problems.

    A section with any error is dropped, but the other sections still load,
    so one typo does not hide the rest of the file.
    """
    problems: list[Problem] = []
    sections: list[dict] = []
    seen: dict[str, int] = {}
    current: dict | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        header = re.fullmatch(r"\[([^\[\]]*)\]", line)
        if header:
            name = header.group(1).strip()
            current = {"name": name, "line": lineno, "fields": {}, "valid": True}
            sections.append(current)
            if not name:
                problems.append(Problem("error", lineno, "tea name in [] is empty"))
                current["valid"] = False
            elif name.lower() in seen:
                problems.append(Problem(
                    "error", lineno,
                    f"duplicate tea {name!r}; first defined at line {seen[name.lower()]}",
                ))
                current["valid"] = False
            else:
                seen[name.lower()] = lineno
            continue

        pair = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if pair:
            if current is None:
                problems.append(Problem("error", lineno, f"setting {pair.group(1)!r} is outside any [tea] section"))
                continue
            if not current["valid"]:
                continue
            key = pair.group(1).lower()
            if key in current["fields"]:
                first = current["fields"][key][0]
                problems.append(Problem("error", lineno, f"duplicate setting {key!r}; first set at line {first}"))
                continue
            current["fields"][key] = (lineno, pair.group(2).strip())
            continue

        problems.append(Problem("error", lineno, f"cannot read {line!r}; expected [Tea name] or key = value"))

    teas: list[Tea] = []
    for section in sections:
        if not section["valid"]:
            continue
        tea = _build_tea(section, problems)
        if tea is not None:
            teas.append(tea)
    return teas, problems


def _build_tea(section: dict, problems: list[Problem]) -> Tea | None:
    name, line, fields = section["name"], section["line"], section["fields"]

    for key, (key_line, _) in fields.items():
        if key not in KNOWN_KEYS:
            problems.append(Problem("warning", key_line, f"unknown setting {key!r} in [{name}] ignored"))

    missing = [key for key in REQUIRED_KEYS if key not in fields]
    if missing:
        problems.append(Problem("error", line, f"[{name}] is missing {', '.join(missing)}"))
        return None

    ok = True
    values: dict = {}
    for key, parse in SETTING_PARSERS.items():
        if key not in fields:
            continue
        key_line, text = fields[key]
        try:
            values[key] = parse(text)
        except ValueError as exc:
            problems.append(Problem("error", key_line, f"{key}: {exc}"))
            ok = False

    if "stock_g" in values and "counted" not in values:
        problems.append(Problem("error", fields["stock_g"][0], "stock_g needs a counted date to count from"))
        ok = False
    if not ok:
        return None

    return Tea(
        name=name,
        line=line,
        kind=values["type"],
        leaf_g=values["leaf_g"],
        water_ml=int(values["water_ml"]),
        temp_c=int(values["temp_c"]),
        steep_s=int(values["steep_s"]),
        extend_s=int(values.get("extend_s", DEFAULT_EXTEND_S)),
        max_steeps=int(values.get("max_steeps", DEFAULT_MAX_STEEPS)),
        price_per_100g=values.get("price_per_100g"),
        stock_g=values.get("stock_g"),
        counted=values.get("counted"),
        opened=values.get("opened"),
    )


def lint_teas(teas: list[Tea]) -> list[Problem]:
    """Warnings for teas that parsed but sit outside the type guidance."""
    out: list[Problem] = []
    for tea in teas:
        profile = PROFILES[tea.kind]
        lo, hi = profile.temp_c
        if not lo <= tea.temp_c <= hi:
            out.append(Problem("warning", tea.line, f"{tea.name}: temp_c {tea.temp_c} is outside the usual {lo}-{hi} C for {tea.kind}"))
        lo, hi = profile.steep_s
        if not lo <= tea.steep_s <= hi:
            out.append(Problem("warning", tea.line, f"{tea.name}: steep_s {tea.steep_s} is outside the usual {lo}-{hi} s for {tea.kind}"))
        ratio = dose_ratio(tea)
        lo, hi = profile.g_per_100ml
        if not lo <= ratio <= hi:
            out.append(Problem("warning", tea.line, f"{tea.name}: dose is {_num(ratio)} g/100 ml, usual range for {tea.kind} is {_num(lo)}-{_num(hi)}"))
    return out


def format_problem(problem: Problem, source: str) -> str:
    return f"{source}:{problem.line}: {problem.level}: {problem.message}"


# ------------------------------------------------------------ brew planning


def dose_ratio(tea: Tea) -> Fraction:
    """Grams of leaf per 100 ml of water, exact."""
    return tea.leaf_g * 100 / tea.water_ml


def infusion_schedule(tea: Tea) -> list[Infusion]:
    """Each infusion steeps longer than the last by extend_s seconds."""
    rows: list[Infusion] = []
    offset = 0
    for number in range(1, tea.max_steeps + 1):
        steep = tea.steep_s + (number - 1) * tea.extend_s
        rows.append(Infusion(number, steep, offset))
        offset += steep
    return rows


def total_seconds(tea: Tea) -> int:
    last = infusion_schedule(tea)[-1]
    return last.pour_offset_s + last.steep_s


def _to_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def brew_cost(tea: Tea) -> Decimal | None:
    """Cost of one full brew of the dose, rounded half-up to the cent."""
    if tea.price_per_100g is None:
        return None
    return money(tea.price_per_100g * _to_decimal(tea.leaf_g) / 100)


def months_between(earlier: date, later: date) -> int:
    months = (later.year - earlier.year) * 12 + later.month - earlier.month
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)


def shelf_state(tea: Tea, today: date) -> tuple[str, int | None]:
    """Return (state, months open). Uses 80% of the shelf limit as 'use soon'."""
    if tea.opened is None:
        return "not opened", None
    limit = PROFILES[tea.kind].shelf_months
    age = months_between(tea.opened, today)
    if age >= limit:
        return "past shelf life", age
    if age * 5 >= limit * 4:
        return "use soon", age
    return "fresh", age


# ----------------------------------------------------------- journal logic


def load_journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JournalError(f"{path}:{exc.lineno}: invalid JSON: {exc.msg}") from exc
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, list):
        raise JournalError(f"{path}: expected an object with a 'sessions' list")
    for index, session in enumerate(sessions, start=1):
        if not isinstance(session, dict) or not {"tea", "date", "leaf_g", "infusions"} <= session.keys():
            raise JournalError(f"{path}: session {index} needs tea, date, leaf_g and infusions")
    return sessions


def save_journal(path: Path, sessions: list[dict]) -> None:
    body = {"version": JOURNAL_VERSION, "sessions": sessions}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def make_session(tea: Tea, *, day: date, infusions: int | None = None,
                 rating: int | None = None, notes: str = "",
                 leaf_g: Fraction | None = None) -> dict:
    infusions = tea.max_steeps if infusions is None else infusions
    if not 1 <= infusions <= tea.max_steeps:
        raise ValueError(f"infusions must be between 1 and {tea.max_steeps} for {tea.name}")
    if rating is not None and not 1 <= rating <= 5:
        raise ValueError("rating must be between 1 and 5")
    leaf = tea.leaf_g if leaf_g is None else leaf_g
    return {
        "tea": tea.name,
        "date": day.isoformat(),
        "leaf_g": str(leaf),
        "infusions": infusions,
        "rating": rating,
        "notes": notes,
    }


def stock_left(tea: Tea, sessions: list[dict]) -> Fraction | None:
    """Counted stock minus leaf logged on or after the counted date."""
    if tea.stock_g is None:
        return None
    used = Fraction(0)
    for session in sessions:
        if session["tea"].lower() != tea.name.lower():
            continue
        if date.fromisoformat(session["date"]) >= tea.counted:
            used += Fraction(session["leaf_g"])
    return tea.stock_g - used


def journal_summary(teas: list[Tea], sessions: list[dict]) -> list[dict]:
    by_name = {tea.name.lower(): tea for tea in teas}
    rows: dict[str, dict] = {}
    for session in sessions:
        key = session["tea"].lower()
        row = rows.setdefault(key, {
            "tea": session["tea"], "sessions": 0, "infusions": 0,
            "grams": Fraction(0), "ratings": [], "spend": Decimal("0"),
        })
        leaf = Fraction(session["leaf_g"])
        row["sessions"] += 1
        row["infusions"] += int(session["infusions"])
        row["grams"] += leaf
        if session.get("rating") is not None:
            row["ratings"].append(int(session["rating"]))
        tea = by_name.get(key)
        if tea is not None and tea.price_per_100g is not None:
            row["spend"] += tea.price_per_100g * _to_decimal(leaf) / 100

    out = []
    for row in rows.values():
        ratings = row.pop("ratings")
        row["avg_rating"] = Fraction(sum(ratings), len(ratings)) if ratings else None
        row["spend"] = money(row["spend"])
        out.append(row)
    return sorted(out, key=lambda r: (-r["sessions"], r["tea"].lower()))


def suggest_next(teas: list[Tea], sessions: list[dict], today: date,
                 cooldown_days: int = 3, count: int = 3) -> list[tuple[float, Tea, str]]:
    """Rank brewable teas by average rating plus a staleness bonus.

    Unrated teas count as 3.0. The bonus grows linearly to 1.0 at 30 days
    unbrewed; a never-brewed tea gets the full bonus. Teas with no stock left,
    past shelf life, or brewed within cooldown_days are skipped.
    """
    summary = {row["tea"].lower(): row for row in journal_summary(teas, sessions)}
    last: dict[str, date] = {}
    for session in sessions:
        key = session["tea"].lower()
        day = date.fromisoformat(session["date"])
        last[key] = max(last.get(key, day), day)

    scored = []
    for tea in teas:
        key = tea.name.lower()
        left = stock_left(tea, sessions)
        if left is not None and left <= 0:
            continue
        if shelf_state(tea, today)[0] == "past shelf life":
            continue
        since = (today - last[key]).days if key in last else None
        if since is not None and since < cooldown_days:
            continue
        avg = summary[key]["avg_rating"] if key in summary else None
        base = float(avg) if avg is not None else 3.0
        bonus = 1.0 if since is None else min(since, 30) / 30
        reason = f"rated {base:.1f}" if avg is not None else "unrated"
        reason += ", never brewed" if since is None else f", last brewed {since} days ago"
        scored.append((base + bonus, tea, reason))

    scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
    return scored[:count]


# ------------------------------------------------------------------ render


def _num(value) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _mmss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _clock(start_s: int | None, offset_s: int) -> str:
    if start_s is None:
        return f"+{_mmss(offset_s)}"
    t = (start_s + offset_s) % DAY_SECONDS
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(str(value)) for value in column) for column in zip(headers, *rows)]

    def line(cells) -> str:
        return "  ".join(str(cell).ljust(width) for cell, width in zip(cells, widths)).rstrip()

    return [line(headers), line(["-" * width for width in widths])] + [line(row) for row in rows]


def render_show(tea: Tea, sessions: list[dict], today: date, start_s: int | None) -> str:
    profile = PROFILES[tea.kind]
    lines = [f"{tea.name} ({tea.kind}), teas file line {tea.line}"]
    lines.append(f"  Dose: {_num(tea.leaf_g)} g in {tea.water_ml} ml = {_num(dose_ratio(tea))} g/100 ml")
    lines.append(f"  Water: {tea.temp_c} C; first steep {_mmss(tea.steep_s)}, +{tea.extend_s} s each, up to {tea.max_steeps} infusions")
    cost = brew_cost(tea)
    if cost is not None:
        lines.append(f"  Cost: {cost} per brew (price {tea.price_per_100g} per 100 g)")
    if tea.stock_g is not None:
        lines.append(f"  Stock: {_num(stock_left(tea, sessions))} g left (counted {tea.counted.isoformat()}: {_num(tea.stock_g)} g)")
    if tea.opened is not None:
        state, age = shelf_state(tea, today)
        lines.append(f"  Shelf: {state}; opened {tea.opened.isoformat()}, {age} of {profile.shelf_months} months")
    lines.append("  Schedule:")
    for infusion in infusion_schedule(tea):
        pour = _clock(start_s, infusion.pour_offset_s)
        done = _clock(start_s, infusion.pour_offset_s + infusion.steep_s)
        lines.append(f"    #{infusion.number}  pour {pour}  steep {_mmss(infusion.steep_s)}  done {done}")
    lines.append(f"  Total brewing time: {_mmss(total_seconds(tea))}")
    return "\n".join(lines)


def render_list(teas: list[Tea], sessions: list[dict], today: date) -> str:
    rows = []
    for tea in teas:
        left = stock_left(tea, sessions)
        rows.append([
            tea.name, tea.kind, _num(tea.leaf_g), _num(dose_ratio(tea)),
            f"{tea.temp_c} C", "-" if left is None else _num(left),
            shelf_state(tea, today)[0],
        ])
    return "\n".join(_table(["Tea", "Type", "Leaf g", "g/100ml", "Temp", "Stock g", "Shelf"], rows))


def render_stats(teas: list[Tea], sessions: list[dict]) -> str:
    summary = journal_summary(teas, sessions)
    if not summary:
        return "No sessions logged yet."
    rows = [[
        row["tea"], row["sessions"], row["infusions"], _num(row["grams"]),
        "-" if row["avg_rating"] is None else f"{float(row['avg_rating']):.2f}",
        f"{row['spend']}",
    ] for row in summary]
    lines = _table(["Tea", "Sessions", "Infusions", "Grams", "Avg rating", "Spend"], rows)
    total_spend = sum((row["spend"] for row in summary), Decimal("0"))
    total_grams = sum((row["grams"] for row in summary), Fraction(0))
    lines.append("")
    lines.append(f"Totals: {sum(row['sessions'] for row in summary)} sessions, {_num(total_grams)} g, spend {money(total_spend)}")
    return "\n".join(lines)


def render_suggestions(suggestions: list[tuple[float, Tea, str]]) -> str:
    if not suggestions:
        return "Nothing to suggest: every tea is out of stock, past shelf life, or brewed too recently."
    return "\n".join(
        f"{rank}. {tea.name} (score {score:.2f}): {reason}"
        for rank, (score, tea, reason) in enumerate(suggestions, start=1)
    )


SESSION_FIELDS = ["tea", "date", "leaf_g", "infusions", "rating", "notes"]


def render_csv(sessions: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SESSION_FIELDS, lineterminator="\n")
    writer.writeheader()
    for session in sorted(sessions, key=lambda s: (s["date"], s["tea"].lower())):
        row = {field: session.get(field) for field in SESSION_FIELDS}
        writer.writerow({field: "" if value is None else value for field, value in row.items()})
    return buffer.getvalue()


# --------------------------------------------------------------------- CLI


def find_tea(teas: list[Tea], name: str) -> Tea | None:
    for tea in teas:
        if tea.name.lower() == name.strip().lower():
            return tea
    return None


def _usable_teas(path: str) -> list[Tea] | None:
    """Load teas for a command. Print any errors and return None if there are some."""
    teas, problems = parse_teas(Path(path).read_text(encoding="utf-8"))
    errors = [p for p in problems if p.level == "error"]
    if errors:
        for problem in sorted(errors, key=lambda p: p.line):
            print(format_problem(problem, path), file=sys.stderr)
        print(f"steepwise: {len(errors)} error(s) in {path}; run 'check' for the full report", file=sys.stderr)
        return None
    return teas


def _parse_day(text: str) -> date:
    return _iso_date(text)


def _parse_start(text: str) -> int:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError(f"{text!r} is not a 24-hour HH:MM time")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60


def cmd_check(args, today: date) -> int:
    teas, problems = parse_teas(Path(args.teas).read_text(encoding="utf-8"))
    problems += lint_teas(teas)
    problems.sort(key=lambda p: p.line)
    for problem in problems:
        print(format_problem(problem, args.teas))
    errors = sum(1 for p in problems if p.level == "error")
    warnings = len(problems) - errors
    print(f"{len(teas)} teas loaded, {errors} error(s), {warnings} warning(s)")
    return 1 if errors or (args.strict and warnings) else 0


def cmd_show(args, today: date) -> int:
    teas = _usable_teas(args.teas)
    if teas is None:
        return 1
    tea = find_tea(teas, args.name)
    if tea is None:
        print(f"steepwise: no tea named {args.name!r}", file=sys.stderr)
        return 2
    sessions = load_journal(Path(args.journal))
    start = _parse_start(args.start) if args.start else None
    print(render_show(tea, sessions, today, start))
    return 0


def cmd_list(args, today: date) -> int:
    teas = _usable_teas(args.teas)
    if teas is None:
        return 1
    print(render_list(teas, load_journal(Path(args.journal)), today))
    return 0


def cmd_next(args, today: date) -> int:
    teas = _usable_teas(args.teas)
    if teas is None:
        return 1
    suggestions = suggest_next(teas, load_journal(Path(args.journal)), today, args.cooldown, args.count)
    print(render_suggestions(suggestions))
    return 0


def cmd_log(args, today: date) -> int:
    teas = _usable_teas(args.teas)
    if teas is None:
        return 1
    tea = find_tea(teas, args.name)
    if tea is None:
        print(f"steepwise: no tea named {args.name!r}", file=sys.stderr)
        return 2
    leaf = _number(args.leaf_g) if args.leaf_g else None
    day = args.date or today
    session = make_session(tea, day=day, infusions=args.infusions,
                           rating=args.rating, notes=args.notes, leaf_g=leaf)
    path = Path(args.journal)
    sessions = load_journal(path)
    sessions.append(session)
    save_journal(path, sessions)

    print(f"logged {tea.name} on {day.isoformat()}: {session['infusions']} infusion(s), {session['leaf_g']} g")
    left = stock_left(tea, sessions)
    if left is not None:
        print(f"stock left: {_num(left)} g")
        if left < 0:
            print(f"warning: {tea.name} is over its counted stock by {_num(-left)} g", file=sys.stderr)
    return 0


def cmd_stats(args, today: date) -> int:
    teas = _usable_teas(args.teas)
    if teas is None:
        return 1
    print(render_stats(teas, load_journal(Path(args.journal))))
    return 0


def cmd_export(args, today: date) -> int:
    sessions = load_journal(Path(args.journal))
    text = render_csv(sessions)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {len(sessions)} session(s) to {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steepwise",
        description="Offline tea-brewing planner driven by a plain-text tea file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_today(p):
        p.add_argument("--today", type=_parse_day, help="date to use for shelf life and rotation (YYYY-MM-DD)")

    p = sub.add_parser("check", help="lint a tea file with line-numbered messages")
    p.add_argument("teas")
    p.add_argument("--strict", action="store_true", help="treat warnings as failures")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("show", help="print dose, cost, stock, shelf life and the infusion schedule")
    p.add_argument("teas")
    p.add_argument("name")
    p.add_argument("--journal", default="journal.json", help="journal used for stock (default journal.json)")
    p.add_argument("--start", help="first pour time, HH:MM, to print clock times")
    add_today(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="table of every tea with stock and shelf state")
    p.add_argument("teas")
    p.add_argument("--journal", default="journal.json")
    add_today(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("next", help="suggest what to brew next")
    p.add_argument("teas")
    p.add_argument("--journal", default="journal.json")
    p.add_argument("--cooldown", type=int, default=3, help="days before a tea can repeat (default 3)")
    p.add_argument("--count", type=int, default=3, help="how many suggestions (default 3)")
    add_today(p)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("log", help="record a brewing session in the journal")
    p.add_argument("teas")
    p.add_argument("name")
    p.add_argument("--journal", default="journal.json")
    p.add_argument("--date", type=_parse_day, help="session date (default today)")
    p.add_argument("--infusions", type=int, help="infusions actually brewed")
    p.add_argument("--rating", type=int, help="rating from 1 to 5")
    p.add_argument("--notes", default="")
    p.add_argument("--leaf-g", help="grams used, if different from the file's leaf_g")
    add_today(p)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("stats", help="per-tea totals, average ratings and spend")
    p.add_argument("teas")
    p.add_argument("--journal", default="journal.json")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("export", help="write the journal as CSV")
    p.add_argument("journal", nargs="?", default="journal.json")
    p.add_argument("--out", default="-", help="output path, or - for stdout (default)")
    p.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = getattr(args, "today", None) or date.today()
    try:
        return args.func(args, today)
    except (OSError, JournalError, ValueError) as exc:
        print(f"steepwise: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
