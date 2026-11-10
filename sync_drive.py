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
ROOT_FOLDER_ID = "1vMxNBQ635rnxOiyUjJKW3vADy9QoTMku"


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


def image_folders(week: str) -> dict[str, str]:
    week_id = next((file_id for kind, file_id, name in folder_entries(ROOT_FOLDER_ID)
                    if kind == "drive/folders" and name == week), None)
    if not week_id:
        return {}
    return {name: file_id for kind, file_id, name in folder_entries(week_id)
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


def update_button(response_path: Path, url: str) -> None:
    if not response_path.exists():
        return
    value = json.loads(response_path.read_text(encoding="utf-8"))
    messages = value.get("content", {}).get("messages", [])
    if len(messages) < 2 or messages[1].get("type") != "image":
        return
    buttons = messages[0].setdefault("buttons", [])
    buttons[:] = [button for button in buttons if button.get("name") != "Mở trên Drive"]
    buttons.append({"name": "Mở trên Drive", "type": "url", "url": url})
    response_path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    endpoint = os.environ.get("DRIVE_UPLOAD_ENDPOINT", "").strip()
    key = os.environ.get("DRIVE_UPLOAD_KEY", "").strip()
    if not endpoint or not key:
        print("Drive sync skipped: add both GitHub secrets to enable it")
        return
    if not endpoint.startswith("https://script.google.com/macros/s/") or not endpoint.endswith("/exec"):
        raise ValueError("DRIVE_UPLOAD_ENDPOINT must be a deployed Apps Script web app URL")

    existing = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    entries = existing.get("images", {})
    status = json.loads((PUBLIC / "status.json").read_text(encoding="utf-8"))
    date_iso = f"{status['starts'][6:10]}-{status['starts'][3:5]}-{status['starts'][:2]}"
    week = f"{date_iso}-tuan-{status['week']}"
    image_root = PUBLIC / "anh" / week
    paths = {group: sorted((image_root / group).glob("*.png")) for group in ("lop", "gv")}
    digests = {path: hashlib.sha256(path.read_bytes()).hexdigest()
               for group_paths in paths.values() for path in group_paths}
    missing = {group: [path for path in group_paths if
                       entries.get(path.relative_to(PUBLIC).as_posix(), {}).get("sha256") != digests[path]
                       or not entries.get(path.relative_to(PUBLIC).as_posix(), {}).get("view_url")]
               for group, group_paths in paths.items()}
    folders = image_folders(week) if any(missing.values()) else {}
    known = {group: scan_existing(folders[group], {path.name for path in group_paths})
             if group_paths and group in folders else {}
             for group, group_paths in missing.items()}
    uploaded = recovered = reused = failed = 0
    for group in ("lop", "gv"):
        for path in paths[group]:
            relative = path.relative_to(PUBLIC).as_posix()
            digest = digests[path]
            previous = entries.get(relative, {})
            if previous.get("sha256") == digest and previous.get("view_url"):
                view_url = previous["view_url"]
                reused += 1
            else:
                file_id = known[group].get((path.name, digest))
                if file_id:
                    recovered += 1
                else:
                    request = Request(
                        endpoint,
                        data=json.dumps({
                            "key": key, "week": week, "group": group, "name": path.name,
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
                                folders = image_folders(week)
                            if group in folders:
                                found = scan_existing(folders[group], {path.name})
                                file_id = found.get((path.name, digest))
                                if file_id:
                                    break
                        if not file_id:
                            failed += 1
                            print(f"::warning::Drive upload failed for {relative}: {exc}")
                            continue
                        recovered += 1
                view_url = f"https://drive.google.com/file/d/{file_id}/view"
                entries[relative] = {"sha256": digest, "view_url": view_url, "id": file_id}
            response_path = (PUBLIC / f"{path.stem}.json" if group == "lop"
                             else PUBLIC / "gv" / f"{path.stem}.json")
            update_button(response_path, view_url)
    INDEX.write_text(json.dumps({"images": entries}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Drive sync: {uploaded} uploaded, {recovered} found in Drive, "
          f"{reused} unchanged, {failed} failed")


if __name__ == "__main__":
    main()
