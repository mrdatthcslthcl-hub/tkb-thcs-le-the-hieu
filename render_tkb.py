"""Render one-week class and teacher timetables as compact PNG images."""

from __future__ import annotations

import html
from pathlib import Path
import shutil
import subprocess
import tempfile

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Paragraph, Table, TableStyle
from PIL import Image, ImageDraw, ImageFont


FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
pdfmetrics.registerFont(TTFont("DejaVu", str(FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVuBold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVuBold")

NAVY = colors.HexColor("#153D55")
BLUE = colors.HexColor("#17658A")
PALE_BLUE = colors.HexColor("#EAF5FA")
PALE_ORANGE = colors.HexColor("#FFF1DE")
GRID = colors.HexColor("#A7BDC9")
MUTED = colors.HexColor("#4D6675")
NORMAL = ParagraphStyle("normal", fontName="DejaVu", fontSize=8.5, leading=11, textColor=NAVY)
EMPTY = ParagraphStyle("empty", parent=NORMAL, alignment=TA_CENTER, textColor=colors.HexColor("#B8C9D0"))


def _teacher_cell(items: list[dict]) -> Paragraph:
    if not items:
        return Paragraph("—", EMPTY)
    parts = []
    for item in items:
        mark = "*" if item["kind"] == "composite" else ""
        parts.append(
            f'<b>{html.escape(item["subject"])}-{html.escape(item["pdf_teacher"])}{mark}</b>'
            f'<br/>{html.escape(item["class"])}'
        )
    return Paragraph("<br/>".join(parts), NORMAL)


def _draw_grid(canvas, *, title: str, name: str, snapshot: dict,
               rows_by_day: dict[str, list[dict]], is_teacher: bool):
    width, height = landscape(A4)
    left, right = 35, width - 35
    canvas.setTitle(f"{title} {name} - Tuần {snapshot['week']}")
    canvas.setFillColor(BLUE)
    canvas.setFont("DejaVuBold", 10)
    canvas.drawString(left, height - 47, title)
    canvas.setFillColor(NAVY)
    canvas.setFont("DejaVuBold", 17 if is_teacher else 18)
    canvas.drawString(left, height - 73, name)
    canvas.setFont("DejaVuBold", 11)
    canvas.drawRightString(right, height - 52, f'TUẦN {snapshot["week"]}')
    canvas.setFont("DejaVu", 8.5)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(right, height - 69, f'Từ {snapshot["start_date"]}')
    canvas.setStrokeColor(BLUE)
    canvas.setLineWidth(1.5)
    canvas.line(left, height - 85, right, height - 85)

    has_fifth = any(
        item["session"] == "Sáng" and item["period"] == 5
        for day in range(2, 7) for item in rows_by_day.get(str(day), [])
    )
    morning_periods = range(1, 6 if has_fifth else 5)
    afternoon_start = len(morning_periods) + 1
    afternoon_end = afternoon_start + 2
    rows = [["BUỔI", "TIẾT", "THỨ 2", "THỨ 3", "THỨ 4", "THỨ 5", "THỨ 6"]]
    for session, periods in (("Sáng", morning_periods), ("Chiều", range(1, 4))):
        for i, period in enumerate(periods):
            row = [session if i == 0 else "", str(period)]
            for day in range(2, 7):
                items = [x for x in rows_by_day.get(str(day), [])
                         if x["session"] == session and x["period"] == period]
                if is_teacher:
                    row.append(_teacher_cell(items))
                else:
                    row.append(" / ".join(x["subject"] for x in items) if items else "—")
            rows.append(row)

    table_width = right - left
    row_heights = [31] + [52] * (len(rows) - 1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "DejaVuBold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("BACKGROUND", (1, 1), (1, -1), colors.HexColor("#F4F8FA")),
        ("BACKGROUND", (0, 1), (0, afternoon_start - 1), PALE_BLUE),
        ("BACKGROUND", (0, afternoon_start), (0, afternoon_end), PALE_ORANGE),
        ("TEXTCOLOR", (0, 1), (0, afternoon_start - 1), colors.HexColor("#18516F")),
        ("TEXTCOLOR", (0, afternoon_start), (0, afternoon_end), colors.HexColor("#955614")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("SPAN", (0, 1), (0, afternoon_start - 1)),
        ("SPAN", (0, afternoon_start), (0, afternoon_end)),
        ("LINEABOVE", (0, afternoon_start), (-1, afternoon_start), 1.5, BLUE),
    ]
    if is_teacher:
        style += [
            ("FONTNAME", (0, 1), (1, -1), "DejaVuBold"),
            ("FONTSIZE", (0, 1), (1, -1), 10),
            ("LEFTPADDING", (2, 1), (-1, -1), 8),
            ("RIGHTPADDING", (2, 1), (-1, -1), 5),
        ]
    else:
        style += [
            ("FONTNAME", (0, 1), (-1, -1), "DejaVuBold"),
            ("FONTSIZE", (0, 1), (1, -1), 10),
            ("TEXTCOLOR", (2, 1), (-1, -1), NAVY),
        ]
        subject_width = (table_width - 110) / 5 - 12
        for row_index, row in enumerate(rows[1:], start=1):
            for col_index, subject in enumerate(row[2:], start=2):
                size = 16.5
                while size > 10 and pdfmetrics.stringWidth(subject, "DejaVuBold", size) > subject_width:
                    size -= 0.5
                style.append(("FONTSIZE", (col_index, row_index), (col_index, row_index), size))
    table = Table(rows, colWidths=[65, 45] + [(table_width - 110) / 5] * 5,
                  rowHeights=row_heights)
    table.setStyle(TableStyle(style))
    table_y = height - 95 - sum(row_heights)
    table.wrapOn(canvas, table_width, sum(row_heights))
    table.drawOn(canvas, left, table_y)

    if is_teacher and any(item["kind"] == "composite"
                          for items in rows_by_day.values() for item in items):
        foot = Paragraph(
            '* Tiết ghép “…” được thống kê theo phân công; giáo viên trực tiếp dạy theo thông báo của nhà trường. Ô có hai lớp cùng giờ cần xác nhận.',
            ParagraphStyle("foot", parent=NORMAL, fontSize=7.7, leading=10, textColor=MUTED),
        )
        foot.wrapOn(canvas, table_width, 25)
        foot.drawOn(canvas, left, table_y - 21)


def _render_png(path: Path, draw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "timetable.pdf"
        png_stem = Path(tmp) / "timetable"
        canvas = pdf_canvas.Canvas(str(pdf_path), pagesize=landscape(A4))
        draw(canvas)
        canvas.showPage()
        canvas.save()
        subprocess.run(
            ["pdftoppm", "-f", "1", "-l", "1", "-scale-to", "1600", "-png", "-singlefile",
             str(pdf_path), str(png_stem)],
            check=True, timeout=30, stdout=subprocess.DEVNULL,
        )
        image_path = png_stem.with_suffix(".png")
        if not image_path.exists() or image_path.stat().st_size > 1_000_000:
            raise ValueError("Rendered PNG is missing or unexpectedly large")
        shutil.copyfile(image_path, path)


def render_class_png(snapshot: dict, code: str, path: Path) -> None:
    entry = snapshot["classes"][code]
    _render_png(path, lambda canvas: _draw_grid(
        canvas, title="THỜI KHÓA BIỂU LỚP", name=code, snapshot=snapshot,
        rows_by_day=entry["days"], is_teacher=False,
    ))


def render_teacher_png(snapshot: dict, name: str, full_name: str,
                       items: list[dict], path: Path) -> None:
    """Draw a narrow, readable weekly image for phone screens."""
    width = 720
    margin = 28
    image = Image.new("RGB", (width, 10000), "#ffffff")
    draw = ImageDraw.Draw(image)
    regular = str(FONT_DIR / "DejaVuSans.ttf")
    bold = str(FONT_DIR / "DejaVuSans-Bold.ttf")
    font = lambda size, strong=False: ImageFont.truetype(bold if strong else regular, size)
    title_font, name_font = font(25, True), font(38, True)
    day_font, session_font = font(27, True), font(22, True)
    lesson_font, period_font = font(30, True), font(24, True)
    detail_font, note_font = font(23), font(18)
    navy, blue, muted = "#153D55", "#17658A", "#4D6675"

    def text_width(value: str, text_font) -> int:
        return draw.textbbox((0, 0), value, font=text_font)[2]

    def wrap(value: str, text_font, limit: int) -> list[str]:
        lines, current = [], ""
        for word in value.split():
            candidate = f"{current} {word}".strip()
            if current and text_width(candidate, text_font) > limit:
                lines.append(current)
                current = word
            else:
                current = candidate
        return lines + ([current] if current else [])

    y = 26
    draw.text((margin, y), "LỊCH DẠY GIÁO VIÊN", fill=blue, font=title_font)
    y += 45
    for line in wrap(full_name, name_font, width - 2 * margin):
        draw.text((margin, y), line, fill=navy, font=name_font)
        y += 51
    draw.text((margin, y + 1), f"Tuần {snapshot['week']} · Từ {snapshot['start_date']}",
              fill=muted, font=detail_font)
    y += 51
    draw.line((margin, y, width - margin, y), fill=blue, width=3)

    for day in range(2, 7):
        daily = [item for item in items if item["day"] == str(day)]
        daily.sort(key=lambda item: (item["session"] != "Sáng", item["period"], item["class"]))
        y += 18
        draw.rounded_rectangle((margin, y, width - margin, y + 47), radius=10, fill=blue)
        draw.text((margin + 17, y + 7), f"THỨ {day}", fill="#ffffff", font=day_font)
        y += 47
        if not daily:
            draw.text((margin + 17, y + 17), "Không có tiết dạy",
                      fill=muted, font=detail_font)
            y += 65
            continue
        for session_name in ("Sáng", "Chiều"):
            scheduled = [item for item in daily if item["session"] == session_name]
            if not scheduled:
                continue
            y += 6
            session_fill = "#EAF5FA" if session_name == "Sáng" else "#FFF1DE"
            session_text = blue if session_name == "Sáng" else "#955614"
            draw.rectangle((margin, y, width - margin, y + 36), fill=session_fill)
            draw.text((margin + 16, y + 5), session_name.upper(),
                      fill=session_text, font=session_font)
            y += 36
            for item in scheduled:
                row_top = y
                draw.text((margin + 15, row_top + 22), f"Tiết {item['period']}",
                          fill=muted, font=period_font)
                label = f"{item['subject']}-{item['pdf_teacher']}"
                if item["kind"] == "composite":
                    label += "*"
                label_font = lesson_font
                while text_width(label, label_font) > width - margin - 176 and label_font.size > 23:
                    label_font = font(label_font.size - 1, True)
                draw.text((margin + 150, row_top + 7), label,
                          fill=navy, font=label_font)
                draw.text((margin + 150, row_top + 44), f"Lớp {item['class']}",
                          fill=muted, font=detail_font)
                y += 83
                draw.line((margin, y, width - margin, y), fill="#DDE7EC", width=2)

    if any(item["kind"] == "composite" for item in items):
        y += 18
        note = "* Tiết ghép được thống kê theo phân công; giáo viên trực tiếp dạy theo thông báo của nhà trường."
        for line in wrap(note, note_font, width - 2 * margin):
            draw.text((margin, y), line, fill=muted, font=note_font)
            y += 27
    y += 30
    if y > image.height:
        raise ValueError("Teacher timetable is too tall")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.crop((0, 0, width, y)).save(path, format="PNG", optimize=True)
    if path.stat().st_size > 1_000_000:
        raise ValueError("Teacher timetable PNG is unexpectedly large")
