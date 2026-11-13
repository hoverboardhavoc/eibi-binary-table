# eibi-binary-table

This repository holds the frequency guide for [Havoc Radio](https://github.com/hoverboardhavoc/havoc-radio): the
schedule of what broadcasts on each LW, MW and SW frequency, as a table the radio reads straight from flash. The
radio and its web page fetch the table from here when the user asks for an update.

## Source and credit

The schedule is EiBi's shortwave schedule, compiled by Eike Bierwirth and published at
[http://www.eibispace.de/](http://www.eibispace.de/). EiBi's README says: "any person is absolutely free to
download, use, copy, or distribute these files or to use them within third-party software."

## Contents

| Path | Contents |
|---|---|
| `eibi/sked-<season>.csv` | EiBi's CSV for the current season, byte for byte as served (Latin-1, CRLF). |
| `v1/stations.bin` | The table converted from the CSV, format 1. |
| `v1/manifest.json` | The table's format, season, entry count, length, SHA-256, EiBi's date and the CSV it was made from. |
| `state.json` | The season and the `ETag` and `Last-Modified` sent with the next check, and the time of the last check. |

The table format is defined here, in [FORMAT.md](FORMAT.md), and the table is made by `tools/guide_table.py`.

## The import

The workflow `.github/workflows/import.yml` runs daily at 04:37 UTC and can be started by hand. The CSV for the
season of the UTC date is requested once from eibispace.de, conditionally, so that an unchanged file is answered
with 304 and nothing is downloaded. A new or revised CSV is converted with `tools/guide_table.py` and checked: the
converter must report no errors, the table must hold 1,000 to 30,000 entries and pass the format's checks. Only
then are the CSV, the table, the manifest and `state.json` committed together. When a check fails, nothing is
committed and the last good table stays. When a new season starts, the previous CSV is renamed to the new season's
name in the same commit, so that the file's history runs through the seasons.

The import is `tools/import.py` (Python standard library only), tested by `tools/test_import.py` against excerpts
of EiBi's CSVs with a fake HTTP fetch. The converter is tested by `tools/test_guide_table.py` on EiBi's full B26
and A26 CSVs (`tools/fixtures/full/`); it also writes the tables Havoc Radio's reader tests use to `.test-out/`.

```
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/test_guide_table.py
```
