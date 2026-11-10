"""Optionally copy generated weekly PNGs to the owner's Google Drive folder."""

import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


PUBLIC = Path(__file__).with_name("public")
INDEX = PUBLIC / "drive-index.json"


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
    uploaded = reused = failed = 0
    for group in ("lop", "gv"):
        for path in sorted((image_root / group).glob("*.png")):
            relative = path.relative_to(PUBLIC).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            previous = entries.get(relative, {})
            if previous.get("sha256") == digest and previous.get("view_url"):
                view_url = previous["view_url"]
                reused += 1
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
                except Exception as exc:
                    failed += 1
                    print(f"::warning::Drive upload failed for {relative}: {exc}")
                    continue
                view_url = result["view_url"]
                entries[relative] = {"sha256": digest, "view_url": view_url, "id": result.get("id")}
                uploaded += 1
            response_path = (PUBLIC / f"{path.stem}.json" if group == "lop"
                             else PUBLIC / "gv" / f"{path.stem}.json")
            update_button(response_path, view_url)
    INDEX.write_text(json.dumps({"images": entries}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Drive sync: {uploaded} uploaded, {reused} unchanged, {failed} failed")


if __name__ == "__main__":
    main()
