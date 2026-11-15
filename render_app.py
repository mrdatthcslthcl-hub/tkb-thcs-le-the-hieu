"""Fast Zalo OA endpoint backed by the verified timetable files in public/."""

from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlparse


PUBLIC = Path(__file__).with_name("public")
CLASS_CODE = re.compile(r"^[6789][AB][123]$")
TEACHER_SLUG = re.compile(r"^[a-z0-9-]+$")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_files() -> tuple[dict, dict, dict]:
    status = read_json(PUBLIC / "status.json")
    teachers = read_json(PUBLIC / "gv" / "index.json")
    drive = read_json(PUBLIC / "drive-index.json")["images"]
    return status, teachers, drive


def image_relative_path(status: dict, group: str, name: str) -> str:
    start = datetime.strptime(status["starts"], "%d/%m/%Y").date()
    return f"anh/{start.isoformat()}-tuan-{status['week']}/{group}/{name}.png"


def text_reply(message: str) -> dict:
    return {"version": "chatbot", "content": {"messages": [
        {"type": "text", "text": message},
    ]}}


def teacher_slug(value: str, index: dict) -> str | None:
    value = value.strip().casefold()
    if not value:
        return None
    for item in index.get("teachers", []):
        slug = Path(item["path"]).stem
        if value in (slug.casefold(), item["name"].casefold(),
                     item.get("full_name", "").casefold()):
            return slug
    # Accept a teacher's Vietnamese name without accents as entered by OA.
    def plain(s: str) -> str:
        s = s.replace("đ", "d").replace("Đ", "D")
        return "".join(c for c in unicodedata.normalize("NFD", s)
                       if unicodedata.category(c) != "Mn").casefold()
    for item in index.get("teachers", []):
        if plain(value) == plain(item.get("full_name", "")):
            return Path(item["path"]).stem
    return None


def timetable_reply(class_code: str = "", teacher: str = "") -> dict:
    status, teachers, drive = snapshot_files()
    start = datetime.strptime(status["starts"], "%d/%m/%Y").date()
    if (date.today() - start).days > 13:
        return text_reply("Chưa xác nhận được TKB tuần mới. Vui lòng xem trang thời khóa biểu của trường.")
    if teacher:
        name = teacher_slug(teacher, teachers)
        if not name or not TEACHER_SLUG.fullmatch(name):
            return text_reply("Em chưa tìm thấy giáo viên này trong TKB hiện tại. Anh/chị kiểm tra lại tên.")
        group, reply_path = "gv", PUBLIC / "gv" / f"{name}.json"
    else:
        name = class_code.strip().upper()
        if not CLASS_CODE.fullmatch(name) or name not in status["classes"]:
            return text_reply("Em chưa tìm thấy lớp này. Anh/chị nhập mã lớp, ví dụ 7A2.")
        group, reply_path = "lop", PUBLIC / f"{name}.json"
    item = drive.get(image_relative_path(status, group, name), {})
    if not item.get("id") or not item.get("view_url"):
        return text_reply("Ảnh TKB đang được cập nhật. Anh/chị vui lòng thử lại sau.")
    reply = read_json(reply_path)
    messages = reply["content"]["messages"]
    if len(messages) < 2 or messages[1].get("type") != "image":
        raise ValueError(f"Invalid image reply: {reply_path}")
    for button in messages[0].get("buttons", []):
        if button.get("name") == "Mở ảnh TKB":
            button["url"] = item["view_url"]
    messages[1]["image_url"] = f"https://drive.google.com/uc?export=view&id={item['id']}"
    return reply


def verify_snapshot() -> dict:
    status, teachers, drive = snapshot_files()
    if len(status["classes"]) != 21 or len(teachers["teachers"]) != status["teacher_count"]:
        raise ValueError("Timetable class or teacher count is incomplete")
    for code in status["classes"]:
        if not (PUBLIC / f"{code}.json").is_file() or not drive.get(image_relative_path(status, "lop", code), {}).get("id"):
            raise ValueError(f"Missing class image: {code}")
    for item in teachers["teachers"]:
        slug = Path(item["path"]).stem
        if not (PUBLIC / item["path"]).is_file() or not drive.get(image_relative_path(status, "gv", slug), {}).get("id"):
            raise ValueError(f"Missing teacher image: {slug}")
    return status


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                status = verify_snapshot()
                payload = {"ok": True, "week": status["week"], "starts": status["starts"],
                           "classes": len(status["classes"]), "teachers": status["teacher_count"]}
            elif parsed.path == "/tkb":
                query = parse_qs(parsed.query)
                payload = timetable_reply(query.get("lop", [""])[0], query.get("gv", [""])[0])
            else:
                self.send_error(404)
                return
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            print(f"Request failed: {exc}", flush=True)
            self.send_error(503)


if __name__ == "__main__":
    status = verify_snapshot()
    print(f"Serving timetable week {status['week']} for {len(status['classes'])} classes", flush=True)
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Handler).serve_forever()
