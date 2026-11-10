"""离线回归测试；AstrBot 接口用最小替身，避免依赖正在运行的机器人。"""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
        "astrbot.api.message_components", "astrbot.api.provider", "video_watcher_test",
    )}
    modules["video_watcher_test"].__path__ = [str(ROOT)]
    api = modules["astrbot.api"]
    api.logger = logging.getLogger("video_watcher_test")
    api.llm_tool = lambda **kwargs: lambda func: func
    event = modules["astrbot.api.event"]
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        command=lambda *a, **kw: lambda func: func,
        on_llm_request=lambda *a, **kw: lambda func: func,
    )
    components = modules["astrbot.api.message_components"]
    for name in ("Plain", "Json", "Reply", "Share", "Video"):
        setattr(components, name, type(name, (), {
            "__init__": lambda self, **kw: self.__dict__.update(kw),
        }))
    star = modules["astrbot.api.star"]
    star.Context = object
    star.Star = type("Star", (), {"__init__": lambda self, ctx: setattr(self, "context", ctx)})
    modules["astrbot.api.provider"].ProviderRequest = object
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("video_watcher_test.main", ROOT / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bili = sys.modules["video_watcher_test.bilibili"]
    return module, bili, components


plugin, bili, comp = load_plugin()
URL = "https://www.bilibili.com/video/BV1xx411c7mD"
SHORT = "https://b23.tv/AbC123"


def make_event(chain=(), text=""):
    extras = {}
    return types.SimpleNamespace(
        get_messages=lambda: chain,
        get_message_str=lambda: text,
        unified_msg_origin="test:group:123",
        plain_result=lambda value: value,
        get_extra=lambda key, default=None: extras.get(key, default),
        set_extra=lambda key, value: extras.__setitem__(key, value),
        request_llm=lambda **kwargs: types.SimpleNamespace(kind="llm_request", **kwargs),
    )


class LinkTests(unittest.TestCase):
    def test_text_punctuation_mobile_and_parts(self):
        text = "看这个（https://m.bilibili.com/video/BV1xx411c7mD/?p=2&share_source=qq）。"
        self.assertEqual(bili.find_urls(text), [URL + "?p=2"])

    def test_json_card_and_escaped_json(self):
        card = {"meta": {"detail_1": {"qqdocurl": SHORT + "?share_source=qq"}}}
        for value in (card, json.dumps(card), json.dumps(card).replace("/", r"\/")):
            self.assertEqual(bili.find_urls(value), [SHORT])

    def test_no_unrelated_or_disguised_hosts(self):
        for value in (
            "https://b23.tv.evil.example/AbC123", "https://b23.tv@evil.example/AbC123",
            "https://www.bilibili.com.evil.example/video/BV1xx411c7mD",
            "https://www.bilibili.com:8443/video/BV1xx411c7mD",
            "https://live.bilibili.com/123", "https://space.bilibili.com/123",
            "https://example.com/a.mp4", "file:///tmp/video.mp4",
        ):
            with self.subTest(value=value):
                self.assertEqual(bili.find_urls(value), [])

    def test_reply_card_and_direct_link_precedence(self):
        reply = comp.Reply(chain=[comp.Json(data={"meta": {"news": {"jumpUrl": SHORT}}})],
                           message_str="", text="")
        event = make_event([reply, comp.Plain(text=URL)], URL)
        self.assertEqual(plugin._message_links(event), [URL, SHORT])

    def test_quoted_video_is_found(self):
        video = comp.Video()
        self.assertIs(plugin._find_video([comp.Reply(chain=[video])]), video)

    def test_short_link_redirect_preserves_part(self):
        response = Mock(headers={"Location": URL + "?p=3&share_source=qq"})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(bili, "build_opener", return_value=opener):
            self.assertEqual(bili.resolve_url(SHORT), URL + "?p=3")
        opener.open.assert_called_once()

    def test_short_link_does_not_follow_unrelated_redirect(self):
        response = Mock(headers={"Location": "http://127.0.0.1/private"})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(bili, "build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                bili.resolve_url(SHORT)
        opener.open.assert_called_once()

    def test_redirect_http_error_is_handled_and_closed(self):
        from io import BytesIO

        body = BytesIO(b"")
        response = bili.HTTPError(SHORT, 302, "Found", {"Location": URL}, body)
        opener = Mock()
        opener.open.side_effect = response
        with patch.object(bili, "build_opener", return_value=opener):
            self.assertEqual(bili.resolve_url(SHORT), URL)
        self.assertTrue(body.closed)

    def test_download_stops_when_stream_has_unknown_size_and_exceeds_limit(self):
        class FakeDownloader:
            def __init__(self, options):
                self.hook = options["progress_hooks"][0]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download):
                self.hook({"downloaded_bytes": 1024 * 1024 + 1})

        with patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=FakeDownloader)}):
            with self.assertRaisesRegex(ValueError, "超过 1 MB"):
                bili.download_video(URL, ROOT, 1)


class DownloadRegressionTests(unittest.TestCase):
    def test_server_requiring_range_downloads_complete_file(self):
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            self.skipTest("需要安装 yt-dlp 才能测试真实 HTTP 下载器")

        payload = b"range-only-video" * 400000
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                value = self.headers.get("Range")
                requests.append(value)
                if not value:
                    self.send_error(567, "Range required")
                    return
                start, end = value.removeprefix("bytes=").split("-", 1)
                start = int(start)
                end = min(int(end) if end else len(payload) - 1, len(payload) - 1)
                if start >= len(payload):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(payload)}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                self.wfile.write(payload[start:end + 1])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def extract_info(downloader, url, download):
            # 仅替换远程视频元数据解析；格式试读和正式下载均使用真实 yt-dlp。
            # 测试仅访问本机 HTTP 服务，不使用系统代理。
            downloader.params["proxy"] = ""
            return downloader.process_ie_result({
                "id": "range-test",
                "title": "range-test",
                "extractor": "test",
                "webpage_url": URL,
                "formats": [{
                    "format_id": "test",
                    "url": f"http://127.0.0.1:{server.server_port}/video.mp4",
                    "ext": "mp4",
                    "height": 360,
                    "vcodec": "avc1",
                    "acodec": "none",
                }],
            }, download=download)

        try:
            with tempfile.TemporaryDirectory(prefix="vw_range_test_") as directory:
                with patch.object(YoutubeDL, "extract_info", extract_info):
                    path = bili.download_video(URL, Path(directory), 10)
                self.assertEqual(path.read_bytes(), payload)
                self.assertGreater(len(requests), 2)
                self.assertTrue(all(requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class SamplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ffmpeg_samples_one_frame_per_second_in_batches(self):
        ffmpeg = plugin._ffmpeg_path()
        if not ffmpeg:
            self.skipTest("需要 imageio-ffmpeg 才能验证真实抽帧")
        for seconds, expected in ((0.2, 1), (60, 60), (65.4, 66)):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory(prefix="vw_sampling_") as folder:
                root = Path(folder)
                video = root / "sample.mp4"
                code, _, err = await plugin._run([
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
                    "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", str(video),
                ])
                self.assertEqual(code, 0, err)
                all_names = []
                for start in range(0, expected, plugin.FRAMES_PER_BATCH):
                    with tempfile.TemporaryDirectory(dir=root) as batch:
                        frames = await plugin._extract_frames(
                            ffmpeg, video, Path(batch), start,
                            min(plugin.FRAMES_PER_BATCH, expected - start),
                        )
                        self.assertLessEqual(len(frames), plugin.FRAMES_PER_BATCH)
                        all_names.extend(Path(path).name for path in frames)
                self.assertEqual(all_names, [f"f_{i:08d}.jpg" for i in range(expected)])

    async def test_every_frame_is_analyzed_and_previous_batch_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="vw_analysis_") as folder:
            video = Path(folder) / "sample.mp4"
            video.write_bytes(b"video")
            seen, calls = [], []

            async def extract(ffmpeg, path, out_dir, start, count):
                self.assertTrue(all(not old.exists() for old in seen))
                frames = [out_dir / f"f_{i:08d}.jpg" for i in range(start, start + count)]
                for frame in frames:
                    frame.write_bytes(b"image")
                seen.extend(frames)
                return list(map(str, frames))

            async def text_chat(prompt, image_urls):
                self.assertTrue(all(Path(path).is_file() for path in image_urls))
                calls.append(len(image_urls))
                return types.SimpleNamespace(completion_text="画面观察")

            provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=text_chat))
            watcher = plugin.VideoWatcher(types.SimpleNamespace(get_using_provider=lambda umo: provider))
            with patch.object(plugin, "_extract_frames", side_effect=extract), patch.object(
                plugin, "_probe_duration", AsyncMock(return_value=25.3)
            ), patch.object(plugin, "_ffmpeg_path", return_value="ffmpeg"):
                result = await watcher._describe(str(video), "有什么变化？", "session")
            self.assertEqual(calls, [12, 12, 2])
            self.assertIn("26 帧", result)
            self.assertTrue(all(not path.parent.exists() for path in seen))
            self.assertTrue(video.is_file())

    async def test_failed_or_cancelled_vision_call_removes_batch(self):
        for failure in (RuntimeError("vision unavailable"), asyncio.CancelledError()):
            with tempfile.TemporaryDirectory(prefix="vw_analysis_") as folder:
                video = Path(folder) / "sample.mp4"
                video.write_bytes(b"video")
                seen = []

                async def extract(ffmpeg, path, out_dir, start, count):
                    frame = out_dir / "f_00000000.jpg"
                    frame.write_bytes(b"image")
                    seen.append(frame)
                    return [str(frame)]

                provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=failure))
                watcher = plugin.VideoWatcher(types.SimpleNamespace(get_using_provider=lambda umo: provider))
                with patch.object(plugin, "_extract_frames", side_effect=extract), patch.object(
                    plugin, "_probe_duration", AsyncMock(return_value=1)
                ), patch.object(plugin, "_ffmpeg_path", return_value="ffmpeg"):
                    if isinstance(failure, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await watcher._describe(str(video), "", None)
                    else:
                        self.assertIn("出错", await watcher._describe(str(video), "", None))
                self.assertFalse(seen[0].parent.parent.exists())

    async def test_unknown_duration_reads_until_end_and_summaries_are_merged(self):
        with tempfile.TemporaryDirectory(prefix="vw_analysis_") as folder:
            video = Path(folder) / "sample.mp4"
            video.write_bytes(b"video")
            starts = []

            async def extract(ffmpeg, path, out_dir, start, count):
                starts.append(start)
                paths = []
                for i in range(min(count, 13 - start)):
                    frame = out_dir / f"f_{start+i:08d}.jpg"
                    frame.write_bytes(b"image")
                    paths.append(str(frame))
                return paths

            provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=[
                types.SimpleNamespace(completion_text="第一段观察内容"),
                types.SimpleNamespace(completion_text="合并摘要"),
                types.SimpleNamespace(completion_text="第二段观察内容"),
                types.SimpleNamespace(completion_text="最终合并摘要"),
            ]))
            watcher = plugin.VideoWatcher(types.SimpleNamespace(get_using_provider=lambda umo: provider))
            with patch.object(plugin, "_extract_frames", side_effect=extract), patch.object(
                plugin, "_probe_duration", AsyncMock(return_value=0)
            ), patch.object(plugin, "_ffmpeg_path", return_value="ffmpeg"), patch.object(
                plugin, "SUMMARY_MERGE_CHARS", 1
            ):
                result = await watcher._describe(str(video), "", None)
            self.assertEqual(starts, [0, 12])
            self.assertIn("13 帧", result)
            self.assertIn("最终合并摘要", result)
            self.assertNotIn("image_urls", provider.text_chat.call_args_list[1].kwargs)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conversation = types.SimpleNamespace(
            cid="current", persona_id="dafeyu", history='[{"role":"user","content":"之前的聊天"}]'
        )
        self.manager = types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="current"),
            get_conversation=AsyncMock(return_value=self.conversation),
            new_conversation=AsyncMock(return_value="new"),
        )
        self.watcher = plugin.VideoWatcher(types.SimpleNamespace(conversation_manager=self.manager))

    async def test_command_handles_reply_card(self):
        event = make_event([comp.Reply(chain=[comp.Json(data={"url": SHORT})],
                                      message_str="", text="")], "/看视频")
        self.watcher._describe_source = AsyncMock(return_value="画面描述")
        results = [result async for result in self.watcher.watch_video_cmd(event)]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "llm_request")
        self.assertIs(results[0].conversation, self.conversation)
        self.assertIn("画面描述", results[0].prompt)
        self.assertFalse(hasattr(results[0], "contexts"))
        self.watcher._describe_source.assert_awaited_once_with(SHORT, "", event.unified_msg_origin)

    async def test_command_with_direct_link(self):
        self.watcher._describe_source = AsyncMock(return_value="描述")
        result = [r async for r in self.watcher.watch_video_cmd(make_event(text="/看视频 " + URL))]
        self.assertEqual(result[0].kind, "llm_request")
        self.assertIs(result[0].conversation, self.conversation)
        self.assertIn("描述", result[0].prompt)
        self.watcher._describe_source.assert_awaited_once_with(URL, "", "test:group:123")

    async def test_command_with_quoted_video(self):
        video = comp.Video(convert_to_file_path=AsyncMock(return_value="/tmp/clip.mp4"))
        self.watcher._describe = AsyncMock(return_value="描述")
        result = [r async for r in self.watcher.watch_video_cmd(make_event([
            comp.Reply(chain=[video], message_str="", text=""),
        ]))]
        self.assertEqual(result[0].kind, "llm_request")
        self.assertIs(result[0].conversation, self.conversation)
        self.watcher._describe.assert_awaited_once_with("/tmp/clip.mp4", "", "test:group:123")

    async def test_command_creates_conversation_when_none_exists(self):
        self.manager.get_curr_conversation_id.return_value = None
        self.watcher._describe_source = AsyncMock(return_value="观察资料")
        result = [r async for r in self.watcher.watch_video_cmd(make_event(text="/看视频 " + URL))]
        self.manager.new_conversation.assert_awaited_once_with("test:group:123")
        self.manager.get_conversation.assert_awaited_once_with("test:group:123", "new")
        self.assertIs(result[0].conversation, self.conversation)

    async def test_completed_command_does_not_inject_another_watch_tool_hint(self):
        event = make_event(text="/看视频 " + URL)
        self.watcher._describe_source = AsyncMock(return_value="已分析")
        result = [r async for r in self.watcher.watch_video_cmd(event)]
        prompt = result[0].prompt
        await self.watcher.expose_video_links(event, result[0])
        self.assertEqual(result[0].prompt, prompt)

    async def test_failed_download_is_also_reported_through_current_persona(self):
        self.watcher._describe_source = AsyncMock(return_value="下载失败（HTTP 567）")
        result = [r async for r in self.watcher.watch_video_cmd(make_event(text="/看视频 " + URL))]
        self.assertEqual(result[0].kind, "llm_request")
        self.assertIn("HTTP 567", result[0].prompt)
        self.assertIn("不能假装看过", result[0].prompt)

    async def test_unavailable_conversation_does_not_send_raw_observations(self):
        self.manager.get_conversation.side_effect = RuntimeError("conversation unavailable")
        self.watcher._describe_source = AsyncMock(return_value="原始分析报告")
        result = [r async for r in self.watcher.watch_video_cmd(make_event(text="/看视频 " + URL))]
        self.assertNotIn("原始分析报告", result[0])
        self.assertIn("未能接入当前对话", result[0])

    async def test_llm_can_see_card_url_without_downloading(self):
        event = make_event([comp.Json(data={"url": SHORT})])
        request = types.SimpleNamespace(prompt="帮我看看")
        await self.watcher.expose_video_links(event, request)
        await self.watcher.expose_video_links(event, request)
        self.assertEqual(request.prompt.count(SHORT), 1)
        self.assertTrue(request.prompt.startswith("帮我看看"))
        self.assertIn("describe_video", request.prompt)

    async def test_unrelated_message_is_unchanged(self):
        request = types.SimpleNamespace(prompt=None)
        await self.watcher.expose_video_links(make_event(text="你好"), request)
        self.assertIsNone(request.prompt)

    async def test_tool_finds_link_when_path_is_empty(self):
        self.watcher._describe_source = AsyncMock(return_value="猫在跑")
        event = make_event([comp.Share(url=SHORT)])
        result = await self.watcher.describe_video(event, question="有什么动物？")
        self.assertEqual(result, "猫在跑")
        self.watcher._describe_source.assert_awaited_once_with(SHORT, "有什么动物？", event.unified_msg_origin)

    async def test_local_file_still_uses_existing_pipeline(self):
        self.watcher._describe = AsyncMock(return_value="原有功能")
        result = await self.watcher._describe_source("/tmp/a.mp4", "问题", "session")
        self.assertEqual(result, "原有功能")
        self.watcher._describe.assert_awaited_once_with("/tmp/a.mp4", "问题", "session")

    async def test_download_success_cleans_up_after_description(self):
        seen = []

        async def fake_run(cmd, timeout, env=None):
            path = Path(cmd[-2]) / "source.mp4"
            path.write_bytes(b"test video")
            seen.append(path)
            return 0, json.dumps({"path": str(path)}), ""

        async def describe(path, question, umo):
            self.assertTrue(Path(path).is_file())
            self.assertEqual(question, "问题")
            return "描述"

        self.watcher._describe = describe
        with patch.object(plugin, "_run", side_effect=fake_run), patch.object(plugin, "_ffmpeg_path", return_value="ffmpeg"):
            result = await self.watcher._describe_source(SHORT, "问题", "session")
        self.assertEqual(result, "描述")
        self.assertFalse(seen[0].parent.exists())

    async def test_download_failure_and_timeout_clean_up(self):
        for code, error, expected in ((1, "403", "下载失败"), (-1, "timeout", "超时")):
            seen = []

            async def fake_run(cmd, timeout, env=None):
                seen.append(Path(cmd[-2]))
                (seen[0] / "source.mp4.part").write_bytes(b"partial")
                return code, "", error

            with patch.object(plugin, "_run", side_effect=fake_run), patch.object(plugin, "_ffmpeg_path", return_value="ffmpeg"):
                result = await self.watcher._describe_source(SHORT, "", None)
            self.assertIn(expected, result)
            self.assertFalse(seen[0].exists())

    async def test_cancelled_download_cleans_up(self):
        seen = []

        async def fake_run(cmd, timeout, env=None):
            seen.append(Path(cmd[-2]))
            raise asyncio.CancelledError

        with patch.object(plugin, "_run", side_effect=fake_run), patch.object(plugin, "_ffmpeg_path", return_value="ffmpeg"):
            with self.assertRaises(asyncio.CancelledError):
                await self.watcher._describe_source(SHORT, "", None)
        self.assertFalse(seen[0].exists())

    async def test_http_error_and_missing_dependency_have_distinct_messages(self):
        for error, expected in (
            ("ERROR: HTTP Error 567: Unknown Status", "HTTP 567"),
            ("缺少下载依赖，请在 AstrBot 环境安装 requirements.txt。", "缺少视频下载依赖"),
        ):
            with patch.object(plugin, "_run", AsyncMock(return_value=(1, "", error))), patch.object(
                plugin, "_ffmpeg_path", return_value="ffmpeg"
            ):
                result = await self.watcher._describe_source(URL, "", None)
            self.assertIn(expected, result)
            self.assertNotIn("需要登录", result)

    async def test_downloader_inherits_astrbot_dependency_directory(self):
        with tempfile.TemporaryDirectory(prefix="vw_dependencies_") as directory:
            module = Path(directory) / "yt_dlp.py"
            module.write_text(
                "from pathlib import Path\n"
                "class YoutubeDL:\n"
                "    def __init__(self, options):\n"
                "        self.path = options['outtmpl'].replace('%(ext)s', 'mp4')\n"
                "    def __enter__(self): return self\n"
                "    def __exit__(self, *args): pass\n"
                "    def extract_info(self, url, download):\n"
                "        Path(self.path).write_bytes(b'test video')\n"
                "        return {'id': 'test'}\n"
                "    def prepare_filename(self, info): return self.path\n",
                encoding="utf-8",
            )
            seen = []

            async def describe(path, question, umo):
                seen.append(Path(path))
                self.assertEqual(seen[0].read_bytes(), b"test video")
                return "描述"

            self.watcher._describe = describe
            with patch.object(plugin.sys, "path", [directory, *sys.path]), patch.object(
                plugin, "_ffmpeg_path", return_value="ffmpeg"
            ):
                result = await self.watcher._describe_source(URL, "", None)
            self.assertEqual(result, "描述")
            self.assertFalse(seen[0].parent.exists())

    async def test_subprocess_timeout_and_cancellation_reap_process(self):
        for failure in (asyncio.TimeoutError(), asyncio.CancelledError()):
            process = Mock(returncode=None)
            process.communicate = AsyncMock(side_effect=[failure, (b"", b"")])
            with patch.object(plugin.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                if isinstance(failure, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await plugin._run(["unused"])
                else:
                    self.assertEqual(await plugin._run(["unused"]), (-1, "", "timeout"))
            process.kill.assert_called_once()
            self.assertEqual(process.communicate.await_count, 2)


if __name__ == "__main__":
    unittest.main()
