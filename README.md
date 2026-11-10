# astrbot_plugin_video_watcher

让 AstrBot 里的机器人能「看」视频。

## 它解决什么问题

AstrBot 收到视频时，只有在上下文里追加一行路径：

```python
# astr_main_agent.py
text = f"[Video Attachment: name {video_name}, path {video_path}]"
```

模型本身没有视频解码能力，所以它只知道「有个文件在这」，看不到画面。

这个插件补上中间那一步：**用 ffmpeg 抽帧 → 把帧图交给多模态模型 → 返回文字描述**。

## 前提

- AstrBot `>= 4.24.0`
- 当前对话模型**支持图片输入**（不支持会报错，例如纯文本模型）
- 依赖 `imageio-ffmpeg`（会自带静态 ffmpeg，不需要系统预装）

## 安装

### 方式一：AstrBot 插件市场

搜索「视频观察员」，或直接填仓库地址。

### 方式二：手动

```bash
cd AstrBot/data/plugins
git clone https://github.com/wuyinhh/astrbot_plugin_video_watcher.git
cd astrbot_plugin_video_watcher
pip install -r requirements.txt
```

装完重启 AstrBot，插件页应该能看到「视频观察员」。

## 用法

三种触发方式：

| 方式 | 说明 |
|---|---|
| `/看视频` + 一条视频 | 最直接 |
| 引用一条视频消息，再发 `/看视频` | 不用重复上传 |
| 什么都不做 | 模型在需要时自行调用 `describe_video` 工具 |

正常对话示例：

> 你：（发一个视频）/看视频
> 她：画面里是一个厨房，有个人在切菜，台面上放着两个碗……

## 配置

参数写在 `main.py` 顶部，改完重启生效：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_FRAMES` | 6 | 抽几帧。帧数越多越贵，6 帧通常够描述 |
| `FRAME_WIDTH` | 640 | 帧图宽度。越大越清晰，token 也越多 |
| `MAX_VIDEO_MB` | 500 | 超过这个大小直接拒绝 |
| `FFMPEG_TIMEOUT` | 180 | 单次 ffmpeg 超时（秒） |

## 局限

**它看的是「几张截图」，不是视频。**

- 能看出：场景、人物、物体、正在做的事、画面上的文字
- 看不出：连贯动作、镜头运动、说了什么（声音）、剧情因果

想要声音，得另外接 ASR（例如 faster-whisper）把音轨转成字幕，再把文字一起喂给模型。

## 实现细节

1. `ffmpeg -i` 解析时长（解析 stderr 的 `Duration:`）
2. 按 `duration * (i + 0.5) / MAX_FRAMES` 均匀取 6 个时间点，各自 seek 抽 1 帧
3. 缩放到 `FRAME_WIDTH`，JPEG 质量 3
4. 帧图路径直接传给 `provider.text_chat(image_urls=[...])`，由 AstrBot 内部转成 data URL
5. 临时目录用完即删

拿不到时长时（流式/损坏文件）退化为 `fps=1/5` 固定间隔抽取，最多 6 帧。

## License

MIT
