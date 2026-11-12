# agent-voice-notify

让 AI coding agent 干完活**开口说一句** —— 挑重点说，不是把回复念一遍。

你在另一个窗口忙别的事，它跑完几十步操作，你不用回头翻日志：

> "缓存清掉了，服务重启正常，你可以再试一次。"

---

## 这是什么

一个极小的 TTS 播报器，用 Microsoft Edge 的免费在线合成（`edge-tts`），**零 API key、零本地模型、零下载**。

它解决的不是"文字转语音"——那个有无数库——而是**"该说什么"**：

| 常见做法 | 问题 |
|---|---|
| 把 agent 的回复整个念出来 | 又长又书面，包含路径、命令、报错码，没人想听 |
| 只播一声提示音 | 你还得自己回去看屏幕上写了什么 |
| 本项目 | LLM 写一句 30~60 字的口语汇报，只讲结论 |

具体规则见 [`docs/broadcast-style.md`](docs/broadcast-style.md)。

## 两个工程细节（这两个才是别人没做的）

### 1. 多会话不会抢话

同时开三个 agent 会话，它们可能在同一刻收尾。合成并行跑（只是网络请求），但**播放必须串行**，否则三段声音叠在一起谁也听不清。

`speak.py` 用 `O_CREAT | O_EXCL` 原子创建锁文件实现跨进程互斥，抢不到就排队等，播完释放。锁超过 180 秒视为进程崩溃残留，自动清理。

### 2. 平台差异被隔离在播放这一步

| 平台 | 播放方式 | 原因 |
|---|---|---|
| Windows | PowerShell + `System.Media.SoundPlayer.PlaySync()` | 实测最稳。曾试过 wscript 中转异步播放，进程起来了、`PlaySync` 也返回了，但用户听不到 —— 隐藏窗口下音频没进输出设备 |
| macOS / Linux | `ffplay -nodisp -autoexit` | 兜底，直接播 mp3 不转码 |

Windows 上必须先转 wav（`SoundPlayer` 只认 wav），其它平台直接播 mp3。

## 快速开始

需要 **Python 3.9+** 和 **ffmpeg**（在 PATH 里即可）。

```bash
git clone https://github.com/walker2top/agent-voice-notify.git
cd agent-voice-notify

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

python speak.py "缓存清掉了，服务重启正常，你可以再试一次。"
```

## 用法

```
python speak.py "要说的话"
  --edge zh-CN-XiaoyiNeural   换音色（默认 zh-CN-XiaoxiaoNeural 晓晓）
  --no-play                   只合成不播，输出文件路径
```

### 可用音色

任意 Edge TTS 音色都行，中文常用：

| 音色 ID | 特点 |
|---|---|
| `zh-CN-XiaoxiaoNeural` | 晓晓，平缓、吐字清楚，读技术内容不刺耳（默认） |
| `zh-CN-XiaoyiNeural` | 晓伊，更活泼 |
| `zh-CN-YunxiNeural` | 云希，男声 |
| `zh-CN-YunjianNeural` | 云健，男声，低沉 |

英文用 `en-US-AriaNeural` 等，完整列表见 `edge-tts --list-voices`。

## 接入 AI agent

### Claude Code —— Stop hook（推荐，不依赖模型自觉）

写进 `.claude/settings.json`：

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python /path/to/agent-voice-notify/notify.py",
        "timeout": 15
      }]
    }]
  }
}
```

`Stop` 是 Claude Code 的生命周期事件：每轮结束时它自己调用这个脚本，模型没有"忘记播报"的可能。

> **为什么要用 hook**：如果让模型自己决定"要不要播报"，长回复 + 多工具调用的轮次里它会漏 —— 写完整篇解释后注意力已经耗尽，收尾动作直接被跳过。这是实测踩过的（漏报三次）。机制强制比自律可靠。

### Codex

```toml
# ~/.codex/config.toml
notify = ["python", "/path/to/agent-voice-notify/notify.py"]
```

### 手动 / 其它 agent

任何能执行命令的地方，调一句就行：

```bash
python speak.py "文档写完了，四篇都在产出目录里。"
```

## 播报规则速查

完整版在 [`docs/broadcast-style.md`](docs/broadcast-style.md)，核心是四句话：

1. **30~60 字，1~3 句** —— 压到 20 字会退化成公告，没有重点
2. **先结论，再待办，最后风险** —— 不是按时间顺序复述过程
3. **不出现文件名、路径、命令、参数、报错码**
4. **不说"我执行了 / 我修改了"** —— 直接讲结果

对照：

| ✅ 这样念 | ❌ 不要这样 |
|---|---|
| 桌宠嗓子换好了，两种音色都能切，你可以试试。 | 我把 config.json 的 speaker 字段改成 voices/wx/wx-C-clean-up4.mp3，pitchShift 设成 1.06，然后重启了 9880 服务… |
| 麦克风调好了，现在说话能识别了。 | 日志里 peak 从 0.005 回到 0.223，超过阈值 0.0025，所以静音过滤不再触发… |
| 这个我搞不定，得你定用哪个音色。 | 关于音色选择，经我分析有若干因素需要考虑，首先是… |

## 工作原理

```
文本 ──edge-tts──→ mp3 ──[Windows: ffmpeg 转 wav]──→ 抢锁 ──→ 播放 ──→ 释放锁
```

- 临时文件用 `pid + 毫秒` 命名（早期用固定名，两条并发会互相覆盖，听起来像"重复播报"）
- 半小时前的临时文件自动清理
- 合成失败直接返回非零，不阻塞调用方

## 环境变量

| 变量 | 作用 |
|---|---|
| `VOICE_NOTIFY_DIR` | 覆盖临时音频目录（默认 `./audio`） |

## 限制

- **需要联网**：Edge TTS 是微软的在线服务（免费、无需 key，但要能访问）
- **不处理音频输入**：这个项目只管"说"，"听"是另一件事
- **没有自动摘要**：播报稿需要由 agent（或你）自己写好传进来。本项目不做 LLM 调用

## License

MIT
