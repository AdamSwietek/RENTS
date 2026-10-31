#!/usr/bin/env python3
"""Extract the Beverly Hills unit report PDF's positioned text into a flat CSV."""

from __future__ import annotations

import argparse
import bisect
import csv
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


NS = {"x": "http://www.w3.org/1999/xhtml"}

# Left edges of the 17 source columns in the landscape report. Poppler reports
# coordinates in the PDF's original (pre-rotation) coordinate system.
COLUMN_STARTS = [
    51.4,
    99.6,
    147.8,
    196.0,
    244.1,
    292.3,
    340.5,
    388.7,
    436.9,
    485.0,
    533.2,
    581.4,
    629.6,
    677.8,
    726.0,
    774.1,
    822.3,
]

HEADERS = [
    "apn",
    "year_built",
    "total_units",
    "unit",
    "address",
    "property_owner",
    "rent_control_chapter",
    "monthly_rent",
    "maximum_allowable_rent",
    "move_in_date",
    "last_rental_increase_date",
    "original_rent_amount",
    "bathrooms",
    "bedrooms",
    "services_included_with_rent",
    "utilities_served_by_landlord",
    "is_unit_currently_occupied",
    "apn_formatted",
    "jurisdiction",
    "is_test_record",
    "source_page",
    "source_row",
]


def clean_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_date(value: str) -> str:
    value = clean_space(value)
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return value


def normalize_number(value: str) -> str:
    value = clean_space(value).replace("$", "").replace(",", "")
    if not value:
        return ""
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def column_index(x_min: float) -> int | None:
    # Text in each cell starts at or to the right of its column's fixed left edge.
    # Use the greatest start not exceeding the word, with a small PDF tolerance.
    index = bisect.bisect_right(COLUMN_STARTS, x_min + 0.15) - 1
    return index if 0 <= index < len(COLUMN_STARTS) else None


def page_words(page: ET.Element) -> list[tuple[float, float, str]]:
    words = []
    for word in page.findall(".//x:word", NS):
        text = clean_space("".join(word.itertext()))
        if text:
            words.append((float(word.attrib["xMin"]), float(word.attrib["yMin"]), text))
    return words


def extract_rows(bbox_html: Path) -> list[dict[str, str | int]]:
    root = ET.parse(bbox_html).getroot()
    pages = root.findall(".//x:page", NS)
    output: list[dict[str, str | int]] = []

    for page_number, page in enumerate(pages, start=1):
        words = page_words(page)
        # Each record has an APN-like value at the first column's fixed x-position.
        # This deliberately retains the report's three TEST123456 records.
        anchors = sorted(
            y
            for x, y, text in words
            if 45 <= x < 85 and y > 70 and re.fullmatch(r"(?:\d{10}|TEST\d{6})", text)
        )
        if not anchors:
            continue

        for page_row, anchor in enumerate(anchors, start=1):
            row_start = anchor - 12.7
            row_end = anchors[page_row] - 12.7 if page_row < len(anchors) else anchor + 6.8
            cells: list[list[tuple[float, float, str]]] = [[] for _ in COLUMN_STARTS]

            for x, y, text in words:
                if not (row_start <= y < row_end):
                    continue
                index = column_index(x)
                if index is not None:
                    cells[index].append((y, x, text))

            values = []
            for cell in cells:
                cell.sort(key=lambda item: (round(item[0], 2), item[1]))
                values.append(clean_space(" ".join(item[2] for item in cell)))

            row: dict[str, str | int] = dict(zip(HEADERS[:17], values))
            row["move_in_date"] = normalize_date(str(row["move_in_date"]))
            row["last_rental_increase_date"] = normalize_date(str(row["last_rental_increase_date"]))
            for field in ("monthly_rent", "maximum_allowable_rent", "original_rent_amount", "bathrooms", "bedrooms"):
                row[field] = normalize_number(str(row[field]))
            apn = str(row["apn"])
            row["apn_formatted"] = f"{apn[:4]}-{apn[4:7]}-{apn[7:]}" if re.fullmatch(r"\d{10}", apn) else ""
            row["jurisdiction"] = "Beverly Hills"
            row["is_test_record"] = "true" if apn.startswith("TEST") else "false"
            row["source_page"] = page_number
            row["source_row"] = page_row
            output.append(row)

    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("output_csv", type=Path)
    args = parser.parse_args()

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="beverly_unit_report_") as temp_dir:
        bbox_html = Path(temp_dir) / "report.html"
        subprocess.run(
            ["pdftotext", "-bbox-layout", str(args.input_pdf), str(bbox_html)],
            check=True,
        )
        rows = extract_rows(bbox_html)

    if not rows:
        raise RuntimeError("No unit records were extracted")

    with args.output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows):,} records to {args.output_csv}")


if __name__ == "__main__":
    main()
