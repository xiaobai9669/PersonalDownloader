from __future__ import annotations

import concurrent.futures
import ctypes
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from curl_cffi import requests as curl_requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.message import Message
from dataclasses import dataclass, asdict, field
from functools import lru_cache
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pystray
from PIL import Image, ImageDraw


APP_DIR = Path(os.environ.get("PERSONAL_DOWNLOADER_DATA", Path.home() / ".personal-downloader"))
STATE_FILE = APP_DIR / "tasks.json"
CHUNK_SIZE = 8 * 1024 * 1024
BRIDGE_PORT = int(os.environ.get("PERSONAL_DOWNLOADER_PORT", "18765"))
INSTANCE_MUTEX = None
# 与浏览器一致的默认 UA：部分文件服务器会拒绝非浏览器 UA；接管下载时扩展还会转发真实浏览器 UA。
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


def acquire_single_instance() -> bool:
    """Keep a Windows mutex alive for this process to prevent split task queues."""
    global INSTANCE_MUTEX
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    INSTANCE_MUTEX = create_mutex(None, False, "Local\\PersonalDownloader_Instance_v1")
    if not INSTANCE_MUTEX:
        raise ctypes.WinError(ctypes.get_last_error())
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def safe_name(value: str) -> str:
    value = urllib.parse.unquote(value.split("?")[0].split("#")[0].rstrip("/").split("/")[-1])
    value = "".join(c for c in value if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
    return value or "download.bin"


def safe_filename(value: str) -> str:
    value = "".join(c for c in value if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
    return value[:180] or "download.bin"


def request(url: str, headers: dict[str, str] | None = None, byte_range: tuple[int, int] | None = None):
    h = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        h.update(headers)
    if byte_range:
        h["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    if is_youtube_media_url(url):
        response = curl_requests.get(url, headers=h, impersonate="chrome", stream=True, timeout=30)
        if response.status_code >= 400:
            body = b"".join(response.iter_content(chunk_size=2048))[:2048]
            error = urllib.error.HTTPError(
                url, response.status_code, response.reason or "HTTP error",
                response.headers, io.BytesIO(body),
            )
            response.close()
            raise error
        return CurlStreamResponse(response)
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=30)


class CurlStreamResponse:
    """Small urllib-compatible streaming wrapper for Chrome-impersonated TLS."""

    def __init__(self, response):
        self._response = response
        self._chunks = iter(response.iter_content(chunk_size=256 * 1024))
        self._buffer = bytearray()
        self._finished = False
        self.status = response.status_code
        self.headers = response.headers

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self._finished:
                self._read_chunk()
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        while len(self._buffer) < size and not self._finished:
            self._read_chunk()
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _read_chunk(self):
        try:
            self._buffer.extend(next(self._chunks))
        except StopIteration:
            self._finished = True

    def close(self):
        self._response.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def is_googlevideo_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host == "googlevideo.com" or host.endswith(".googlevideo.com")
    except ValueError:
        return False


def is_youtube_media_url(url: str) -> bool:
    """Only allow the actual temporary playback endpoint, not arbitrary googlevideo URLs."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        query = urllib.parse.parse_qs(parsed.query)
        return (
            parsed.scheme == "https"
            and (host == "googlevideo.com" or host.endswith(".googlevideo.com"))
            and parsed.path == "/videoplayback"
            and bool(query.get("itag"))
        )
    except ValueError:
        return False


def youtube_media_filename(url: str, title: str = "YouTube 视频") -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    mime = (query.get("mime") or [""])[0].lower()
    itag = (query.get("itag") or [""])[0]
    extension = ".mp4" if "video/mp4" in mime else ".webm" if "video/webm" in mime else ".m4a" if "audio/" in mime else ".bin"
    cleaned = "".join(c if c not in '<>:"/\\|?*' and ord(c) >= 32 else "_" for c in title).strip(" ._")
    cleaned = cleaned[:140] or "YouTube 视频"
    return f"{cleaned} [itag-{itag}]{extension}"


def safe_media_headers(value) -> dict[str, str]:
    """Accept only non-secret request headers needed for a captured media URL."""
    if not isinstance(value, dict):
        return {}
    allowed = {
        "user-agent": "User-Agent", "referer": "Referer", "origin": "Origin",
        "accept": "Accept", "accept-language": "Accept-Language",
        "sec-fetch-dest": "Sec-Fetch-Dest", "sec-fetch-mode": "Sec-Fetch-Mode",
        "sec-fetch-site": "Sec-Fetch-Site",
    }
    headers = {}
    for name, content in value.items():
        canonical = allowed.get(str(name).lower())
        if canonical and isinstance(content, str) and len(content) <= 2048 and "\r" not in content and "\n" not in content:
            headers[canonical] = content
    return headers


def http_error_message(error: urllib.error.HTTPError) -> str:
    """Keep a short CDN reason for diagnostics without exposing the full body."""
    message = str(error)
    if error.code != 403:
        return message
    try:
        body = error.read(2048).decode("utf-8", errors="replace")
        fields = [match.group(1).strip() for tag in ("Code", "Message", "Details")
                  if (match := re.search(fr"<{tag}>([^<]{{1,300}})</{tag}>", body, re.I))]
        if fields:
            message += "（CDN：" + ", ".join(fields) + "）"
    except Exception:
        pass
    return message


def is_youtube_page(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    except ValueError:
        return False


def is_bilibili_page(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host == "bilibili.com" or host.endswith(".bilibili.com") or host == "b23.tv"
    except ValueError:
        return False


@lru_cache(maxsize=1024)
def is_ytdlp_supported_site(url: str) -> bool:
    """yt-dlp 的 extractor 是否认识这个页面（覆盖数百个视频站点）。

    排除 GenericIE——它对任意 URL 都返回 suitable（设计上就是直接抓网页里的
    video/m3u8 引用），不排除会把白名单变成“万物放行”。
    """
    try:
        from yt_dlp.extractor import gen_extractor_classes
        for ie in gen_extractor_classes():
            # 按类名排除 GenericIE：yt-dlp 新版 lazy extractors 返回的是动态
            # 代理类（同名不同对象），身份比较（is/issubclass）无效。
            # GenericIE 对任意 URL 都 suitable（直接抓网页里的 video/m3u8
            # 引用），不排除会把白名单变成“万物放行”。
            if ie.__name__ == "GenericIE":
                continue
            try:
                if ie.suitable(url):
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def is_site_video_page(url: str) -> bool:
    # YouTube/Bilibili 走快速路径；其余站点交给 yt-dlp 的 extractor 判断。
    # yt-dlp 不认识的页面会在下载器里给出明确错误，不会造成混乱。
    try:
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            return False
    except ValueError:
        return False
    return is_youtube_page(url) or is_bilibili_page(url) or is_ytdlp_supported_site(url)


def detect_js_runtime() -> dict[str, dict]:
    """yt-dlp 解析 YouTube 需要 JS 运行时完成播放器去混淆；优先使用系统里的 node/deno。"""
    for name in ("node", "deno"):
        path = shutil.which(name)
        if path:
            return {name: {"path": path}}
    return {}


def bridge_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def cors(self):
            origin = self.headers.get("Origin", "")
            if origin.startswith(("chrome-extension://", "edge-extension://")):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-File-Name, X-Source-Url, X-Total-Bytes")
                if self.headers.get("Access-Control-Request-Private-Network") == "true":
                    self.send_header("Access-Control-Allow-Private-Network", "true")

        def do_OPTIONS(self):
            if not self.headers.get("Origin", "").startswith(("chrome-extension://", "edge-extension://")):
                self.send_error(403)
                return
            self.send_response(204)
            self.cors()
            self.end_headers()

        def do_POST(self):
            if self.path == "/api/restore" and not self.headers.get("Origin"):
                app.after(0, app._restore_from_tray)
                self.send_response(204)
                self.end_headers()
                return
            if self.path == "/api/browser-stream":
                self.do_browser_stream()
                return
            if self.path not in ("/api/tasks", "/api/direct-download") or not self.headers.get("Origin", "").startswith(("chrome-extension://", "edge-extension://")):
                self.send_error(403)
                return
            if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
                self.send_error(415)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                size_limit = 65536 if self.path == "/api/direct-download" else 8192
                if size < 1 or size > size_limit:
                    raise ValueError("invalid request body")
                data = json.loads(self.rfile.read(size).decode("utf-8"))
                url = data.get("url", "").strip()
                if self.path == "/api/tasks" and not is_site_video_page(url):
                    raise ValueError("yt-dlp 暂不支持解析该页面。可在浏览器播放视频后，用扩展媒体列表中的“发送此流”下载单条媒体流。")
                if self.path == "/api/direct-download":
                    if urllib.parse.urlparse(url).scheme not in ("http", "https") or (is_googlevideo_url(url) and not is_youtube_media_url(url)):
                        raise ValueError("只接管普通 HTTP(S) 文件，或 YouTube 播放器当前请求的媒体流")
                headers = safe_media_headers(data.get("headers"))
                filename = data.get("filename", "")
                if not isinstance(filename, str):
                    filename = ""
                filename = safe_filename(filename[:240]) if filename else ""
            except (ValueError, UnicodeError) as exc:
                self.send_response(400)
                self.cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
                return
            app.bridge_queue.put(("site" if self.path == "/api/tasks" else "direct", url, headers, filename))
            self.send_response(202)
            self.cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')

        def do_browser_stream(self):
            if not self.headers.get("Origin", "").startswith(("chrome-extension://", "edge-extension://")):
                self.send_error(403)
                return
            if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/octet-stream":
                self.send_error(415)
                return
            source_url = self.headers.get("X-Source-Url", "")
            if urllib.parse.urlparse(source_url).scheme not in ("http", "https"):
                self.send_error(400, "Invalid media source URL")
                return
            filename = safe_filename(urllib.parse.unquote(self.headers.get("X-File-Name", "YouTube 视频.mp4"))[:240])
            total = int(self.headers.get("X-Total-Bytes", "0") or 0)
            folder = Path(app.folder)
            folder.mkdir(parents=True, exist_ok=True)
            task = Task(url=source_url, filename=filename, folder=str(folder), status="downloading", total=total)
            part = folder / f"{filename}.part"
            target = folder / filename
            app.bridge_queue.put(("browser-stream", task, None, None))
            last_report = 0
            try:
                with open(part, "wb") as output:
                    transfer = self.headers.get("Transfer-Encoding", "").lower()
                    if "chunked" in transfer:
                        while True:
                            line = self.rfile.readline(128)
                            if not line:
                                raise ConnectionError("Chrome 流式传输意外中断")
                            size = int(line.split(b";", 1)[0].strip(), 16)
                            if size == 0:
                                while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                                    pass
                                break
                            remaining = size
                            while remaining:
                                block = self.rfile.read(min(256 * 1024, remaining))
                                if not block:
                                    raise ConnectionError("Chrome 流式传输意外中断")
                                output.write(block)
                                remaining -= len(block)
                                task.done += len(block)
                                if task.done - last_report >= 1024 * 1024:
                                    app.events.put(task)
                                    last_report = task.done
                            self.rfile.read(2)
                    else:
                        remaining = int(self.headers.get("Content-Length", "0") or 0)
                        if remaining <= 0:
                            raise ValueError("缺少流式请求体")
                        task.total = task.total or remaining
                        while remaining:
                            block = self.rfile.read(min(256 * 1024, remaining))
                            if not block:
                                raise ConnectionError("Chrome 流式传输意外中断")
                            output.write(block)
                            remaining -= len(block)
                            task.done += len(block)
                            if task.done - last_report >= 1024 * 1024:
                                app.events.put(task)
                                last_report = task.done
                if task.done <= 0:
                    raise ValueError("浏览器没有传回媒体数据")
                task.total = task.total or task.done
                os.replace(part, target)
                task.status = "completed"
            except Exception as exc:
                task.status = "error"
                task.error = str(exc)
            app.events.put(task)
            self.send_response(200 if task.status == "completed" else 502)
            self.cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            payload = {"accepted": task.status == "completed", "error": task.error}
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def log_message(self, _format, *_args):
            pass

    return Handler


class LocalBridgeServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


@dataclass
class Task:
    url: str
    filename: str
    folder: str
    segments: int = 8
    status: str = "queued"
    total: int = 0
    done: int = 0
    error: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class DownloadJob:
    def __init__(self, task: Task, on_update):
        self.task, self.on_update = task, on_update
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.finished_event = threading.Event()
        self.workers: list[threading.Thread] = []
        self.temp_dir = Path(task.folder) / f".{task.filename}.pdparts"

    def pause(self):
        self.pause_event.set()

    def resume(self):
        self.pause_event.clear()

    def cancel(self):
        self.stop_event.set()
        self.pause_event.clear()

    def run(self):
        try:
            if is_site_video_page(self.task.url):
                self._run_site_video()
                self.task.status = "completed"
                self.on_update(self.task)
                return
            self.task.status = "probing"
            self.on_update(self.task)
            with request(self.task.url, self.task.headers) as response:
                length = int(response.headers.get("Content-Length", "0") or 0)
                supports_range = response.headers.get("Accept-Ranges", "").lower() == "bytes"
                content_disposition = response.headers.get("Content-Disposition")
                if content_disposition:
                    message = Message()
                    message["content-disposition"] = content_disposition
                    disposition_name = message.get_filename()
                    if disposition_name:
                        self.task.filename = safe_name(disposition_name)
                if response.status == 206:
                    supports_range = True
                self.task.total = length
            if not supports_range or length <= 0 or self.task.segments <= 1:
                self._single()
            else:
                self._parallel(length)
            if not self.stop_event.is_set():
                self.task.status = "completed"
        except Exception as exc:
            self.task.status = "error"
            self.task.error = http_error_message(exc) if isinstance(exc, urllib.error.HTTPError) else str(exc)
            if is_youtube_media_url(self.task.url) and any(code in self.task.error for code in ("HTTP Error 400", "HTTP Error 403", "HTTP Error 410", "HTTP Error 404")):
                self.task.error += "\nYouTube 播放流链接可能已过期或需要当前浏览器会话。回到视频页继续播放，重新打开扩展并点该媒体项的“发送此流”；该链接只在短时间内有效。"
            if is_site_video_page(self.task.url) and any(word in self.task.error.lower() for word in ("bot", "sign in", "confirm", "403", "412", "forbidden", "unavailable", "truncated", "does not exist")):
                if is_youtube_page(self.task.url):
                    self.task.error += "\n可能原因：视频已删除、设为私有或不存在；链接不完整；或 YouTube 反机器人验证。可先在浏览器打开该视频确认能播放，再播放视频后用扩展媒体列表的“发送此流”兜底。"
                else:
                    self.task.error += "\n站点解析失败：可稍后重试，或检查视频是否需要登录、所在地区是否受限；也可播放视频后用扩展的“发送此流”兜底。"
        finally:
            self.finished_event.set()
        self.on_update(self.task)

    def _run_site_video(self):
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("站点视频下载需要 yt-dlp。请先运行「安装媒体支持.bat」，再重启下载器。") from exc

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                ffmpeg = None
        options = {
            "outtmpl": str(Path(self.task.folder) / "%(title).180B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [self._youtube_progress],
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" if ffmpeg else "best[ext=mp4]/best",
        }
        if ffmpeg:
            options["ffmpeg_location"] = ffmpeg
            options["merge_output_format"] = "mp4"
        js_runtimes = detect_js_runtime()
        if js_runtimes:
            options["js_runtimes"] = js_runtimes
        self.task.status = "extracting"
        self.task.error = ""
        self.task.total = self.task.done = 0
        self.on_update(self.task)
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(self.task.url, download=True)
            if not result:
                raise RuntimeError("yt-dlp 没有返回可下载的视频")
            # 合并后的真实文件名以 requested_downloads 为准，避免 info 里的 ext 与磁盘文件不一致。
            filepath = ""
            for entry in (result.get("requested_downloads") or []):
                if isinstance(entry, dict) and entry.get("filepath"):
                    filepath = str(entry["filepath"])
                    break
            self.task.filename = Path(filepath).name if filepath else f"{result.get('title') or 'video'} [{result.get('id') or ''}].{result.get('ext') or 'mp4'}"

    def _youtube_progress(self, data):
        if self.stop_event.is_set():
            raise RuntimeError("下载已取消")
        while self.pause_event.is_set() and not self.stop_event.is_set():
            self.task.status = "paused"
            self.on_update(self.task)
            time.sleep(.2)
        status = data.get("status")
        self.task.status = "merging" if status == "finished" else "downloading"
        self.task.done = int(data.get("downloaded_bytes") or self.task.done)
        self.task.total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or self.task.total)
        filename = data.get("filename")
        if filename:
            self.task.filename = Path(filename).name
        self.on_update(self.task)

    def _single(self):
        target = Path(self.task.folder) / self.task.filename
        part = target.with_suffix(target.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        headers = dict(self.task.headers)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            response = request(self.task.url, headers)
        except urllib.error.HTTPError as e:
            if offset and e.code == 416:
                os.replace(part, target)
                return
            if e.code == 403:
                detail = http_error_message(e)
                if detail != str(e):
                    raise RuntimeError(f"{detail}。请回到视频页继续播放并立即重新发送当前媒体流。") from e
            raise
        with response:
            if offset and response.status != 206:
                offset = 0
                mode = "wb"
            else:
                mode = "ab" if offset else "wb"
            self.task.done = offset
            with open(part, mode) as f:
                while not self.stop_event.is_set():
                    self._wait_if_paused()
                    block = response.read(256 * 1024)
                    if not block:
                        break
                    f.write(block)
                    self.task.done += len(block)
                    self.on_update(self.task)
        if not self.stop_event.is_set():
            os.replace(part, target)

    def _parallel(self, length: int):
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        n = max(1, min(int(self.task.segments), 32, length // (1024 * 1024) or 1))
        stride = (length + n - 1) // n
        ranges = [(i * stride, min(length - 1, (i + 1) * stride - 1)) for i in range(n)]
        self.task.total = length
        self.task.done = sum((self.temp_dir / f"{i}.part").stat().st_size for i in range(n) if (self.temp_dir / f"{i}.part").exists())
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(self._segment, i, start, end) for i, (start, end) in enumerate(ranges)]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        if self.stop_event.is_set():
            return
        target = Path(self.task.folder) / self.task.filename
        assembling = target.with_suffix(target.suffix + ".assembling")
        with open(assembling, "wb") as out:
            for i in range(n):
                with open(self.temp_dir / f"{i}.part", "rb") as part:
                    while True:
                        block = part.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block)
        os.replace(assembling, target)
        for p in self.temp_dir.glob("*.part"):
            p.unlink(missing_ok=True)
        self.temp_dir.rmdir()

    def _segment(self, index: int, start: int, end: int):
        path = self.temp_dir / f"{index}.part"
        existing = path.stat().st_size if path.exists() else 0
        if existing >= end - start + 1:
            return
        offset = start + existing
        with request(self.task.url, self.task.headers, byte_range=(offset, end)) as response:
            if response.status != 206:
                raise RuntimeError("服务器未正确响应 Range 请求；请将分段数设为 1 后重试")
            with open(path, "ab") as f:
                while not self.stop_event.is_set():
                    self._wait_if_paused()
                    block = response.read(256 * 1024)
                    if not block:
                        break
                    f.write(block)
                    with self.lock:
                        self.task.done += len(block)
                    self.on_update(self.task)
        if path.stat().st_size != end - start + 1:
            raise RuntimeError(f"分段 {index + 1} 长度不完整，已保留临时文件，可重试续传")

    def _wait_if_paused(self):
        while self.pause_event.is_set() and not self.stop_event.is_set():
            self.task.status = "paused"
            self.on_update(self.task)
            time.sleep(.2)
            self.task.status = "downloading"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("个人下载器")
        self.geometry("1000x650")
        self.minsize(820, 520)
        self.configure(bg="#07111f")
        self.events: queue.Queue = queue.Queue()
        self.bridge_queue: queue.Queue = queue.Queue()
        self.tasks: list[Task] = []
        self.jobs: dict[str, DownloadJob] = {}
        self._style()
        self._ui()
        self._load()
        self.tray_icon = self._create_tray_icon()
        self.tray_icon.run_detached(setup=lambda icon: setattr(icon, "visible", True))
        self.bridge_server = None
        try:
            self.bridge_server = LocalBridgeServer(("127.0.0.1", BRIDGE_PORT), bridge_handler(self))
            threading.Thread(target=self.bridge_server.serve_forever, daemon=True).start()
            self.bridge_status_label.configure(text="● 本机媒体桥接已就绪", fg="#16c4d2")
        except OSError:
            self.bridge_server = None
            self.bridge_status_label.configure(text="● 桥接端口被占用：请退出旧实例后重启", fg="#ff8a78")
            self.after(250, lambda: messagebox.showerror("下载器桥接端口被占用", "另一个下载器实例正在接收浏览器任务。请从系统托盘右键退出所有旧实例，然后重新打开此下载器。"))
        self.after(150, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

    def _create_tray_icon(self):
        image = Image.new("RGBA", (64, 64), (7, 17, 31, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 5, 59, 59), radius=15, fill=(9, 35, 52, 255), outline=(19, 198, 213, 255), width=3)
        draw.polygon([(15, 32), (30, 16), (30, 26), (49, 26), (49, 38), (30, 38), (30, 48)], fill=(19, 198, 213, 255))
        return pystray.Icon(
            "PersonalDownloader", image, "个人下载器",
            menu=pystray.Menu(
                pystray.MenuItem("打开下载器", lambda icon, item: self.after(0, self._restore_from_tray), default=True),
                pystray.MenuItem("退出", lambda icon, item: self.after(0, self._exit_app)),
            ),
        )

    def _restore_from_tray(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()
        # Windows 前台锁会拦截程序抢焦点：deiconify 出来的窗口常留在
        # 全屏应用（如 Chrome）后面。临时置顶再取消，确保窗口真正到最前。
        self._pulse_topmost(self)

    @staticmethod
    def _pulse_topmost(win):
        try:
            win.attributes("-topmost", True)
            win.after(900, lambda: win.attributes("-topmost", False))
        except Exception:
            pass

    def _reveal_for_new_task(self):
        # 新任务时主动把窗口弹出到前台（用户明确要求）：即使窗口当前可见也置前，
        # 保证浏览器里点完下载，窗口和确认框一起出现在最前面。
        try:
            self._restore_from_tray()
        except Exception:
            pass

    def _confirm_new_task(self, task: Task):
        """浏览器触发的下载任务：先弹确认框让用户核对文件名与保存路径，确认后才开始下载。"""
        result = {"ok": False, "filename": task.filename, "folder": task.folder}
        win = tk.Toplevel(self)
        win.title("新建下载任务")
        win.transient(self)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        ttk.Label(win, text="文件名").grid(row=0, column=0, sticky="w", padx=14, pady=(14, 4))
        name_var = tk.StringVar(value=task.filename)
        ttk.Entry(win, textvariable=name_var, width=48).grid(row=0, column=1, columnspan=2, padx=(0, 14), pady=(14, 4))

        ttk.Label(win, text="保存到").grid(row=1, column=0, sticky="w", padx=14, pady=4)
        folder_var = tk.StringVar(value=task.folder)
        ttk.Entry(win, textvariable=folder_var, width=48).grid(row=1, column=1, padx=(0, 6), pady=4)

        def browse():
            chosen = filedialog.askdirectory(parent=win, initialdir=folder_var.get() or None, title="选择保存位置")
            if chosen:
                folder_var.set(chosen)

        ttk.Button(win, text="浏览…", width=8, command=browse).grid(row=1, column=2, padx=(0, 14), pady=4)

        ttk.Label(win, text=task.url, foreground="#7c879a", wraplength=440, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(8, 10))

        bar = ttk.Frame(win)
        bar.grid(row=3, column=0, columnspan=3, sticky="e", padx=14, pady=(0, 12))

        def commit():
            name = safe_filename(name_var.get().strip())
            if not name:
                messagebox.showwarning("文件名无效", "请输入有效的文件名。", parent=win)
                return
            result.update(ok=True, filename=name, folder=folder_var.get().strip() or task.folder)
            win.destroy()

        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="开始下载", command=commit).pack(side="right", padx=(0, 8))

        win.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - win.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        self._pulse_topmost(win)
        win.wait_window()
        return result

    def _minimize_to_tray(self):
        # Keep a taskbar entry as a recovery path in case Windows hides the tray icon.
        self.iconify()
        try:
            self.tray_icon.visible = True
            self.tray_icon.notify("下载器仍在后台运行，可点击任务栏或托盘图标重新打开。", "个人下载器已最小化")
        except Exception:
            pass

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview", rowheight=42, background="#0c1a2b", fieldbackground="#0c1a2b", foreground="#d8e6f5", borderwidth=0, font=("Segoe UI", 10))
        s.map("Treeview", background=[("selected", "#123b54")], foreground=[("selected", "#ffffff")])
        s.configure("Treeview.Heading", background="#102239", foreground="#7f9bb8", font=("Segoe UI", 9, "bold"), relief="flat", padding=(10, 12))
        s.map("Treeview.Heading", background=[("active", "#16304b")])
        s.configure("TButton", background="#14263b", foreground="#d7e7f7", borderwidth=1, bordercolor="#29425e", focusthickness=0, padding=(13, 8), font=("Segoe UI", 9, "bold"))
        s.map("TButton", background=[("active", "#1a3855"), ("pressed", "#102239")], foreground=[("disabled", "#60748a")])
        s.configure("Accent.TButton", background="#00b8c8", foreground="#04131e", borderwidth=0)
        s.map("Accent.TButton", background=[("active", "#27d4df"), ("pressed", "#009daa")])
        s.configure("TEntry", fieldbackground="#0c1a2b", foreground="#e5f1fc", insertcolor="#42d9e6", bordercolor="#29425e", padding=10)
        s.configure("Horizontal.TProgressbar", troughcolor="#172b40", background="#13c6d5", bordercolor="#172b40", lightcolor="#13c6d5", darkcolor="#13c6d5")

    def _ui(self):
        top = tk.Frame(self, bg="#091827", padx=26, pady=21, highlightbackground="#15314a", highlightthickness=1)
        top.pack(fill="x")
        tk.Label(top, text="PERSONAL  /  DOWNLOAD CORE", bg="#091827", fg="#16c4d2", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(top, text="下载管理器", bg="#091827", fg="#e8f4ff", font=("Segoe UI", 23, "bold")).pack(anchor="w", pady=(5, 0))
        tk.Label(top, text="多线程分段  ·  断点续传  ·  网页媒体探测", bg="#091827", fg="#7f9bb8", font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 0))
        row = tk.Frame(self, bg="#07111f", padx=24, pady=18)
        row.pack(fill="x")
        self.url_var = tk.StringVar()
        url = ttk.Entry(row, textvariable=self.url_var, font=("Segoe UI", 11))
        url.pack(side="left", fill="x", expand=True, ipady=9)
        url.bind("<Button-3>", lambda event, entry=url: self._entry_context_menu(event, entry))
        url.bind("<Return>", lambda _e: self.add_task())
        ttk.Button(row, text="＋ 添加下载", style="Accent.TButton", command=self.add_task).pack(side="left", padx=(10, 0), ipady=3)
        bar = tk.Frame(self, bg="#07111f", padx=24)
        bar.pack(fill="x", pady=(0, 12))
        ttk.Button(bar, text="▶ 开始 / 继续", command=self.start_selected).pack(side="left")
        ttk.Button(bar, text="Ⅱ 暂停", command=self.pause_selected).pack(side="left", padx=8)
        ttk.Button(bar, text="✕ 移除", command=self.remove_selected).pack(side="left")
        ttk.Button(bar, text="⚙ 保存目录", command=self.choose_folder).pack(side="right")
        self.folder = str(Path.home() / "Downloads")
        self.folder_label = tk.Label(bar, text=self.folder, bg="#07111f", fg="#6e8aa5", font=("Segoe UI", 9))
        self.folder_label.pack(side="right", padx=8)
        body = tk.Frame(self, bg="#07111f", padx=24, pady=8)
        body.pack(fill="both", expand=True)
        cols = ("name", "size", "progress", "status", "url")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        for c, title, width in [("name", "文件名", 220), ("size", "大小", 100), ("progress", "进度", 180), ("status", "状态", 100), ("url", "下载地址", 360)]:
            self.tree.heading(c, text=title)
            self.tree.column(c, width=width, minwidth=75, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-3>", self._tree_context_menu)
        foot = tk.Frame(self, bg="#07111f", padx=24, pady=14)
        foot.pack(fill="x")
        self.bridge_status_label = tk.Label(foot, text="● 本机媒体桥接启动中", bg="#07111f", fg="#16c4d2", font=("Segoe UI", 9))
        self.bridge_status_label.pack(anchor="w")
        tk.Label(foot, text="YouTube / Bilibili：在扩展中发送视频页面，下载器会解析完整视频流", bg="#07111f", fg="#718ca5", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder)
        if folder:
            self.folder = folder
            self.folder_label.configure(text=folder)

    def _entry_context_menu(self, event, entry):
        if not entry.selection_present():
            entry.icursor(entry.index(f"@{event.x}"))
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="剪切", command=lambda: entry.event_generate("<<Cut>>"))
        menu.add_command(label="复制", command=lambda: entry.event_generate("<<Copy>>"))
        menu.add_command(label="粘贴", command=lambda: entry.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: entry.select_range(0, "end"))
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()
        return "break"

    def _tree_context_menu(self, event):
        """右键下载任务：打开文件或打开所在文件夹（仅对已存在且路径非法的项禁用）。"""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        task = next((t for t in self.tasks if str(id(t)) == iid), None)
        if not task:
            return
        folder = Path(task.folder).resolve()
        target = (folder / task.filename).resolve()
        safe = Path(task.filename).name == task.filename and target.parent == folder
        file_ready = safe and target.is_file()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="打开文件", state="normal" if file_ready else "disabled",
                         command=lambda: self._open_task_file(target))
        menu.add_command(label="打开文件夹", state="normal" if safe and folder.is_dir() else "disabled",
                         command=lambda: self._open_task_folder(target))
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    @staticmethod
    def _open_task_file(target: Path):
        try:
            os.startfile(str(target))
        except OSError as exc:
            messagebox.showwarning("无法打开文件", f"{target}\n{exc}")

    @staticmethod
    def _open_task_folder(target: Path):
        try:
            if os.name == "nt" and target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(target.parent))
        except OSError as exc:
            messagebox.showwarning("无法打开文件夹", f"{target.parent}\n{exc}")

    def add_task(self):
        url = self.url_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showerror("链接无效", "请输入 http:// 或 https:// 下载地址")
            return
        if is_googlevideo_url(url) and not is_youtube_media_url(url):
            messagebox.showerror("媒体链接无效", "只支持 YouTube 播放器当前请求的 videoplayback 媒体链接。请播放视频后，从扩展中发送已探测到的媒体流。")
            return
        filename = youtube_media_filename(url) if is_youtube_media_url(url) else safe_name(url)
        task = Task(url=url, filename=filename, folder=self.folder)
        self.tasks.append(task)
        self.url_var.set("")
        self._render(task)
        self._save()

    def _render(self, task: Task):
        iid = str(id(task))
        if not self.tree.exists(iid):
            self.tree.insert("", "end", iid=iid, values=(task.filename, self._size(task.total), "", task.status, task.url))
        else:
            progress = f"失败：{task.error[:52]}" if task.status == "error" else f"{task.done / task.total * 100:.1f}%" if task.total else "探测中"
            self.tree.item(iid, values=(task.filename, self._size(task.total), f"{progress}  ({self._size(task.done)})", task.status, task.url))

    @staticmethod
    def _size(n):
        if not n:
            return "—"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
            n /= 1024

    def start_selected(self):
        task = self._selected()
        if not task:
            return
        self.start_task(task)

    def start_task(self, task: Task):
        if id(task) in self.jobs:
            self.jobs[id(task)].resume()
            return
        if task.status in ("queued", "paused", "error"):
            task.error = ""
            job = DownloadJob(task, lambda t: self.events.put(t))
            self.jobs[id(task)] = job
            task.status = "downloading"
            threading.Thread(target=job.run, daemon=True).start()

    def pause_selected(self):
        task = self._selected()
        if task and id(task) in self.jobs:
            self.jobs[id(task)].pause()
            task.status = "paused"
            self._render(task)
            self._save()

    def remove_selected(self):
        selected = set(self.tree.selection())
        tasks = [task for task in self.tasks if str(id(task)) in selected]
        if not tasks:
            return
        count = len(tasks)
        delete_files = messagebox.askyesnocancel(
            "移除下载任务",
            f"确定移除选中的 {count} 个任务吗？\n\n选择“是”会同时删除已下载文件和临时分段文件；\n选择“否”只从列表移除任务，保留文件；\n选择“取消”返回。",
        )
        if delete_files is None:
            return
        for task in tasks:
            job = self.jobs.pop(id(task), None)
            if job:
                job.cancel()
            self.tasks.remove(task)
            self.tree.delete(str(id(task)))
            if delete_files:
                if job:
                    threading.Thread(target=self._cleanup_after_job, args=(task, job), daemon=True).start()
                else:
                    try:
                        self._delete_task_files(task)
                    except OSError as exc:
                        messagebox.showwarning("文件未能完全删除", f"{task.filename}\n{exc}")
        self._save()

    def _cleanup_after_job(self, task: Task, job: DownloadJob):
        job.finished_event.wait()
        try:
            self._delete_task_files(task)
        except OSError as exc:
            error = str(exc)
            self.after(0, lambda: messagebox.showwarning("文件未能完全删除", f"{task.filename}\n{error}"))

    @staticmethod
    def _delete_task_files(task: Task):
        folder = Path(task.folder).resolve()
        if Path(task.filename).name != task.filename:
            raise OSError("任务文件名异常，已跳过删除以保护其他文件。")
        target = (folder / task.filename).resolve()
        if target.parent != folder:
            raise OSError("任务文件名不在下载目录内，已跳过删除以保护其他文件。")
        assembling = target.with_suffix(target.suffix + ".assembling")
        temp_dir = folder / f".{task.filename}.pdparts"
        partial = target.with_suffix(target.suffix + ".part")
        ytdl_temp = target.with_suffix(target.suffix + ".ytdl")
        for path in (target, partial, assembling, ytdl_temp):
            if path.exists() and path.is_file():
                path.unlink()
        if temp_dir.exists():
            if temp_dir.resolve().parent != folder:
                raise OSError("临时文件目录不在下载目录内，已跳过删除。")
            shutil.rmtree(temp_dir)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((t for t in self.tasks if str(id(t)) == sel[0]), None)

    def _drain(self):
        while not self.bridge_queue.empty():
            kind, url, headers, filename_hint = self.bridge_queue.get()
            if kind == "browser-stream" and isinstance(url, Task):
                task = url
                self.tasks.append(task)
                self._render(task)
                self._save()
                self._reveal_for_new_task()
                continue
            if not any(task.url == url and task.status not in ("completed", "error") for task in self.tasks):
                filename = "视频（解析中）" if kind == "site" else (youtube_media_filename(url, filename_hint or "YouTube 视频") if is_youtube_media_url(url) else (filename_hint or safe_name(url)))
                task = Task(url=url, filename=filename, folder=self.folder, headers=headers)
                self.tasks.append(task)
                self._render(task)
                self._save()
                if kind in ("direct", "site"):
                    self._reveal_for_new_task()
                    confirmed = self._confirm_new_task(task)
                    if not confirmed["ok"]:
                        # 用户取消确认：移除任务，不开始下载。
                        self.tasks.remove(task)
                        self.tree.delete(str(id(task)))
                        self._save()
                        continue
                    task.filename = confirmed["filename"]
                    task.folder = confirmed["folder"]
                    self._render(task)
                    self.start_task(task)
        while not self.events.empty():
            task = self.events.get()
            if task in self.tasks:
                self._render(task)
            if task.status in ("completed", "error"):
                self.jobs.pop(id(task), None)
                if task.status == "error" and task in self.tasks:
                    messagebox.showerror("下载失败", task.error)
            self._save()
        self.after(150, self._drain)

    def _load(self):
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf8"))
            self.folder = raw.get("folder", self.folder)
            self.folder_label.configure(text=self.folder)
            # 过滤掉旧版本状态文件里的已移除字段（如历史 Cookie 设置），保证兼容加载。
            fields = set(Task.__dataclass_fields__)
            self.tasks = [
                Task(**{key: value for key, value in item.items() if key in fields})
                for item in raw.get("tasks", []) if item.get("status") != "completed"
            ]
            for t in self.tasks:
                if t.status in ("downloading", "probing"):
                    t.status = "paused"
                self._render(t)
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"folder": self.folder, "tasks": [asdict(t) for t in self.tasks]}
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf8")
            tmp.replace(STATE_FILE)
        except OSError:
            # 杀毒软件或外部工具短暂占用状态文件时，重试而不是让异常打断 Tk 事件循环
            # （_drain 每 150ms 调用一次，异常逃逸会导致 after 链断裂、界面假死）。
            if not getattr(self, "_save_retry_pending", False):
                self._save_retry_pending = True
                self.after(300, self._save_retry)

    def _save_retry(self):
        self._save_retry_pending = False
        self._save()

    def _exit_app(self):
        for job in self.jobs.values():
            job.pause()
        self._save()
        if self.bridge_server:
            self.bridge_server.shutdown()
            self.bridge_server.server_close()
        self.tray_icon.stop()
        if INSTANCE_MUTEX and os.name == "nt":
            close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_bool
            close_handle(INSTANCE_MUTEX)
        self.destroy()


if __name__ == "__main__":
    if not acquire_single_instance():
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{BRIDGE_PORT}/api/restore", data=b"", method="POST"), timeout=2).close()
        except Exception:
            ctypes.windll.user32.MessageBoxW(0, "下载器已经在运行。请在任务栏通知区域的“显示隐藏的图标”中查找下载器图标。", "个人下载器", 0x40)
    else:
        App().mainloop()
