# Steepwise

**An offline tea-brewing planner that turns one plain-text tea file into exact doses, timed infusion schedules, costs and stock counts, with line-numbered errors.**

Steepwise reads a readable `teas.txt` where each tea is a `[section]` of `key = value` lines. It checks the file, plans how long each infusion should steep, works out the dose ratio and the cost of a brew to the cent, counts down your stock as you log sessions, flags tea past its shelf life, and suggests what to brew next. It uses only the Python standard library, with no servers, no accounts, no API keys and no network calls.

## Features

- **Plain-text tea file** with one `[Tea name]` section per tea, easy to edit in any editor.
- **Line-numbered checks** for missing settings, bad numbers, unknown types, duplicate teas, duplicate keys and stray lines. One broken section never hides the rest.
- **Lint warnings** when a tea's temperature, first steep or dose falls outside the usual range for its type. Add `--strict` to fail CI on warnings.
- **Exact dose ratio** in grams per 100 ml, computed with fractions so nothing rounds away.
- **Brew cost to the cent**, rounded half-up from the price per 100 g.
- **Infusion schedule** where each steep lengthens by a set number of seconds, with pour and finish times for each infusion.
- **Clock-stamped brewing** via `show --start HH:MM`, so you can follow the timings on a real day.
- **Stock tracking** that counts down from the counted amount, using only sessions logged on or after the count date. It warns when you go over.
- **Shelf-life states** (`fresh`, `use soon`, `past shelf life`) from the opened date and a per-type limit.
- **Brewing journal** in one JSON file, with date, grams, infusions, rating and notes for each session.
- **Per-tea stats** showing sessions, infusions, grams used, average rating and spend, plus totals.
- **"What next?" suggestions** that rank teas by average rating with a staleness bonus, skipping anything out of stock, past shelf life or brewed too recently.
- **CSV export** of the journal, for a spreadsheet or another tool.
- **Deterministic dates**: `--today` lets you reproduce any shelf or rotation answer exactly.

## Quick start

Clone the repo, then from its folder run these against the included example files. The `--today` date keeps the shelf and rotation answers the same every time you run them.

```sh
# 1. Check the example tea file (exit 0 means no errors)
python3 steepwise.py check examples/teas.txt

# 2. See the plan for one tea, with clock times from 07:30
python3 steepwise.py show examples/teas.txt "Dragon Well" --journal examples/journal.json --start 07:30 --today 2026-10-18

# 3. Log a session. This writes examples/journal.json and deducts the leaf from stock
python3 steepwise.py log examples/teas.txt "Dragon Well" --journal examples/journal.json --date 2026-10-10 --rating 4

# 4. Stock, shelf life and totals across all teas
python3 steepwise.py list examples/teas.txt --journal examples/journal.json --today 2026-10-18
python3 steepwise.py stats examples/teas.txt --journal examples/journal.json

# 5. What should I brew next?
python3 steepwise.py next examples/teas.txt --journal examples/journal.json --today 2026-10-18

# 6. See line-numbered errors on a deliberately broken file
python3 steepwise.py check examples/broken.txt
```

Here is the file format, from `examples/teas.txt`:

```ini
[Dragon Well]
type = green
leaf_g = 3
water_ml = 150
temp_c = 80
steep_s = 45
max_steeps = 3
price_per_100g = 11.90
stock_g = 40
counted = 2026-08-15
opened = 2026-08-15
```

Required settings are `type`, `leaf_g`, `water_ml`, `temp_c` and `steep_s`. Optional settings are `extend_s` (default 15), `max_steeps` (default 4), `price_per_100g`, `stock_g` (which needs `counted`) and `opened`. Types are `green`, `white`, `oolong`, `black`, `puerh` and `herbal`. Lines starting with `#` are comments.

## How to run

Requires Python 3 and nothing else. No install step is needed.

```sh
python3 steepwise.py <command> ...
```

| Command | What it does |
| --- | --- |
| `check FILE [--strict]` | Lint the tea file. Exit 0 if clean, 1 on errors (or on warnings with `--strict`). |
| `show FILE NAME [--start HH:MM] [--journal J] [--today D]` | Dose, cost, stock, shelf life and infusion schedule for one tea. |
| `list FILE [--journal J] [--today D]` | Table of every tea with stock and shelf state. |
| `next FILE [--journal J] [--today D] [--cooldown N] [--count N]` | Suggest what to brew next. |
| `log FILE NAME [--journal J] [--date D] [--infusions N] [--rating 1-5] [--notes T] [--leaf-g G]` | Record a session in the journal. |
| `stats FILE [--journal J]` | Per-tea totals, average ratings and spend. |
| `export [JOURNAL] [--out PATH]` | Write the journal as CSV to stdout, or to a file with `--out`. |

Exit codes: `0` success, `1` the tea file has errors (or warnings under `--strict`), `2` a usage problem such as an unknown tea name, a bad journal or a missing file.

## How to test

Install pytest if you don't have it, then run the suite from the project folder:

```sh
python3 -m pytest -q
```

The tests in `tests/test_steepwise.py` cover the parser and its line-numbered errors, the infusion schedule, exact ratios, cent-accurate costs, lint warnings, stock and shelf-life rules, journal read and write, the rotation suggestions, CSV export and the command-line exit codes. They use only temporary directories and fixed dates, so they never depend on today's date or the network.

## Project layout

```
steepwise.py              the whole tool: parser, planner, journal and CLI
examples/teas.txt         a working tea file with seven teas
examples/broken.txt       a tea file with deliberate errors for check
tests/test_steepwise.py   pytest suite
tests/conftest.py         puts the project root on the import path
```

## Notes

- Guidance ranges for temperature, steep time, dose and shelf life are starting points in the `PROFILES` table at the top of `steepwise.py`. Edit them to match the teas you drink; they only produce warnings and never block a brew.
- Stock is only deducted for sessions dated on or after a tea's `counted` date, so a session logged before you counted the tin does not reduce it.
