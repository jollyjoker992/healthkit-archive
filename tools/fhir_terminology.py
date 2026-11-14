#!/usr/bin/env python3
"""Write the FHIR terminology files (cli/hka/terminology/) from the mapper's tables and the app's HealthKit catalog.

    python3 tools/fhir_terminology.py           write cli/hka/terminology/*.json (shipped with the CLI)
    python3 tools/fhir_terminology.py --check   exit 1 if the committed files differ (CI)

Sources: cli/hka/fhir.py (METRICS, mapping tables, MAPPING_VERSION) and the type lists in
ios/HKACore/Sources/HKACore/HealthKitSource.swift (HealthCatalog), so the healthkit-type CodeSystem lists
exactly the types the app reads.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cli"))

from hka import fhir  # noqa: E402

SWIFT = REPO / "ios/HKACore/Sources/HKACore/HealthKitSource.swift"


def swift_list(text: str, name: str) -> list[str]:
    m = re.search(rf"static let {name}: \[String\] = \[(.*?)\]", text, re.S)
    if not m:
        raise SystemExit(f"{SWIFT}: no list {name}")
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return re.findall(r'"([^"]+)"', body)


def catalog() -> list[str]:
    """Every type in HealthCatalog, in its order (the Swift `all` list)."""
    text = SWIFT.read_text()
    quantity = ["HKQuantityTypeIdentifier" + n for n in swift_list(text, "quantityIDs")]
    category = ["HKCategoryTypeIdentifier" + n for n in swift_list(text, "categoryIDs")]
    correlation = re.search(r"static let correlationIDs = \[(.*?)\]", text, re.S)
    singles = [re.search(rf'static let {n} = "([^"]+)"', text).group(1)
               for n in ("workout", "ecg", "heartbeats", "audiogram", "stateOfMind", "gad7", "phq9")]
    return quantity + category + re.findall(r'"([^"]+)"', correlation.group(1)) + singles


def render(resource: dict) -> str:
    return json.dumps(resource, indent=1, ensure_ascii=False) + "\n"


def main() -> int:
    check = sys.argv[1:] == ["--check"]
    if sys.argv[1:] not in ([], ["--check"]):
        print(__doc__)
        return 2
    stale = []
    for name, resource in zip(fhir.TERMINOLOGY_FILES, fhir.build_terminology(catalog())):
        path = fhir.TERMINOLOGY_DIR / name
        text = render(resource)
        if check:
            if not path.is_file() or path.read_text() != text:
                stale.append(name)
        else:
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
            print(f"wrote {path.relative_to(REPO)}")
    if stale:
        print("out of date (run python3 tools/fhir_terminology.py): " + ", ".join(stale))
        return 1
    if check:
        print("terminology files match the code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
