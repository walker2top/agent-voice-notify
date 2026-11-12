# -*- coding: utf-8 -*-
"""跨平台 TTS 播报：用 edge 云端合成，带跨进程排队。

用法：
    python speak.py "要说的话"
    ... --edge zh-CN-XiaoyiNeural     换音色（默认 zh-CN-XiaoxiaoNeural 晓晓）
    ... --no-play                     只合成不播

文本控制在 30~60 字——这是"念一句概括"，不是朗读全文。

三个关键机制：
1. **音频文件用独立文件名**（pid + 毫秒）。早期用固定名，两条并发播放会互相覆盖
   同一个文件，听起来像"重复播报"（实测踩过）。
2. **播放前抢跨进程锁**（见 acquire_play_lock）。多个会话可能同时收尾，合成可以
   并行，但播放必须串行，否则两段声音叠在一起。
3. **播放器分平台**：Windows 走 PowerShell + System.Media.SoundPlayer（本机实测最稳，
   异步 wscript 中转会无声，已废弃）；其它平台用 ffplay 兜底，直接播 mp3 不转码。
"""
import asyncio
import os
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = sys.platform == "win32"

OUTDIR = os.path.join(HERE, "audio")
PLAY_PS1 = os.path.join(HERE, "play_wav.ps1")
FILE_PREFIX = "speak-"

DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"


def _find_binary(name):
    """先查 PATH，再看几个常见安装位置。找不到返回 None。"""
    found = shutil.which(name)
    if found:
        return found
    exe = name + ".exe" if IS_WINDOWS else name
    candidates = [
        # 本机旧位置（开发者机器上放这儿）
        r"C:\Users\walker\.dsh-wallpaper-engine\ffmpeg" + os.sep + exe,
        r"C:\ffmpeg\bin" + os.sep + exe,
        "/opt/homebrew/bin/" + name,
        "/usr/local/bin/" + name,
        "/usr/bin/" + name,
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


FF = _find_binary("ffmpeg")


def _cleanup_old(keep_seconds=1800):
    """清掉半小时前的临时音频，避免堆文件。"""
    now = time.time()
    try:
        for name in os.listdir(OUTDIR):
            if not name.startswith(FILE_PREFIX) and not name.startswith("reasonix-speak-"):
                continue
            p = os.path.join(OUTDIR, name)
            try:
                if now - os.path.getmtime(p) > keep_seconds:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def synth_edge(text: str, voice: str, mp3_path: str) -> bytes:
    import edge_tts

    async def run():
        await edge_tts.Communicate(text, voice).save(mp3_path)

    asyncio.run(run())
    return open(mp3_path, "rb").read()


def play(audio_path: str):
    """播放音频文件。

    Windows：PowerShell + System.Media.SoundPlayer.PlaySync()。
        本机实测最稳。曾经试过 wscript 中转异步播放，进程起来了、PlaySync 也返回了，
        但用户听不到（隐藏窗口下音频没进输出设备），那条路已删。
    其它平台：ffplay 兜底，直接播原文件，不用先转 wav。
    """
    if IS_WINDOWS:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", PLAY_PS1, audio_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")

    ffplay = _find_binary("ffplay")
    if not ffplay:
        print("[speak] 找不到 ffplay（ffmpeg 自带的播放器），无法播放", flush=True)
        return None
    return subprocess.run(
        [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", audio_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---------- 跨进程播放锁 ----------
# 多个会话可能同时收尾、同时要说话。合成可以并行（只是网络请求），
# 但播放必须串行，否则两段声音叠在一起谁也听不清。
LOCK_PATH = os.path.join(HERE, "speak.lock")
STALE_LOCK_SECONDS = 180   # 锁文件超过这个时间视为进程崩溃留下的残留


def _lock_is_stale():
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            parts = f.read().split()
        ts = int(parts[1]) if len(parts) > 1 else 0
        return (time.time() - ts) > STALE_LOCK_SECONDS
    except (OSError, ValueError, IndexError):
        # 读不出来（正好被创建/删除）——当成不算陈旧，让下一轮 poll 再看
        return False


def acquire_play_lock(timeout=150.0, poll=0.25):
    """抢播放锁，抢不到就排队等。返回 True=拿到，False=等超时。"""
    deadline = time.time() + timeout
    waited = False
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ("%d %d" % (os.getpid(), int(time.time()))).encode())
            os.close(fd)
            if waited:
                print("[speak] 轮到本会话播放", flush=True)
            return True
        except FileExistsError:
            if _lock_is_stale():
                try:
                    os.remove(LOCK_PATH)
                except OSError:
                    pass
                continue
            if not waited:
                print("[speak] 别的会话正在说话，排队等待…", flush=True)
                waited = True
            if time.time() > deadline:
                return False
            time.sleep(poll)
        except OSError:
            return True   # 锁机制本身出错时不要卡死播报，直接放行


def release_play_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def main(argv):
    args, flags, edge = [], set(), None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--edge":
            edge = argv[i + 1] if i + 1 < len(argv) else DEFAULT_EDGE_VOICE
            i += 2
            continue
        if a.startswith("--edge="):
            edge = a.split("=", 1)[1]
        elif a.startswith("--"):
            flags.add(a)
        else:
            args.append(a)
        i += 1

    if not args:
        print('用法: python speak.py "文本" [--edge 音色] [--no-play]')
        return 2
    text = args[0]

    os.makedirs(OUTDIR, exist_ok=True)
    _cleanup_old()
    token = "%d-%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    mp3_path = os.path.join(OUTDIR, FILE_PREFIX + "%s.mp3" % token)
    wav_path = os.path.join(OUTDIR, FILE_PREFIX + "%s.wav" % token)

    try:
        voice = edge or DEFAULT_EDGE_VOICE
        data = synth_edge(text, voice, mp3_path)
        tag = "edge:" + voice
    except Exception as exc:
        print(f"[speak] 合成失败：{exc}", flush=True)
        return 1

    print(f"[speak] ok {len(data)} bytes {tag} file={os.path.basename(mp3_path)}", flush=True)

    if "--no-play" in flags:
        print(f"[speak] 未播放，文件：{mp3_path}", flush=True)
        return 0

    got_lock = acquire_play_lock()
    if not got_lock:
        print("[speak] 排队超时（队列太挤），本条放弃播放", flush=True)
        return 1

    try:
        if IS_WINDOWS:
            # Windows 的 SoundPlayer 只认 wav，先转一道
            if not FF:
                print("[speak] 找不到 ffmpeg，无法转 wav（Windows 播放需要）", flush=True)
                return 1
            subprocess.run([FF, "-y", "-loglevel", "error", "-i", mp3_path,
                            "-ar", "44100", "-ac", "1", wav_path], check=True)
            target = wav_path
        else:
            # 其它平台 ffplay 直接播 mp3，不转码
            target = mp3_path
        res = play(target)
        if res is None:
            print("[speak] 播放器缺失，本条未发声", flush=True)
        else:
            print(f"[speak] 播放退出码 {res.returncode}", flush=True)
    finally:
        release_play_lock()
        # 播完就删，避免堆文件
        for p in (mp3_path, wav_path):
            try:
                os.remove(p)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
