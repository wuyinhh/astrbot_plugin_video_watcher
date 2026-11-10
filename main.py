import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Json, Plain, Reply, Share, Video
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .bilibili import find_urls

# ---- 可调参数 ----
MAX_FRAMES = 6          # 抽几帧
FRAME_WIDTH = 640       # 帧图宽度（越大越清晰，也越费 token）
MAX_VIDEO_MB = 500      # 超过这个大小直接拒绝
FFMPEG_TIMEOUT = 180    # 单次 ffmpeg 超时（秒）
DOWNLOAD_TIMEOUT = 180  # 链接解析及下载总超时（秒）


def _message_links(event: AstrMessageEvent) -> list[str]:
    # 本条消息中的地址优先于引用消息；QQ 卡片不会出现在纯文本里。
    links = find_urls(event.get_message_str())

    def visit(chain, depth=0):
        if depth > 8:
            return
        for comp in chain:
            if isinstance(comp, Plain):
                links.extend(find_urls(comp.text))
            elif isinstance(comp, Json):
                links.extend(find_urls(comp.data))
            elif isinstance(comp, Share):
                links.extend(find_urls(comp.url))
        for comp in chain:
            if isinstance(comp, Reply):
                links.extend(find_urls(comp.message_str or comp.text))
                visit(comp.chain or [], depth + 1)

    visit(event.get_messages())
    return list(dict.fromkeys(links))


def _find_video(chain, depth=0):
    if depth > 8:
        return None
    for comp in chain:
        if isinstance(comp, Video):
            return comp
    for comp in chain:
        if isinstance(comp, Reply):
            video = _find_video(comp.chain or [], depth + 1)
            if video:
                return video
    return None


def _ffmpeg_path() -> str | None:
    """优先用 imageio-ffmpeg 自带的静态 ffmpeg，找不到再回退到 PATH。"""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


async def _run(
    cmd: list[str], timeout: int = 120, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        return -1, "", "timeout"
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="ignore"),
        err.decode("utf-8", errors="ignore"),
    )


async def _probe_duration(ffmpeg: str, path: str) -> float:
    _, _, err = await _run([ffmpeg, "-hide_banner", "-i", path], timeout=30)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


async def _extract_frames(ffmpeg: str, video: Path, out_dir: Path) -> list[str]:
    duration = await _probe_duration(ffmpeg, str(video))
    if duration <= 0:
        # 拿不到时长（流式/损坏），退回按固定间隔取样
        await _run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video),
                "-vf", f"fps=1/5,scale={FRAME_WIDTH}:-2",
                "-frames:v", str(MAX_FRAMES),
                str(out_dir / "f_%02d.jpg"),
            ],
            timeout=FFMPEG_TIMEOUT,
        )
    else:
        for i in range(MAX_FRAMES):
            ts = duration * (i + 0.5) / MAX_FRAMES
            await _run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{ts:.2f}", "-i", str(video),
                    "-frames:v", "1",
                    "-vf", f"scale={FRAME_WIDTH}:-2",
                    "-q:v", "3",
                    str(out_dir / f"f_{i:02d}.jpg"),
                ],
                timeout=60,
            )
    return sorted(str(p) for p in out_dir.glob("f_*.jpg"))


class VideoWatcher(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._download_slots = asyncio.Semaphore(2)

    async def _describe_source(self, source: str, question: str, umo: str | None) -> str:
        source = (source or "").strip().strip('"').strip("'")
        urls = find_urls(source)
        if not urls:
            if source.lower().startswith(("http://", "https://")):
                return "目前支持 B 站普通视频链接、b23.tv 短链接或直接发送的视频文件。"
            return await self._describe(source, question, umo)
        if not _ffmpeg_path():
            return "找不到 ffmpeg。请先在 AstrBot 环境安装 requirements.txt。"
        async with self._download_slots:
            # 子进程结束后再删目录，超时/取消时不会留下继续下载的后台线程。
            with tempfile.TemporaryDirectory(prefix="vw_bili_") as directory:
                # AstrBot 可能将插件依赖加到 sys.path，而非安装到 Python 默认目录。
                download_env = os.environ.copy()
                download_env["PYTHONPATH"] = os.pathsep.join(
                    str(Path(path).resolve()) for path in sys.path if path
                )
                code, out, err = await _run(
                    [sys.executable, "-X", "utf8", str(Path(__file__).with_name("bilibili.py")),
                     urls[0], directory, str(MAX_VIDEO_MB)],
                    timeout=DOWNLOAD_TIMEOUT,
                    env=download_env,
                )
                if code == -1:
                    return "B 站视频下载超时，请稍后重试，或者直接发送视频文件。"
                if code != 0:
                    logger.warning(f"[video_watcher] B 站下载失败: {err[-1000:]}")
                    return (
                        "B 站视频下载失败。视频可能需要登录、已失效，或当前网络被 B 站限制。"
                        "请确认 AstrBot 已安装最新 requirements.txt，或直接发送视频文件。"
                    )
                try:
                    path = Path(json.loads(out)["path"]).resolve()
                    if path.parent != Path(directory).resolve() or not path.is_file():
                        raise ValueError("下载文件路径无效")
                except (ValueError, KeyError, TypeError):
                    return "B 站视频下载结果无效，请稍后重试。"
                return await self._describe(str(path), question, umo)

    @filter.on_llm_request()
    async def expose_video_links(self, event: AstrMessageEvent, req: ProviderRequest):
        """让模型也能看到 QQ 分享卡片及引用消息中的实际视频地址。"""
        links = _message_links(event)
        if links:
            note = (
                "[视频观察员：本条消息或引用消息包含 B 站视频链接]\n"
                + "\n".join(links[:5])
                + "\n需要了解视频画面时，可调用 describe_video，path 填上面的链接。"
                "工具返回前不要声称已经看过视频。"
            )
            if note not in (req.prompt or ""):
                req.prompt = (req.prompt or "") + "\n\n" + note

    async def _describe(self, video_path: str, question: str, umo: str | None) -> str:
        raw = (video_path or "").strip().strip('"').strip("'")
        if raw.startswith("file://"):
            raw = raw[7:]
        p = Path(raw)
        if not p.is_file():
            return f"视频文件不存在：{video_path}"

        size_mb = p.stat().st_size / 1048576
        if size_mb > MAX_VIDEO_MB:
            return f"视频太大（{size_mb:.0f} MB），跳过。"

        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            return "找不到 ffmpeg。请先安装依赖：pip install imageio-ffmpeg"

        tmp = Path(tempfile.mkdtemp(prefix="vw_"))
        try:
            frames = await _extract_frames(ffmpeg, p, tmp)
            if not frames:
                return "抽帧失败，可能不是有效的视频文件（或编码不受支持）。"

            provider = self.context.get_using_provider(umo)
            if not provider:
                return "没有可用的对话模型提供商。"

            prompt = question.strip() or (
                "这是从一段视频里均匀抽取的几帧画面。请用中文描述视频内容："
                "场景、出现了什么人/物、在做什么、画面上有文字就照抄。"
                "看不清就说看不清，不要编造。"
            )
            resp = await provider.text_chat(prompt=prompt, image_urls=frames)
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text or "模型没有返回内容。"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[video_watcher] 处理视频失败: {exc}")
            return f"处理视频时出错：{exc}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @filter.command("看视频")
    async def watch_video_cmd(self, event: AstrMessageEvent):
        """看视频：发送或引用视频、B 站链接或分享卡片。"""
        umo = getattr(event, "unified_msg_origin", None)
        links = _message_links(event)
        if links:
            yield event.plain_result(await self._describe_source(links[0], "", umo))
            return
        video = _find_video(event.get_messages())
        if video is None:
            yield event.plain_result(
                "发送 /看视频 B站链接，或引用别人发的视频、B站链接/分享卡片后发送 /看视频。"
            )
            return
        try:
            path = await video.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拿不到视频文件：{exc}")
            return
        yield event.plain_result(await self._describe(path, "", umo))

    @llm_tool(name="describe_video")
    async def describe_video(
        self,
        event: AstrMessageEvent,
        path: str = "",
        question: str = "",
    ) -> str:
        """查看视频画面，支持 B 站链接、b23.tv 短链接和本地视频文件。

        用户发来或引用 B 站视频/分享卡片，并想知道画面内容时使用。
        自动下载视频并抽帧，不能听声音；不要仅凭卡片标题推测视频内容。

        Args:
            path(string): B站视频链接、b23.tv短链接或本地视频绝对路径；留空则从本条消息或引用的分享卡片中提取链接。
            question(string): 想从画面里确认的具体问题，留空则做整体描述。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not path.strip():
            links = _message_links(event)
            if not links:
                return "本条消息及引用消息中没有找到 B 站链接，请提供视频链接或本地视频路径。"
            path = links[0]
        return await self._describe_source(path, question, umo)
