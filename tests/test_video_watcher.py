"""离线回归测试；AstrBot 接口用最小替身，避免依赖正在运行的机器人。"""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
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
    return types.SimpleNamespace(
        get_messages=lambda: chain,
        get_message_str=lambda: text,
        unified_msg_origin="test:group:123",
        plain_result=lambda value: value,
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


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.watcher = plugin.VideoWatcher(types.SimpleNamespace())

    async def test_command_handles_reply_card(self):
        event = make_event([comp.Reply(chain=[comp.Json(data={"url": SHORT})],
                                      message_str="", text="")], "/看视频")
        self.watcher._describe_source = AsyncMock(return_value="画面描述")
        results = [result async for result in self.watcher.watch_video_cmd(event)]
        self.assertEqual(results, ["画面描述"])
        self.watcher._describe_source.assert_awaited_once_with(SHORT, "", event.unified_msg_origin)

    async def test_command_with_direct_link(self):
        self.watcher._describe_source = AsyncMock(return_value="描述")
        result = [r async for r in self.watcher.watch_video_cmd(make_event(text="/看视频 " + URL))]
        self.assertEqual(result, ["描述"])
        self.watcher._describe_source.assert_awaited_once_with(URL, "", "test:group:123")

    async def test_command_with_quoted_video(self):
        video = comp.Video(convert_to_file_path=AsyncMock(return_value="/tmp/clip.mp4"))
        self.watcher._describe = AsyncMock(return_value="描述")
        result = [r async for r in self.watcher.watch_video_cmd(make_event([
            comp.Reply(chain=[video], message_str="", text=""),
        ]))]
        self.assertEqual(result, ["描述"])
        self.watcher._describe.assert_awaited_once_with("/tmp/clip.mp4", "", "test:group:123")

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
