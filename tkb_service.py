"""Read the school's weekly PDF and serve Zalo OA Dynamic API responses.

Requires Python 3 and Poppler's pdftotext. Run `sync` from a scheduler; requests
only read the verified local snapshot, so the OA endpoint responds quickly.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import urlopen
from xml.etree import ElementTree
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PAGE_URL = "https://thcscamchinhcl.quangtri.edu.vn/thoi-khoa-bieu"
SOURCE_HOST = "thcscamchinhcl.quangtri.edu.vn"
INTERMEDIATE_CA = Path(__file__).with_name("alphassl2025.pem")
CLASS_RE = re.compile(r"^[6789][AB][123]-")
CLASS_INPUT_RE = re.compile(r"^[6789][AB][123]$")
WEEK_RE = re.compile(r"TUẦN\s+(\d+).*?(\d{2}/\d{2}/\d{4})", re.I)

# Professional assignments effective 12/10/2026, supplied by the school.
# Entries for a combined subject are candidates for a "..." cell; the PDF does
# not identify which subsubject/teacher actually teaches that specific week.
COMPOSITE_TEACHERS = {
    "LSĐL": {
        "Trường Chính": {"6": ("Kiếm",), "7": ("Kiếm", "P.Hải"),
                         "8": ("Hân", "Hằng"), "9": ("Hân", "Hằng")},
        "An Sơn": {"6": ("Hà", "Minh"), "7": ("Bình", "Minh"),
                   "8": ("Hà", "Minh"), "9": ("Bình", "Minh")},
    },
    "KHTN": {
        "Trường Chính": {"6": ("Vân", "Loan", "Nhàn"),
                         "7": ("Vân", "Toàn", "Nhàn"),
                         "8": ("Vân", "Loan", "Nhàn"),
                         "9": ("Vân", "Toàn", "Nhàn")},
        "An Sơn": {grade: ("Lan", "Thông", "Hiền") for grade in "6789"},
    },
}

# Full names from the school's professional assignment dated 12/10/2026.
TEACHER_FULL_NAMES = {
    "Anh": "Nguyễn Nhật Anh", "Bình": "Nguyễn Thanh Bình",
    "Chiến": "Hồ Xuân Chiến", "Chung": "Nguyễn Thành Chung",
    "Giang": "Hoàng Thị Hương Giang", "H.Yến": "Phan Thị Hải Yến",
    "Hiền": "Nguyễn Thị Thu Hiền", "Huệ": "Nguyễn Kim Huệ",
    "Hà": "Nguyễn Thanh Hà", "Hân": "Lê Thị Ngọc Hân",
    "Hòa": "Nguyễn Đức Hòa", "Hùng": "Lê Mậu Hùng",
    "Hương": "Nguyễn Thị Hương", "Hậu": "Trần Thị Thanh Hậu",
    "Hằng": "Lê Thị Thuý Hằng", "Khương": "Trần Hữu Khương",
    "Kiếm": "Nguyễn Đình Kiếm", "Lan": "Trần Thị Nhị Lan",
    "Loan": "Nguyễn Thị Loan", "Long": "Trần Xuân Long",
    "Luyện": "Lê Thị Luyện", "Ly": "Trần Thị Mai Ly",
    "M.Nhã": "Nguyễn Minh Nhã", "Minh": "Trần Thị Hồng Minh",
    "Nghĩa": "Nguyễn Thị Nghĩa", "Nhung": "Đoàn Thị Kim Nhung",
    "Nhàn": "Phan Thị Thanh Nhàn", "P.Hải": "Lê Phước Hải",
    "Phước": "Nguyễn Ngọc Phước", "Phượng — An Sơn": "Nguyễn Thị Phượng",
    "Phượng — Trường Chính": "Phạm Thị Kim Phượng",
    "Quyên": "Trần Thị Quyên", "Quân": "Lê Hải Quân",
    "T.Nhã": "Phan Thục Nhã", "Thiện": "Nguyễn Võ Thiện",
    "Thông": "Trương Thị Thông", "Toàn": "Trần Thị Toàn",
    "Trung": "Trần Kiên Trung", "Tỉnh": "Ngô Văn Tỉnh",
    "Tứ": "Lê Văn Tứ", "Vân": "Nguyễn Thị Hải Vân",
    "Vượng": "Nguyễn Quốc Vượng", "Đ.Hải": "Nguyễn Đức Hải",
    "Đ.Yến": "Đoàn Thị Yến", "Đạt": "Phan Thế Đạt",
}


def teacher_display_name(key: str) -> str:
    if key in TEACHER_FULL_NAMES:
        return TEACHER_FULL_NAMES[key]
    if " — " in key:
        short, campus = key.rsplit(" — ", 1)
        if short in TEACHER_FULL_NAMES:
            return f"{TEACHER_FULL_NAMES[short]} — {campus}"
    return key


def teacher_slug(name: str) -> str:
    """A stable URL segment for a teacher name printed in the source PDF."""
    normalized = unicodedata.normalize("NFD", name.replace("Đ", "D").replace("đ", "d"))
    ascii_name = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def subject_and_teacher(token: str) -> tuple[str, str | None]:
    """Preserve the existing subject display; ignore redacted teacher cells."""
    if token.startswith("GDĐP."):
        subject, teacher = "GDĐP", token.split(".", 1)[1]
    elif "-" in token:
        subject, teacher = token.split("-", 1)
    else:
        return token.strip(), None
    # Some school PDFs export Vietnamese ư as the legacy ƣ glyph.
    teacher = teacher.strip().replace("ƣ", "ư").replace("Ƣ", "Ư")
    if not teacher or "..." in teacher or "…" in teacher:
        return subject.strip(), None
    return subject.strip(), teacher


class FirstPDF(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.in_body = False
        self.in_first_row = False
        self.pdf_href = None
        self.title_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and "list-pdf-table" in a.get("class", ""):
            self.in_table = True
        elif self.in_table and tag == "tbody":
            self.in_body = True
        elif self.in_body and tag == "tr" and self.pdf_href is None:
            self.in_first_row = True
        elif self.in_first_row and tag == "a":
            href = a.get("href", "")
            if urlparse(href).path.lower().endswith(".pdf"):
                self.pdf_href = href

    def handle_endtag(self, tag):
        if tag == "tr":
            self.in_first_row = False
        elif tag == "tbody":
            self.in_body = False

    def handle_data(self, data):
        if self.in_first_row:
            self.title_parts.append(data.strip())


def fetch_latest() -> tuple[bytes, str, str]:
    context = ssl.create_default_context()
    if INTERMEDIATE_CA.exists():
        # The school currently omits this public intermediate certificate.
        # Keep hostname/signature verification enabled while completing chain.
        context.load_verify_locations(cafile=str(INTERMEDIATE_CA))
    with urlopen(PAGE_URL, timeout=15, context=context) as response:
        page = response.read(2_000_001)
    if len(page) > 2_000_000:
        raise ValueError("Listing page is unexpectedly large")
    parser = FirstPDF()
    parser.feed(page.decode("utf-8", errors="replace"))
    if not parser.pdf_href:
        raise ValueError("No PDF in first timetable row")
    pdf_url = urljoin(PAGE_URL, parser.pdf_href)
    if urlparse(pdf_url).hostname != SOURCE_HOST:
        raise ValueError("PDF points outside the school site")
    return fetch_pdf_url(pdf_url, context), pdf_url, " ".join(x for x in parser.title_parts if x)


def fetch_pdf_url(pdf_url: str, context=None) -> bytes:
    """Fetch a school PDF; also used when the listing page is unavailable."""
    if urlparse(pdf_url).hostname != SOURCE_HOST or not urlparse(pdf_url).path.lower().endswith(".pdf"):
        raise ValueError("PDF points outside the school site")
    if context is None:
        context = ssl.create_default_context()
        if INTERMEDIATE_CA.exists():
            context.load_verify_locations(cafile=str(INTERMEDIATE_CA))
    with urlopen(pdf_url, timeout=20, context=context) as response:
        pdf = response.read(10_000_001)
    if len(pdf) > 10_000_000 or not pdf.startswith(b"%PDF-"):
        raise ValueError("Invalid or oversized PDF")
    return pdf


def words_by_page(pdf: bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fp:
        fp.write(pdf)
        fp.flush()
        raw = subprocess.check_output(["pdftotext", "-bbox", fp.name, "-"], timeout=20)
    xml = re.sub(rb"<!DOCTYPE[^>]*>", b"", raw)
    root = ElementTree.fromstring(xml)
    ns = "{http://www.w3.org/1999/xhtml}"
    for page in root.findall(f".//{ns}page"):
        yield [
            (float(w.get("xMin")), float(w.get("yMin")), html.unescape(w.text or ""))
            for w in page.findall(f"{ns}word")
        ]


def parse_pdf(pdf: bytes, pdf_url: str, title: str) -> dict:
    match = re.search(r"tuần\s+(\d+).*?từ\s+(\d{1,2}/\d{1,2}/\d{4})", title, re.I)
    if not match:
        raise ValueError("Timetable title has no week and start date")
    week = int(match.group(1))
    start_date = datetime.strptime(match.group(2), "%d/%m/%Y").strftime("%d/%m/%Y")
    parsed: dict[str, dict] = {}
    for page_no, words in enumerate(words_by_page(pdf)):
        headers = sorted((x, t.split("-", 1)[0]) for x, y, t in words if 75 < y < 116 and CLASS_RE.match(t))
        days = {int(t): y for x, y, t in words if 70 < x < 85 and t in {"2", "3", "4", "5", "6", "7"}}
        periods = sorted((y, int(t)) for x, y, t in words if 130 < x < 145 and y > 100 and t in {"1", "2", "3", "4", "5"})
        if not headers or set(days) != set(range(2, 8)) or len(periods) < 25:
            raise ValueError(f"Page {page_no + 1}: timetable grid changed")
        starts = {}
        for day in range(2, 7):
            candidates = [y for y, period in periods if period == 1 and days[day] - 45 < y < days[day] - 25]
            if len(candidates) != 1:
                raise ValueError(f"Page {page_no + 1}: missing start of day {day}")
            starts[day] = candidates[0]
        # The Friday afternoon third period sits about 27 pt above the
        # Saturday label. A 35 pt cutoff drops that row for every class.
        starts[7] = days[7] - 20
        campus = "An Sơn" if any(t in {"SƠN,", "SƠN"} for _, y, t in words if y < 85) else "Trường Chính"
        for _, code in headers:
            if code in parsed:
                raise ValueError(f"Duplicate class {code}")
            parsed[code] = {"campus": campus, "days": {str(d): [] for d in range(2, 8)}}
        for day in range(2, 7):
            day_periods = [(y, period) for y, period in periods if starts[day] <= y < starts[day + 1]]
            session = "Sáng"
            previous = 0
            for y, period in day_periods:
                if period < previous:
                    session = "Chiều"
                previous = period
                cells: dict[str, list[tuple[float, str]]] = {code: [] for _, code in headers}
                for x, wy, token in words:
                    if x < 150 or abs(wy - y) > 5.3 or ("-" not in token and not token.startswith("GDĐP.")):
                        continue
                    code = min(headers, key=lambda item: abs(item[0] - x))[1]
                    cells[code].append((x, token))
                # The merged activity rows between teaching days contain no
                # class subjects and must not be treated as lessons.
                if sum(bool(cell) for cell in cells.values()) < 2:
                    continue
                for code, cell in cells.items():
                    if not cell:
                        continue
                    assignments = [subject_and_teacher(token) for _, token in sorted(cell)]
                    subjects = [subject for subject, _ in assignments]
                    subject = " / ".join(s for s in subjects if s)
                    if subject:
                        parsed[code]["days"][str(day)].append({
                            "session": session, "period": period, "subject": subject,
                            "teacher_assignments": [
                                {"subject": part, "teacher": teacher}
                                for part, teacher in assignments if part and teacher
                            ],
                        })
    if not start_date or len(parsed) < 15:
        raise ValueError("PDF has no complete timetable")
    if any(sum(map(len, item["days"].values())) < 15 for item in parsed.values()):
        raise ValueError("At least one class has too few lessons")
    return {"week": week, "start_date": start_date, "title": title, "pdf_url": pdf_url, "classes": parsed}


def teacher_schedules(snapshot: dict) -> dict[str, list[dict]]:
    """Index named lessons and candidate combined lessons separately."""
    if not any("teacher_assignments" in lesson
               for entry in snapshot["classes"].values()
               for lessons in entry["days"].values() for lesson in lessons):
        return {}  # Old published snapshots do not contain teacher metadata.
    raw_schedules: dict[str, list[dict]] = {}
    for code, entry in snapshot["classes"].items():
        for day, lessons in entry["days"].items():
            for lesson in lessons:
                for assignment in lesson.get("teacher_assignments", []):
                    name = assignment["teacher"]
                    raw_schedules.setdefault(name, []).append({
                        "day": day, "session": lesson["session"],
                        "period": lesson["period"], "class": code,
                        "campus": entry["campus"], "subject": assignment["subject"],
                        "kind": "named", "pdf_teacher": name,
                    })
    schedules: dict[str, list[dict]] = {}
    for name, items in raw_schedules.items():
        if name == "Phượng" and len({item["campus"] for item in items}) > 1:
            # The assignment sheet identifies two different teachers named Phượng.
            for campus in sorted({item["campus"] for item in items}):
                schedules[f"{name} — {campus}"] = [item for item in items if item["campus"] == campus]
        else:
            # A/B class codes already identify the school site for one teacher.
            schedules[name] = items
    for lesson in unresolved_composite_lessons(snapshot):
        candidates = COMPOSITE_TEACHERS.get(lesson["subject"], {}).get(
            lesson["campus"], {}).get(lesson["class"][0], ())
        for name in candidates:
            schedule_key = name if name in schedules else f"{name} — {lesson['campus']}"
            if schedule_key not in schedules:
                continue  # A changed staff roster needs explicit verification.
            schedules[schedule_key].append({**lesson, "kind": "composite", "pdf_teacher": "..."})
    return schedules


def unresolved_composite_lessons(snapshot: dict) -> list[dict]:
    """Periods whose combined subject is known but teacher/subsubject is not."""
    unresolved = []
    for code, entry in snapshot["classes"].items():
        for day, lessons in entry["days"].items():
            for lesson in lessons:
                if lesson["subject"] in {"LSĐL", "KHTN"} and not lesson.get("teacher_assignments"):
                    unresolved.append({"class": code, "campus": entry["campus"], "day": day,
                                       "session": lesson["session"], "period": lesson["period"],
                                       "subject": lesson["subject"]})
    return sorted(unresolved, key=lambda x: (int(x["day"]), x["session"] != "Sáng", x["period"], x["class"]))


def unresolved_response(snapshot: dict) -> dict:
    items = unresolved_composite_lessons(snapshot)
    lines = [f"Tiết ghép phân môn theo thông báo GV — tuần {snapshot['week']}, từ {snapshot['start_date']}"]
    for item in items:
        lines.append(f"Thứ {item['day']} {item['session'].lower()} tiết {item['period']}: {item['subject']} — {item['class']}")
    if not items:
        lines.append("Không có tiết ghép chưa rõ giáo viên trong PDF hiện tại.")
    lines.append("Những tiết này được tính cho các GV phân môn theo bảng phân công; GV trực tiếp dạy trong tuần theo thông báo của nhà trường/GV.")
    return {"version": "chatbot", "content": {"messages": [{"type": "text", "text": "\n".join(lines)}]}}


def teacher_response(snapshot: dict, teacher: str, day: str | None = None) -> dict:
    schedules = teacher_schedules(snapshot)
    start = datetime.strptime(snapshot["start_date"], "%d/%m/%Y").date()
    if (date.today() - start).days > 13:
        message = "Chưa xác nhận được thời khóa biểu mới. Anh/chị vui lòng xem tệp mới nhất trên trang trường."
    elif teacher not in schedules:
        message = "Em chưa tìm thấy tên giáo viên này trong thời khóa biểu. Anh/chị kiểm tra lại tên trên PDF gốc."
    else:
        chosen = [item for item in schedules[teacher] if day is None or item["day"] == day]
        chosen.sort(key=lambda item: (int(item["day"]), item["session"] != "Sáng", item["period"], item["class"]))
        lines = [f"Lịch dạy GV {teacher_display_name(teacher)}\nTuần {snapshot['week']}, từ {snapshot['start_date']}"]
        composite_count = sum(item["kind"] == "composite" for item in chosen)
        if not chosen:
            lines.append(f"Thứ {day}: không có tiết dạy trong TKB.")
        else:
            last_day = None
            named_at_slot = {
                (item["day"], item["session"], item["period"]): item["class"]
                for item in schedules[teacher] if item["kind"] == "named"
            }
            for item in chosen:
                if item["day"] != last_day:
                    last_day = item["day"]
                    lines.append(f"Thứ {last_day}:")
                suffix = ""
                if item["kind"] == "composite":
                    clash = named_at_slot.get((item["day"], item["session"], item["period"]))
                    suffix = (f" (tiết ghép; trùng tiết ghi tên ở {clash}, cần xác nhận)" if clash and clash != item["class"]
                              else " (tiết ghép, GV thực dạy theo thông báo)")
                lines.append(f"{item['session']} tiết {item['period']}: {item['subject']}-{item['pdf_teacher']} — {item['class']}{suffix}")
        if composite_count:
            lines.append("Tiết ghép được thống kê cho các GV phân môn liên quan; đây chưa phải xác nhận ai trực tiếp dạy tuần này.")
        message = "\n".join(lines)
    return {
        "version": "chatbot",
        "content": {"messages": [{"type": "text", "text": message, "buttons": [{"name": "Xem TKB chung", "type": "url", "url": PAGE_URL}]}]},
    }


def response(snapshot: dict, class_code: str, day: str | None = None) -> dict:
    class_code = class_code.strip().upper()
    start = datetime.strptime(snapshot["start_date"], "%d/%m/%Y").date()
    if (date.today() - start).days > 13:
        message = "Chưa xác nhận được thời khóa biểu mới. Anh/chị vui lòng xem tệp mới nhất trên trang trường."
    elif not CLASS_INPUT_RE.fullmatch(class_code) or class_code not in snapshot["classes"]:
        message = "Em chưa tìm thấy lớp này trong thời khóa biểu mới nhất. Anh/chị kiểm tra lại mã lớp (ví dụ 7A2) hoặc xem tệp gốc."
    else:
        entry = snapshot["classes"][class_code]
        heading = f"TKB lớp {class_code}\nTuần {snapshot['week']}, từ {snapshot['start_date']}"
        day_list = [day] if day in {"2", "3", "4", "5", "6", "7"} else [str(d) for d in range(2, 8)]
        lines = [heading]
        for d in day_list:
            items = entry["days"][d]
            if not items:
                continue
            lines.append(f"Thứ {d}:")
            for item in items:
                lines.append(f"{item['session']} tiết {item['period']}: {item['subject']}")
        message = "\n".join(lines)
    return {
        "version": "chatbot",
        "content": {"messages": [{"type": "text", "text": message, "buttons": [{"name": "Xem TKB chung", "type": "url", "url": PAGE_URL}]}]},
    }


def sync(snapshot_path: Path) -> None:
    pdf, pdf_url, title = fetch_latest()
    candidate = parse_pdf(pdf, pdf_url, title)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = snapshot_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, snapshot_path)


def serve(snapshot_path: Path, port: int):
    def refresh_loop():
        while True:
            try:
                sync(snapshot_path)
                print("Refreshed timetable", flush=True)
            except Exception as exc:
                print(f"Timetable refresh failed: {exc}", flush=True)
            time.sleep(3600)

    threading.Thread(target=refresh_loop, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != "/tkb":
                self.send_error(404)
                return
            try:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                payload = response(snapshot, query.get("lop", [""])[0], query.get("thu", [None])[0])
            except Exception:
                self.send_error(503)
                return
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["sync", "parse-file", "serve"])
    parser.add_argument("--snapshot", type=Path, default=Path("tkb.json"))
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--pdf-url", default=PAGE_URL)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    if args.action == "sync":
        sync(args.snapshot)
    elif args.action == "parse-file":
        if args.pdf is None:
            parser.error("--pdf required")
        result = parse_pdf(args.pdf.read_bytes(), args.pdf_url, "Bản kiểm tra")
        args.snapshot.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    else:
        serve(args.snapshot, args.port)
