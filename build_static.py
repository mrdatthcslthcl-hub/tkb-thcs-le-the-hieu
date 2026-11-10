"""Precompute fast static Zalo Dynamic API replies from the latest school PDF."""

from datetime import date, datetime
from html import unescape
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import Request, urlopen

from render_tkb import render_class_png, render_teacher_png
from tkb_service import (PAGE_URL, fetch_latest, fetch_pdf_url, parse_pdf, response,
                         teacher_display_name, teacher_response,
                         teacher_schedules, teacher_slug, unresolved_composite_lessons,
                         unresolved_response)


OUTPUT = Path(__file__).with_name("public")
MIRROR_URL = "https://tkb-thcs-le-the-hieu.web.simai.vn/tkb.json"
DRIVE_FOLDER_ID = "1vMxNBQ635rnxOiyUjJKW3vADy9QoTMku"
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "mrdatthcslthcl-hub/tkb-thcs-le-the-hieu")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
IMAGE_BASE_URL = os.environ.get(
    "IMAGE_BASE_URL",
    f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}/public",
).rstrip("/")


def load_recent_mirror() -> dict:
    with urlopen(MIRROR_URL, timeout=20) as response:
        snapshot = json.load(response)
    start = datetime.strptime(snapshot["start_date"], "%d/%m/%Y").date()
    if not 0 <= (date.today() - start).days <= 7:
        raise ValueError("External timetable copy is not from this week")
    if len(snapshot["classes"]) < 15 or not snapshot["pdf_url"].startswith("https://thcscamchinhcl.quangtri.edu.vn/"):
        raise ValueError("External timetable copy is incomplete")
    return snapshot


def load_drive_pdf() -> dict:
    """Read a shared original PDF when the school server blocks the runner."""
    listing_url = f"https://drive.google.com/embeddedfolderview?id={DRIVE_FOLDER_ID}#list"
    with urlopen(listing_url, timeout=20) as response:
        listing = response.read().decode("utf-8", errors="replace")
    matches = re.findall(
        r'href="https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)/view[^\"]*".*?'
        r'<div class="flip-entry-title">([^<]+)</div>',
        listing, re.DOTALL,
    )
    candidates = []
    for file_id, raw_name in matches:
        name = unescape(raw_name)
        if not name.lower().endswith(".pdf"):
            continue
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
                pdf = response.read(10_000_001)
            if len(pdf) > 10_000_000 or not pdf.startswith(b"%PDF-"):
                raise ValueError("Invalid or oversized PDF")
            with tempfile.NamedTemporaryFile(suffix=".pdf") as temporary:
                temporary.write(pdf)
                temporary.flush()
                content = subprocess.check_output(
                    ["pdftotext", "-layout", temporary.name, "-"], timeout=20,
                ).decode("utf-8", errors="replace")
            heading = re.search(r"TUẦN\s+(\d+)\s+TỪ\s+NGÀY\s+(\d{1,2}/\d{1,2}/\d{4})", content, re.I)
            if not heading:
                raise ValueError("PDF has no readable timetable heading")
            title = f"Thời khóa biểu tuần {heading.group(1)} - Từ {heading.group(2)}"
            snapshot = parse_pdf(pdf, url, title)
            start = datetime.strptime(snapshot["start_date"], "%d/%m/%Y").date()
            if -4 <= (date.today() - start).days <= 13:
                candidates.append((start, snapshot))
        except Exception as exc:
            print(f"Drive PDF {name} cannot be parsed: {exc}")
    if not candidates:
        raise ValueError("No recent, readable PDF in the shared Drive folder")
    return max(candidates, key=lambda item: item[0])[1]


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def unavailable_teacher_response(name: str) -> dict:
    return {"version": "chatbot", "content": {"messages": [{
        "type": "text", "text": f"Chưa xác nhận được lịch dạy của GV {teacher_display_name(name)} trong TKB hiện tại. Vui lòng xem PDF trên trang trường.",
    }]}}


def image_response(title: str, image_url: str) -> dict:
    """Keep a clickable fallback if Zalo cannot preview the image URL."""
    return {"version": "chatbot", "content": {"messages": [
        {"type": "text", "text": title,
         "buttons": [{"name": "Mở ảnh TKB", "type": "url", "url": image_url},
                     {"name": "Xem TKB chung", "type": "url", "url": PAGE_URL}]},
        {"type": "image", "image_url": image_url, "caption": title},
    ]}}


def main() -> None:
    try:
        pdf, pdf_url, title = fetch_latest()
        snapshot = parse_pdf(pdf, pdf_url, title)
        source_mode = "school"
    except Exception as exc:
        print(f"School timetable unavailable: {exc}; trying the shared Drive folder")
        try:
            snapshot = load_drive_pdf()
            source_mode = "drive_pdf"
        except Exception as drive_exc:
            print(f"Drive PDF unavailable: {drive_exc}; trying the published copy")
            mirror = load_recent_mirror()
            try:
                pdf = fetch_pdf_url(mirror["pdf_url"])
                snapshot = parse_pdf(pdf, mirror["pdf_url"], mirror["title"])
                source_mode = "published_pdf"
            except Exception as pdf_exc:
                print(f"School PDF unavailable: {pdf_exc}; using the published data")
                snapshot = mirror
                source_mode = "published_copy"
    teachers = teacher_schedules(snapshot)
    slugs = [teacher_slug(name) for name in teachers]
    if any(not slug for slug in slugs) or len(slugs) != len(set(slugs)):
        raise ValueError("Teacher names produce empty or duplicate URL segments")
    OUTPUT.mkdir(exist_ok=True)
    start = datetime.strptime(snapshot["start_date"], "%d/%m/%Y").date()
    image_folder = f"anh/{start.isoformat()}-tuan-{snapshot['week']}"
    images_dir = OUTPUT / image_folder
    fresh = 0 <= (date.today() - start).days <= 13
    for code in sorted(snapshot["classes"]):
        if fresh:
            relative = f"{image_folder}/lop/{code}.png"
            render_class_png(snapshot, code, OUTPUT / relative)
            write_json(OUTPUT / f"{code}.json", image_response(
                f"TKB lớp {code} — tuần {snapshot['week']}, từ {snapshot['start_date']}",
                f"{IMAGE_BASE_URL}/{relative}",
            ))
        else:
            write_json(OUTPUT / f"{code}.json", response(snapshot, code))
        for day in range(2, 8):
            write_json(OUTPUT / f"{code}-thu{day}.json", response(snapshot, code, str(day)))
    teacher_dir = OUTPUT / "gv"
    teacher_dir.mkdir(exist_ok=True)
    index_path = teacher_dir / "index.json"
    old_index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    old_names = [item["name"] for item in old_index.get("teachers", [])]
    same_source = (old_index.get("source") == snapshot["pdf_url"]
                   and old_index.get("starts") == snapshot["start_date"])
    if teachers:
        for name in sorted(set(old_names) | set(teachers)):
            slug = teacher_slug(name)
            if name in teachers and fresh:
                relative = f"{image_folder}/gv/{slug}.png"
                render_teacher_png(snapshot, name, teacher_display_name(name),
                                   teachers[name], OUTPUT / relative)
                write_json(teacher_dir / f"{slug}.json", image_response(
                    f"Lịch dạy GV {teacher_display_name(name)} — tuần {snapshot['week']}, từ {snapshot['start_date']}",
                    f"{IMAGE_BASE_URL}/{relative}",
                ))
            else:
                write_json(teacher_dir / f"{slug}.json",
                           teacher_response(snapshot, name) if name in teachers else unavailable_teacher_response(name))
            for day in range(2, 8):
                write_json(teacher_dir / f"{slug}-thu{day}.json",
                           teacher_response(snapshot, name, str(day)) if name in teachers else unavailable_teacher_response(name))
        write_json(index_path, {
            "week": snapshot["week"], "starts": snapshot["start_date"], "source": snapshot["pdf_url"],
            "unresolved_composite_path": "gv/chua-xac-nhan.json",
            "unresolved_count": len(unresolved_composite_lessons(snapshot)),
            "teachers": [{"name": name, "full_name": teacher_display_name(name),
                          "path": f"gv/{teacher_slug(name)}.json"}
                         for name in sorted(teachers)],
        })
        write_json(teacher_dir / "chua-xac-nhan.json", unresolved_response(snapshot))
        teacher_count = len(teachers)
        unresolved_count = len(unresolved_composite_lessons(snapshot))
    elif same_source:
        # The published snapshot predates teacher indexing, but is the same PDF.
        teacher_count = len(old_names)
        unresolved_count = old_index.get("unresolved_count", 0)
        print("Teacher data retained from the same PDF")
    else:
        # Preserve class lookup while withholding teacher schedules from another week.
        for name in old_names:
            slug = teacher_slug(name)
            for suffix in ["", *(f"-thu{day}" for day in range(2, 8))]:
                write_json(teacher_dir / f"{slug}{suffix}.json", unavailable_teacher_response(name))
        write_json(index_path, {"week": snapshot["week"], "starts": snapshot["start_date"],
                                "source": snapshot["pdf_url"], "teachers": []})
        write_json(teacher_dir / "chua-xac-nhan.json", unavailable_teacher_response("các tiết ghép"))
        teacher_count = 0
        unresolved_count = 0
        print("::warning::No teacher data in source snapshot; class responses remain available")
    # This changes once a week even during holidays, keeping the scheduled
    # workflow active while avoiding a Render deploy every day.
    year, week, _ = date.today().isocalendar()
    write_json(OUTPUT / "status.json", {
        "week": snapshot["week"],
        "starts": snapshot["start_date"],
        "source": snapshot["pdf_url"],
        "update_source": source_mode,
        "checked_iso_week": f"{year}-{week:02d}",
        "classes": sorted(snapshot["classes"]),
        "teacher_count": teacher_count,
        "unresolved_composite_count": unresolved_count,
        "image_base_url": IMAGE_BASE_URL,
    })
    print(f"Prepared {len(snapshot['classes'])} classes for timetable week {snapshot['week']}")


if __name__ == "__main__":
    main()
