# -*- coding: utf-8 -*-
"""
折纸笔记 · 本地 Obsidian Vault 零依赖极轻桥接服务
- 纯 Python 标准库，无需 pip install
- 自动读写本地 Obsidian 文件夹（子目录=书本，.md=页面，.ink.svg=矢量涂鸦，attachments/=图片）
- 记住上次连接的 Vault 路径，下次打开免重复授权
"""
import http.server
import json
import os
import re
import socketserver
import threading
import urllib.parse
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / ".vault_config.json"
DEFAULT_VAULT = BASE_DIR / "demo-vault"
PORT = 8788


def load_vault_path() -> Path:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            p = Path(data.get("vault_path", ""))
            if p.exists() and p.is_dir():
                return p
        except Exception:
            pass
    DEFAULT_VAULT.mkdir(parents=True, exist_ok=True)
    return DEFAULT_VAULT


def save_vault_path(p: Path):
    CONFIG_FILE.write_text(
        json.dumps({"vault_path": str(p.resolve())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", (name or "未命名").strip())
    return cleaned[:60] or "未命名"


def parse_frontmatter(raw: str):
    meta = {}
    body = raw
    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?", raw)
    if m:
        body = raw[m.end():]
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                if v.lower() == "true":
                    meta[k] = True
                elif v.lower() == "false":
                    meta[k] = False
                elif re.fullmatch(r"-?\d+", v):
                    meta[k] = int(v)
                else:
                    meta[k] = v.strip('"').strip("'")
    return meta, body


def dump_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            s = str(v).replace("\n", " ")
            lines.append(f"{k}: {s}")
    lines.append("---\n")
    return "\n".join(lines)


def scan_vault(vault: Path):
    """扫描 Obsidian Vault -> books 数组结构"""
    colors = ["#4E86B8", "#C97346", "#6E8A42", "#C47683", "#7A5EA8", "#B08935"]
    books = []
    ignore_dirs = {".obsidian", ".trash", ".git", "attachments", "_attachments"}

    # 1. 子目录各成一本书
    subdirs = sorted(
        [d for d in vault.iterdir() if d.is_dir() and d.name not in ignore_dirs and not d.name.startswith(".")],
        key=lambda d: d.name,
    )

    def read_book_dir(folder: Path, book_title: str, idx: int):
        md_files = sorted(
            [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".md"],
            key=lambda f: f.name,
        )
        pages = []
        for p_idx, md_file in enumerate(md_files):
            raw = md_file.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_frontmatter(raw)
            # 抽离末尾涂鸦引用与块 JSON 注释（如果有）
            state_json = None
            sm = re.search(r"<!--\s*origami-state:([\s\S]*?)-->\s*$", body)
            if sm:
                try:
                    state_json = json.loads(sm.group(1).strip())
                except Exception:
                    state_json = None
                body = body[: sm.start()].rstrip()
            # 去掉自动附加的涂鸦内嵌标记，正文保持干净
            body_clean = re.sub(r"\n?!\[\[[^\]]+\.ink\.svg\]\]\s*$", "", body).rstrip()

            # 读取同名 .ink.svg 涂鸦
            ink_file = md_file.with_suffix(".ink.svg")
            ink_svg = ink_file.read_text(encoding="utf-8", errors="replace") if ink_file.exists() else ""

            pages.append({
                "id": meta.get("id") or f"p_{safe_name(book_title)}_{p_idx+1}",
                "fileName": md_file.name,
                "title": meta.get("title") or md_file.stem,
                "order": meta.get("order", p_idx + 1),
                "bookmarked": bool(meta.get("bookmarked", False)),
                "paper": meta.get("paper", "grid"),
                "viewMode": meta.get("viewMode", "doc"),
                "markdown": body_clean,
                "inkSvg": ink_svg,
                "state": state_json,
            })
        pages.sort(key=lambda x: (x.get("order", 9999), x.get("fileName", "")))
        return {
            "id": f"book_{safe_name(book_title)}",
            "folder": "" if folder == vault else folder.name,
            "title": book_title,
            "color": colors[idx % len(colors)],
            "pages": pages,
        }

    for i, d in enumerate(subdirs):
        b = read_book_dir(d, d.name, i)
        if b["pages"]:
            books.append(b)

    # 2. 根目录散落 .md 归入「随手记」
    root_mds = [f for f in vault.iterdir() if f.is_file() and f.suffix.lower() == ".md"]
    if root_mds:
        books.append(read_book_dir(vault, "随手记", len(books)))

    return {"vaultPath": str(vault.resolve()), "books": books}


def write_page(vault: Path, payload: dict):
    folder_name = payload.get("folder", "")
    target_dir = vault / safe_name(folder_name) if folder_name else vault
    target_dir.mkdir(parents=True, exist_ok=True)

    title = payload.get("title") or "未命名"
    old_file = payload.get("oldFileName")
    file_name = payload.get("fileName") or (safe_name(title) + ".md")
    if not file_name.endswith(".md"):
        file_name += ".md"
    md_path = target_dir / file_name

    # 如果重命名了文件名，删除旧文件及旧涂鸦
    if old_file and old_file != file_name:
        old_p = target_dir / old_file
        if old_p.exists():
            old_p.unlink(missing_ok=True)
        old_ink = old_p.with_suffix(".ink.svg")
        if old_ink.exists():
            old_ink.unlink(missing_ok=True)

    meta = {
        "id": payload.get("id", ""),
        "title": title,
        "order": int(payload.get("order", 1)),
        "bookmarked": bool(payload.get("bookmarked", False)),
        "paper": payload.get("paper", "grid"),
        "viewMode": payload.get("viewMode", "doc"),
    }
    markdown = (payload.get("markdown") or "").rstrip()
    ink_svg = (payload.get("inkSvg") or "").strip()
    state = payload.get("state")

    ink_path = md_path.with_suffix(".ink.svg")
    has_strokes = bool(ink_svg and "<path" in ink_svg)
    if has_strokes:
        ink_path.write_text(ink_svg, encoding="utf-8")
        ink_ref = f"\n\n![[{ink_path.name}]]"
    else:
        if ink_path.exists():
            ink_path.unlink(missing_ok=True)
        ink_ref = ""

    state_comment = ""
    if state:
        state_comment = f"\n\n<!-- origami-state:{json.dumps(state, ensure_ascii=False)} -->"

    content = dump_frontmatter(meta) + markdown + ink_ref + state_comment + "\n"
    md_path.write_text(content, encoding="utf-8")
    return {"ok": True, "fileName": file_name}


def delete_page_file(vault: Path, payload: dict):
    folder_name = payload.get("folder", "")
    file_name = payload.get("fileName", "")
    if not file_name:
        return {"ok": False}
    target_dir = vault / safe_name(folder_name) if folder_name else vault
    md_path = target_dir / file_name
    if md_path.exists():
        md_path.unlink(missing_ok=True)
    ink_path = md_path.with_suffix(".ink.svg")
    if ink_path.exists():
        ink_path.unlink(missing_ok=True)
    return {"ok": True}


def pick_folder_dialog():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="选择你的 Obsidian 笔记库文件夹 (Vault)")
        root.destroy()
        return chosen or ""
    except Exception:
        return ""


class BridgeHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, format, *args):
        pass

    def _send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            return self._send_json({"ok": True, "vaultPath": str(load_vault_path())})
        if parsed.path == "/api/vault":
            return self._send_json(scan_vault(load_vault_path()))
        if parsed.path in ("/", "", "/tutor", "/tutor/"):
            self.path = "/index.html"
        elif parsed.path in ("/desk", "/desk/"):
            self.path = "/desk.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/vault/pick":
            chosen = pick_folder_dialog()
            if chosen:
                p = Path(chosen)
                save_vault_path(p)
                return self._send_json(scan_vault(p))
            return self._send_json({"cancelled": True})
        if parsed.path == "/api/vault/set":
            data = self._read_json()
            p = Path(data.get("path", ""))
            if p.exists() and p.is_dir():
                save_vault_path(p)
                return self._send_json(scan_vault(p))
            return self._send_json({"ok": False, "error": "目录不存在"}, 400)
        if parsed.path == "/api/page/save":
            data = self._read_json()
            return self._send_json(write_page(load_vault_path(), data))
        if parsed.path == "/api/page/delete":
            data = self._read_json()
            return self._send_json(delete_page_file(load_vault_path(), data))
        return self._send_json({"error": "Not found"}, 404)


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"[折纸笔记] 本地 Obsidian 桥接服务已启动: http://127.0.0.1:{PORT}/")
    print(f"[折纸笔记] 当前绑定库: {load_vault_path()}")
    if os.environ.get("NO_OPEN") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    with ReusableTCPServer(("127.0.0.1", PORT), BridgeHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
