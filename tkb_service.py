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
    with urlopen(pdf_url, timeout=20, context=context) as response:
        pdf = response.read(10_000_001)
    if len(pdf) > 10_000_000 or not pdf.startswith(b"%PDF-"):
        raise ValueError("Invalid or oversized PDF")
    return pdf, pdf_url, " ".join(x for x in parser.title_parts if x)


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
    parsed: dict[str, dict] = {}
    week = None
    start_date = None
    for page_no, words in enumerate(words_by_page(pdf)):
        heading = " ".join(t for x, y, t in sorted(words, key=lambda w: (round(w[1] / 5), w[0])) if 60 < y < 100)
        match = WEEK_RE.search(heading)
        if match:
            page_week, page_start = match.groups()
            if week is not None and (week, start_date) != (page_week, page_start):
                raise ValueError("Pages disagree about week")
            week, start_date = page_week, page_start
        headers = sorted((x, t.split("-", 1)[0]) for x, y, t in words if 100 <= y < 116 and CLASS_RE.match(t))
        first_row = sorted(x for x, y, t in words if 125 < x and 115 < y < 132)
        if not headers or len(first_row) != len(headers):
            raise ValueError(f"Page {page_no + 1}: class grid changed")
        starts = first_row
        day_anchors = sorted((y, int(t)) for x, y, t in words if 45 < x < 80 and y > 115 and t in {"2", "3", "4", "5", "6", "7"})
        mornings = sorted(y for x, y, t in words if 75 < x < 105 and y > 115 and t == "Sáng")
        periods = sorted((y, int(t)) for x, y, t in words if 108 < x < 125 and y > 115 and t in {"1", "2", "3", "4", "5"})
        if len(day_anchors) != 6 or len(mornings) != 6 or len(periods) < 20:
            raise ValueError(f"Page {page_no + 1}: timetable rows changed")
        # Day labels are centered in tall merged cells; their y position is not
        # a reliable boundary. Each morning's first period gives the boundary.
        day_starts = []
        for anchor, day in zip(mornings, range(2, 8)):
            candidates = [y for y, period in periods if period == 1 and anchor - 45 < y < anchor]
            if day == 7:
                break  # Saturday is an unnumbered special activity in this PDF.
            if not candidates:
                raise ValueError(f"Page {page_no + 1}: missing morning for day {day}")
            day_starts.append((min(candidates), day))
        if [d for _, d in day_anchors] != list(range(2, 8)):
            raise ValueError(f"Page {page_no + 1}: day labels changed")
        period_rows = []
        for index, (start, day) in enumerate(day_starts):
            end = day_starts[index + 1][0] if index + 1 < len(day_starts) else mornings[-1] - 15
            previous_period = 0
            session = "Sáng"
            for y, period in periods:
                if start <= y < end:
                    if period < previous_period:
                        session = "Chiều"
                    period_rows.append((y, period, day, session))
                    previous_period = period
        campus = "An Sơn" if any(t == "SƠN," for _, y, t in words if y < 100) else "Trường Chính"
        for i, (_, class_code) in enumerate(headers):
            if class_code in parsed:
                raise ValueError(f"Duplicate class {class_code}")
            parsed[class_code] = {"campus": campus, "days": {str(d): [] for d in range(2, 8)}}
            for row_index, (y, period, day, session) in enumerate(period_rows):
                next_y = period_rows[row_index + 1][0] if row_index + 1 < len(period_rows) else y + 16
                cell = sorted((wy, x, t) for x, wy, t in words if x > 125 and y - 1.5 <= wy < next_y - 1.5 and i == min(range(len(starts)), key=lambda j: abs(starts[j] - x)))
                lines: list[list[str]] = []
                line_y = None
                for wy, _, token in cell:
                    if line_y is None or abs(wy - line_y) > 2:
                        lines.append([])
                        line_y = wy
                    lines[-1].append(token)
                subject = " / ".join(" ".join(line) for line in lines).strip()
                if subject:
                    parsed[class_code]["days"][str(day)].append({"session": session, "period": period, "subject": subject})
    if not week or not start_date or len(parsed) < 15:
        raise ValueError("PDF has no complete timetable")
    if any(sum(map(len, item["days"].values())) < 15 for item in parsed.values()):
        raise ValueError("At least one class has too few lessons")
    return {"week": int(week), "start_date": start_date, "title": title, "pdf_url": pdf_url, "classes": parsed}


def response(snapshot: dict, class_code: str, day: str | None = None) -> dict:
    class_code = class_code.strip().upper()
    start = datetime.strptime(snapshot["start_date"], "%d/%m/%Y").date()
    if (date.today() - start).days > 13:
        message = "Chưa xác nhận được thời khóa biểu mới. Anh/chị vui lòng xem tệp mới nhất trên trang trường."
    elif not CLASS_INPUT_RE.fullmatch(class_code) or class_code not in snapshot["classes"]:
        message = "Em chưa tìm thấy lớp này trong thời khóa biểu mới nhất. Anh/chị kiểm tra lại mã lớp (ví dụ 7A2) hoặc xem tệp gốc."
    else:
        entry = snapshot["classes"][class_code]
        heading = f"TKB lớp {class_code} — {entry['campus']}\nTuần {snapshot['week']}, từ {snapshot['start_date']}"
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
        "content": {"messages": [{"type": "text", "text": message, "buttons": [{"name": "Xem tệp gốc", "type": "url", "url": snapshot["pdf_url"]}]}]},
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
