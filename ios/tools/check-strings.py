#!/usr/bin/env python3
"""Check that every localizable string in the app has a translation for every language.

The compiler lists each localizable string it finds in `*.stringsdata` files (build setting
SWIFT_EMIT_LOC_STRINGS). Build the app first, then:

    python3 ios/tools/check-strings.py <derived data path>

Exits 1 when a string is missing from App/Localizable.xcstrings or lacks a translation, and lists
catalog entries no longer used by the code.
"""
import glob
import json
import pathlib
import sys

LANGUAGES = ["vi"]
here = pathlib.Path(__file__).resolve().parent.parent
catalog = json.loads((here / "App/Localizable.xcstrings").read_text())["strings"]
files = glob.glob(f"{sys.argv[1]}/Build/Intermediates.noindex/HealthBackup.build/*/HealthBackup.build/Objects-normal/*/*.stringsdata")
if not files:
    sys.exit("no .stringsdata found: build the app with that derived data path first")
used = {e["key"] for f in files for table in json.load(open(f))["tables"].values() for e in table}

problems = 0
for key in sorted(used):
    entry = catalog.get(key)
    for lang in LANGUAGES:
        if not entry or lang not in entry.get("localizations", {}):
            print(f"missing {lang}: {key!r}")
            problems += 1
for key in sorted(set(catalog) - used):
    print(f"unused: {key!r}")
print(f"{len(used)} strings, {problems} missing")
sys.exit(1 if problems else 0)
