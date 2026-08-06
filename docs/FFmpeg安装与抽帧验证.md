# FFmpeg 安装与抽帧验证

任务类（录屏）和垂域视觉评测需要 FFmpeg 读取视频并抽取关键帧。项目支持
系统 FFmpeg，也支持由 `imageio-ffmpeg` 的 Pip Wheel 提供二进制文件，不要求安装
Conda 或手动配置系统 `PATH`。

## 一、推荐安装

新拉取项目后，在 Python 3.10+ 虚拟环境中执行：

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` 会安装开发、Web 和视频依赖。如果已有项目环境，只补视频依赖：

```bash
python -m pip install -e ".[video]"
```

## 二、一键验证

在项目根目录执行：

```bash
python scripts/check_ffmpeg.py
```

脚本会强制使用 Pip Wheel 内置的 FFmpeg，依次验证：

1. FFmpeg 可执行文件存在且能启动；
2. 能生成一个临时 MP4 视频；
3. 不依赖 `ffprobe` 也能读取视频时长；
4. 能调用项目的 `extract_scene_keyframes()` 完成真实抽帧。

正常输出类似：

```text
[OK] Pip FFmpeg：.../site-packages/imageio_ffmpeg/binaries/ffmpeg-...
[OK] 视频时长：2.00 秒
[OK] 项目抽帧：4 帧
FFmpeg 环境与系统抽帧功能验证通过。
```

临时视频和帧会在验证结束后自动删除。

> `imageio-ffmpeg` 不一定把二进制加入系统 `PATH`，因此终端直接运行
> `ffmpeg -version` 失败，不代表项目内置的 FFmpeg 不可用；以上验证脚本才是本项目
> 的完整验收方式。

## 三、可选：使用系统 FFmpeg

如果系统已经安装 FFmpeg，项目会优先使用系统版本：

```bash
# macOS
brew install ffmpeg

# Windows PowerShell
winget install --id Gyan.FFmpeg -e
```

也可以显式指定可执行文件：

```bash
# macOS/Linux
export AUTO_EVAL_FFMPEG=/path/to/ffmpeg

# Windows PowerShell
$env:AUTO_EVAL_FFMPEG="C:\path\to\ffmpeg.exe"
```

`ffprobe` 只是可选的时长探测加速项；未安装时项目会自动使用 FFmpeg 读取时长。
如需显式指定，可设置 `AUTO_EVAL_FFPROBE`。

## 四、常见错误

### `No module named imageio_ffmpeg`

当前环境没有安装视频依赖，执行：

```bash
python -m pip install -e ".[video]"
```

如果机器上存在多个 Python，始终使用 `python -m pip`，确保安装和运行属于同一环境。

### `未找到 FFmpeg`

先运行一键验证。如果仍失败，检查是否安装到了当前 Python：

```bash
python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
```

### 验证通过但真实数据抽帧失败

此时 FFmpeg 环境已经正常，优先检查视频路径、文件权限、视频是否损坏，以及编码格式。
批量数据中的相对路径应以项目根目录为基准。
