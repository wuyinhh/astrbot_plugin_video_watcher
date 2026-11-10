"""B 站链接识别及下载；也可作为独立子进程执行，以便超时后彻底停止下载。"""

import html
import json
import re
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def normalize_url(value: str) -> str | None:
    """只接受 B 站普通视频页和 b23 短链，保留分 P，去掉分享跟踪参数。"""
    try:
        url = urlsplit(html.unescape(value.strip()))
        if url.scheme not in {"http", "https"} or url.username or url.password:
            return None
        if url.port not in {None, 80, 443}:
            return None
        if url.hostname in {"b23.tv", "www.b23.tv"}:
            if re.fullmatch(r"/[A-Za-z0-9]+/?", url.path):
                return "https://b23.tv/" + url.path.strip("/")
        if url.hostname in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
            match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av\d+)/?", url.path)
            if match:
                result = "https://www.bilibili.com/video/" + match[1]
                part = parse_qs(url.query).get("p", [""])[0]
                if part.isascii() and part.isdigit() and int(part) > 0:
                    result += "?" + urlencode({"p": int(part)})
                return result
    except ValueError:
        pass
    return None


def find_urls(value, depth: int = 0) -> list[str]:
    """兼容普通文本、QQ JSON 卡片、转义后的卡片和原始消息段。"""
    if depth > 12:
        return []
    found = []
    if isinstance(value, str):
        text = html.unescape(value).replace(r"\/", "/")
        try:
            decoded = json.loads(text)
        except (ValueError, RecursionError):
            decoded = None
        if isinstance(decoded, (dict, list, str)) and decoded != value:
            return find_urls(decoded, depth + 1)
        for candidate in re.findall(r"https?://[^\s<>\"'\\\[\]{}，。！？、；（）【】]+", text):
            url = normalize_url(candidate.rstrip(".,;:!?)"))
            if url:
                found.append(url)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(find_urls(item, depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(find_urls(item, depth + 1))
    return list(dict.fromkeys(found))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_url(url: str) -> str:
    """逐次检查短链跳转目标，仅允许 B 站短链或普通视频页。"""
    current = normalize_url(url)
    if not current:
        raise ValueError("目前只支持 B 站普通视频链接和 b23.tv 短链接。")
    opener = build_opener(_NoRedirect())
    for _ in range(5):
        if urlsplit(current).hostname == "www.bilibili.com":
            return current
        request = Request(current, headers={"User-Agent": "Mozilla/5.0"})
        try:
            response = opener.open(request, timeout=20)
        except HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                exc.close()
                raise
            response = exc
        with response:
            location = response.headers.get("Location")
        target = normalize_url(urljoin(current, location)) if location else None
        if not target:
            raise ValueError("短链接没有指向支持的 B 站视频，请复制视频页地址重试。")
        current = target
    raise ValueError("B 站短链接跳转次数过多，请复制视频页地址重试。")


def _cdn_preference(url: str) -> int:
    """常规 CDN 优先，MCDN/PCDN 和非标准端口留作最后的备选。"""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return -10 if (
        parsed.port not in {None, 80, 443}
        or any(part in host.split(".") for part in ("mcdn", "pcdn", "szbdyd"))
    ) else 0


def _expand_cdn_formats(play_info: dict, formats: list[dict]) -> list[dict]:
    """保留 B 站返回的签名备用地址，不自行替换主机名或签名参数。"""
    backups = {}
    streams = [*(play_info.get("dash") or {}).get("video", []),
               *play_info.get("durl", [])]
    for stream in streams:
        primary = stream.get("baseUrl") or stream.get("base_url") or stream.get("url")
        alternatives = stream.get("backupUrl") or stream.get("backup_url") or []
        if isinstance(alternatives, str):
            alternatives = [alternatives]
        backups[primary] = alternatives
    expanded = []
    for fmt in formats:
        # 多段 FLV 的拼接由上游处理，不能把单段备用地址当成整个视频。
        if fmt.get("vcodec") == "none" or fmt.get("fragments"):
            expanded.append(fmt)
            continue
        urls = dict.fromkeys([fmt["url"], *backups.get(fmt["url"], [])])
        for index, url in enumerate(urls):
            if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
                continue
            expanded.append({
                **fmt, "url": url,
                "format_id": f"{fmt.get('format_id') or 'video'}-cdn{index}",
                "source_preference": _cdn_preference(url),
            })
    return expanded


def _bilibili_extractor():
    from yt_dlp.extractor.bilibili import BiliBiliIE as BaseBiliBiliIE

    # 保持同一个 ie_key，仅替换本次下载器中的 B 站格式解析。
    class BiliBiliIE(BaseBiliBiliIE):
        def extract_formats(self, play_info):
            return _expand_cdn_formats(play_info, super().extract_formats(play_info))

    return BiliBiliIE()


def download_video(url: str, directory: Path, max_mb: int) -> Path:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    url = resolve_url(url)
    limit = max_mb * 1024 * 1024

    def check_size(progress):
        if progress.get("downloaded_bytes", 0) > limit:
            raise ValueError(f"视频超过 {max_mb} MB，已停止下载。")

    class QuietLogger:
        def debug(self, message):
            pass

        def warning(self, message):
            pass

        def error(self, message):
            pass

    options = {
        # 只需要画面，不下载音轨、不合并，优先选择 720p 及以下的视频流。
        "format": "bestvideo[height<=720]/best[height<=720]/worstvideo/worst",
        # 同一视频的不同编码可能走不同 CDN，跳过实际无法访问的视频流。
        "check_formats": "selected",
        "format_sort": ["source"],
        # 部分 B 站 CDN 要求 Range 请求；分块下载只作用于视频，不影响网页/API。
        "http_chunk_size": 4 * 1024 * 1024,
        "outtmpl": str(directory / "source.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "allowed_extractors": ["BiliBili"],
        "max_filesize": limit,
        "progress_hooks": [check_size],
        "socket_timeout": 10,
        "retries": 0,
        "fragment_retries": 1,
        "cachedir": False,
        "logger": QuietLogger(),
    }
    with YoutubeDL(options) as downloader:
        downloader.add_info_extractor(_bilibili_extractor())
        info = downloader.extract_info(url, download=False, process=False)
        if not info or info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError("未取得单个视频，请使用具体视频页链接。")
        failed_urls = set()
        last_error = None
        for attempt in range(3):
            candidate = deepcopy(info)
            candidate["formats"] = [
                fmt for fmt in candidate.get("formats", [])
                if fmt.get("url") not in failed_urls and fmt.get("vcodec") != "none"
            ]
            if not candidate["formats"] and last_error is not None:
                raise last_error
            # 每条线路单独保存，切换编码或节点时不续写上一份残缺文件。
            with tempfile.TemporaryDirectory(prefix=f"attempt_{attempt}_", dir=directory) as attempt_folder:
                attempt_dir = Path(attempt_folder)
                downloader.params["outtmpl"]["default"] = str(attempt_dir / "source.%(ext)s")
                selected = downloader.process_ie_result(candidate, download=False)
                try:
                    downloader.process_info(selected)
                except DownloadError as exc:
                    if attempt == 2:
                        raise
                    last_error = exc
                    failed_urls.add(selected["url"])
                    continue
                downloaded = Path(downloader.prepare_filename(selected)).resolve()
                if downloaded.parent != attempt_dir.resolve() or not downloaded.is_file():
                    raise ValueError(f"未下载到视频，文件可能超过 {max_mb} MB 或无法访问。")
                path = directory.resolve() / downloaded.name
                downloaded.replace(path)
                break
    if path.parent != directory.resolve() or not path.is_file():
        raise ValueError(f"未下载到视频，文件可能超过 {max_mb} MB 或无法访问。")
    if path.stat().st_size > limit:
        raise ValueError(f"视频超过 {max_mb} MB，已停止处理。")
    return path


if __name__ == "__main__":
    try:
        result = download_video(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]))
        print(json.dumps({"path": str(result)}, ensure_ascii=True))
    except ImportError:
        print("缺少下载依赖，请在 AstrBot 环境安装 requirements.txt。", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
