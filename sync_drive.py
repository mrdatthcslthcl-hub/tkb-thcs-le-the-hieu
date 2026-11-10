"""Optionally copy generated weekly PNGs to the owner's Google Drive folder."""

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen


PUBLIC = Path(__file__).with_name("public")
INDEX = PUBLIC / "drive-index.json"
NUMBER_INDEX = PUBLIC / "gv" / "drive-numbers.json"
ROOT_FOLDER_ID = "1vMxNBQ635rnxOiyUjJKW3vADy9QoTMku"
CURRENT_FOLDER = "TKB-moi-nhat"
BASE_TEACHER_ORDER = (
    "anh binh chien chung giang h-yen hien hue ha han hoa hung huong hau hang "
    "khuong kiem lan loan long luyen ly m-nha minh nghia nhung nhan p-hai "
    "phuoc phuong-an-son phuong-truong-chinh quyen quan t-nha thien thong "
    "toan trung tinh tu van vuong d-hai d-yen dat"
).split()


def folder_entries(folder_id: str) -> list[tuple[str, str, str]]:
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as response:
        html = response.read().decode("utf-8", errors="replace")
    return [
        (kind, file_id, unescape(name))
        for kind, file_id, name in re.findall(
            r'href="https://drive\.google\.com/(file/d|drive/folders)/([A-Za-z0-9_-]+)[^\"]*"'
            r'.*?<div class="flip-entry-title">([^<]+)</div>', html, re.DOTALL,
        )
    ]


def image_folders() -> dict[str, str]:
    current_id = next((file_id for kind, file_id, name in folder_entries(ROOT_FOLDER_ID)
                       if kind == "drive/folders" and name == CURRENT_FOLDER), None)
    if not current_id:
        return {}
    return {name: file_id for kind, file_id, name in folder_entries(current_id)
            if kind == "drive/folders" and name in {"lop", "gv"}}


def file_digest(file_id: str) -> str:
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Drive file is not a small PNG")
    return hashlib.sha256(data).hexdigest()


def scan_existing(folder_id: str, names: set[str]) -> dict[tuple[str, str], str]:
    files = [(file_id, name) for kind, file_id, name in folder_entries(folder_id)
             if kind == "file/d" and name in names]
    found = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(file_digest, file_id): (name, file_id)
                   for file_id, name in files}
        for future in as_completed(futures):
            name, file_id = futures[future]
            try:
                found[(name, future.result())] = file_id
            except Exception:
                pass
    return found


def teacher_file_names(paths: list[Path]) -> dict[Path, str]:
    """Keep each teacher's Drive number stable when the roster changes."""
    stored = json.loads(NUMBER_INDEX.read_text(encoding="utf-8")) if NUMBER_INDEX.exists() else {}
    numbers = stored.get("numbers", {}) or {
        slug: number for number, slug in enumerate(BASE_TEACHER_ORDER, start=1)
    }
    for path in paths:
        if path.stem not in numbers:
            numbers[path.stem] = max(numbers.values(), default=0) + 1
    if len(set(numbers.values())) != len(numbers):
        raise ValueError("Teacher Drive numbers must be unique")
    NUMBER_INDEX.write_text(json.dumps({"numbers": numbers}, ensure_ascii=False,
                                       separators=(",", ":")), encoding="utf-8")
    return {path: f"{numbers[path.stem]:02d}-{'-'.join(part.capitalize() for part in path.stem.split('-'))}.png"
            for path in paths}


def main() -> None:
    endpoint = os.environ.get("DRIVE_UPLOAD_ENDPOINT", "").strip()
    key = os.environ.get("DRIVE_UPLOAD_KEY", "").strip()
    if not endpoint or not key:
        raise RuntimeError("Drive sync is not configured")
    if not endpoint.startswith("https://script.google.com/macros/s/") or not endpoint.endswith("/exec"):
        raise ValueError("DRIVE_UPLOAD_ENDPOINT must be a deployed Apps Script web app URL")

    existing = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    entries = existing.get("images", {})
    # A new week has a new local path, but should keep the same Drive file ID.
    latest_by_name = {}
    for old_path in sorted(entries):
        match = re.search(r"/(lop|gv)/([^/]+\.png)$", old_path)
        if match:
            latest_by_name[(match.group(1), match.group(2))] = entries[old_path]
    for relative, item in entries.items():
        if "/lop/" in relative:
            item.setdefault("drive_name", Path(relative).name)
    status = json.loads((PUBLIC / "status.json").read_text(encoding="utf-8"))
    date_iso = f"{status['starts'][6:10]}-{status['starts'][3:5]}-{status['starts'][:2]}"
    week = f"{date_iso}-tuan-{status['week']}"
    image_root = PUBLIC / "anh" / week
    paths = {group: sorted((image_root / group).glob("*.png")) for group in ("lop", "gv")}
    if not status["teacher_count"] or len(paths["lop"]) != len(status["classes"]) or len(paths["gv"]) != status["teacher_count"]:
        raise RuntimeError("Generated image count does not match the current timetable")
    drive_names = {path: path.name for path in paths["lop"]}
    drive_names.update(teacher_file_names(paths["gv"]))
    digests = {path: hashlib.sha256(path.read_bytes()).hexdigest()
               for group_paths in paths.values() for path in group_paths}
    missing = {group: [path for path in group_paths if
                       entries.get(path.relative_to(PUBLIC).as_posix(), {}).get("sha256") != digests[path]
                       or entries.get(path.relative_to(PUBLIC).as_posix(), {}).get("folder") != CURRENT_FOLDER
                       or entries.get(path.relative_to(PUBLIC).as_posix(), {}).get("drive_name") != drive_names[path]
                       or not entries.get(path.relative_to(PUBLIC).as_posix(), {}).get("view_url")]
               for group, group_paths in paths.items()}
    folders = image_folders() if any(missing.values()) else {}
    known = {group: scan_existing(folders[group], {drive_names[path] for path in group_paths})
             if group_paths and group in folders else {}
             for group, group_paths in missing.items()}
    uploaded = recovered = reused = failed = 0
    for group in ("lop", "gv"):
        for path in paths[group]:
            relative = path.relative_to(PUBLIC).as_posix()
            digest = digests[path]
            drive_name = drive_names[path]
            previous = entries.get(relative) or latest_by_name.get((group, path.name), {})
            if (previous.get("sha256") == digest and previous.get("folder") == CURRENT_FOLDER
                    and previous.get("drive_name") == drive_name and previous.get("view_url")
                    and previous.get("id")):
                file_id = previous["id"]
                view_url = previous["view_url"]
                reused += 1
            else:
                file_id = known[group].get((drive_name, digest))
                if file_id:
                    recovered += 1
                else:
                    request = Request(
                        endpoint,
                        data=json.dumps({
                            "key": key, "week": week, "group": group, "name": drive_name,
                            "old_name": path.name, "existing_id": previous.get("id", ""),
                            "data_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    try:
                        with urlopen(request, timeout=45) as response:
                            result = json.load(response)
                        if not result.get("ok") or not result.get("view_url"):
                            raise ValueError(result.get("error", "No Drive URL returned"))
                        file_id = result.get("id")
                        view_url = result["view_url"]
                        uploaded += 1
                    except Exception as exc:
                        # Some accounts allow creating a file but reject a later
                        # sharing/deletion call. Recover the public file by hash.
                        for attempt in range(3):
                            if attempt:
                                time.sleep(2)
                            if group not in folders:
                                folders = image_folders()
                            if group in folders:
                                found = scan_existing(folders[group], {drive_name})
                                file_id = found.get((drive_name, digest))
                                if file_id:
                                    break
                        if not file_id:
                            failed += 1
                            print(f"::warning::Drive upload failed for {relative}: {exc}")
                            continue
                        recovered += 1
                view_url = f"https://drive.google.com/file/d/{file_id}/view"
            entries[relative] = {"sha256": digest, "view_url": view_url,
                                 "id": file_id, "folder": CURRENT_FOLDER,
                                 "drive_name": drive_name}
    INDEX.write_text(json.dumps({"images": entries}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Drive sync: {uploaded} uploaded, {recovered} found in Drive, "
          f"{reused} unchanged, {failed} failed")
    if failed:
        raise RuntimeError(f"{failed} timetable images could not be synced to Drive")


if __name__ == "__main__":
    main()
