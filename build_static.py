"""Precompute fast static Zalo Dynamic API replies from the latest school PDF."""

from datetime import date
import json
from pathlib import Path

from tkb_service import fetch_latest, parse_pdf, response


OUTPUT = Path(__file__).with_name("public")


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    pdf, pdf_url, title = fetch_latest()
    snapshot = parse_pdf(pdf, pdf_url, title)
    OUTPUT.mkdir(exist_ok=True)
    for code in sorted(snapshot["classes"]):
        write_json(OUTPUT / f"{code}.json", response(snapshot, code))
        for day in range(2, 8):
            write_json(OUTPUT / f"{code}-thu{day}.json", response(snapshot, code, str(day)))
    # This changes once a week even during holidays, keeping the scheduled
    # workflow active while avoiding a Render deploy every day.
    year, week, _ = date.today().isocalendar()
    write_json(OUTPUT / "status.json", {
        "week": snapshot["week"],
        "starts": snapshot["start_date"],
        "source": snapshot["pdf_url"],
        "checked_iso_week": f"{year}-{week:02d}",
        "classes": sorted(snapshot["classes"]),
    })
    print(f"Prepared {len(snapshot['classes'])} classes for timetable week {snapshot['week']}")


if __name__ == "__main__":
    main()
