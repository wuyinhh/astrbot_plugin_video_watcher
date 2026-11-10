import asyncio
import re
import shutil
import tempfile
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star

# ---- 可调参数 ----
MAX_FRAMES = 6          # 抽几帧
FRAME_WIDTH = 640       # 帧图宽度（越大越清晰，也越费 token）
MAX_VIDEO_MB = 500      # 超过这个大小直接拒绝
FFMPEG_TIMEOUT = 180    # 单次 ffmpeg 超时（秒）


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


async def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
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
        """看视频：附带或引用一个视频，让 AI 描述画面。"""
        video = None
        for comp in event.get_messages():
            if isinstance(comp, Video):
                video = comp
                break
        if video is None:
            yield event.plain_result("把视频和 /看视频 一起发，或者引用一条视频再发 /看视频。")
            return
        try:
            path = await video.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拿不到视频文件：{exc}")
            return
        umo = getattr(event, "unified_msg_origin", None)
        yield event.plain_result(await self._describe(path, "", umo))

    @llm_tool(name="describe_video")
    async def describe_video(
        self,
        event: AstrMessageEvent,
        path: str,
        question: str = "",
    ) -> str:
        """查看一个本地视频文件的画面内容（抽帧后交给视觉模型描述）。

        Args:
            path(string): 视频文件的绝对路径，通常来自消息里的 [Video Attachment: name ... path ...]。
            question(string): 想从画面里确认的具体问题，留空则做整体描述。
        """
        umo = getattr(event, "unified_msg_origin", None)
        return await self._describe(path, question, umo)
