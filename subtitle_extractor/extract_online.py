#!/usr/bin/env python3
# 在线视频字幕提取工具：优先下载官方字幕转 Markdown，缺失时下载音频回退 whisper 转写。
# 调用方：命令行直接运行（python extract_online.py <url> [-o 输出目录]）。
# 关键依赖：yt_dlp（下载字幕/音频）、extract_subs 的 transcribe_audio 与 segments_to_markdown。

import os
import sys
import argparse
import re
import shutil
import tempfile
from pathlib import Path


def load_yt_dlp():
    # 运行依赖懒加载，避免 --help 被未安装下载依赖提前拦截。
    import yt_dlp

    return yt_dlp


def load_transcription_helpers():
    # 转写依赖较重，只在官方字幕缺失、确实需要 whisper 回退时导入。
    from extract_subs import transcribe_audio, segments_to_markdown

    return transcribe_audio, segments_to_markdown


def download_subtitles_from_url(url, output_dir, languages=None):
    # 下载官方字幕。
    if languages is None:
        languages = ['zh', 'en']

    ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'subtitleslangs': languages,
        'subtitlesformat': 'srt/vtt/best',
        'outtmpl': str(output_dir / '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }

    try:
        yt_dlp = load_yt_dlp()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # 检查字幕下载结果。
            if 'requested_subtitles' in info and info['requested_subtitles']:
                requested = info['requested_subtitles']
                candidates = []
                seen = set()
                ordered_languages = list(languages) + [
                    lang for lang in requested if lang not in languages
                ]
                for lang in ordered_languages:
                    sub_info = requested.get(lang) or {}
                    subtitle_file = sub_info.get('filepath')
                    # A reported download is still a candidate if it becomes
                    # unreadable; conversion failure must not start Whisper.
                    if not subtitle_file:
                        continue
                    identity = os.path.normcase(os.path.abspath(subtitle_file))
                    if identity not in seen:
                        seen.add(identity)
                        candidates.append(subtitle_file)
                if candidates:
                    return True, candidates
                return False, "未找到指定语言的字幕"
            else:
                return False, "该视频没有可用字幕"

    except Exception as e:
        return False, f"下载字幕失败： {e}"


def convert_srt_to_markdown(srt_path):
    # 支持 SRT 和 WebVTT；未知格式或无有效正文不得作为成功结果。
    try:
        if Path(srt_path).suffix.lower() not in {".srt", ".vtt"}:
            return None
        with open(srt_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        paragraphs = []
        stamp = r"(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}"
        timing = re.compile(rf"^({stamp})\s+-->\s+({stamp})(?:\s+.*)?$")
        blocks = re.split(r'\n\s*\n', content.strip())
        for block in blocks:
            lines = block.strip().split('\n')
            if not block.strip():
                continue
            if Path(srt_path).suffix.lower() == ".vtt" and (
                re.match(r"^(?:WEBVTT|NOTE)(?:\s|$)", lines[0]) or lines[0] in {"STYLE", "REGION"}
            ):
                continue
            # WebVTT 的 cue id 可省略；SRT 的序号在时间行之前。
            time_index = 0 if "-->" in lines[0] else 1
            if time_index >= len(lines):
                return None
            match = timing.fullmatch(lines[time_index].strip())
            if not match:
                return None
            text = ' '.join(lines[time_index + 1:]).strip()
            if text:
                start, end = (convert_srt_timestamp(value) for value in match.groups())
                paragraphs.append(f"## [{start} - {end}]\n\n{text}\n\n")
        return "# 视频字幕\n\n" + ''.join(paragraphs) if paragraphs else None

    except Exception as e:
        print(f"将 SRT 转换为 Markdown 时出错： {e}")
        return None


def convert_srt_timestamp(srt_time):
    # 将 SRT 时间戳转为统一格式。
    stamp = srt_time.replace(',', '.')
    return f"00:{stamp}" if stamp.count(':') == 1 else stamp


def download_audio_from_url(url, output_dir):
    # 下载最优音频流。
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
            'preferredquality': '192',
        }],
        'outtmpl': str(output_dir / '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }

    try:
        yt_dlp = load_yt_dlp()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            audio_file = ydl.prepare_filename(info)
            audio_file = os.path.splitext(audio_file)[0] + '.wav'

            if os.path.exists(audio_file):
                return True, audio_file
            else:
                return False, "下载后未找到音频文件"

    except Exception as e:
        return False, f"下载音频失败： {e}"


def process_online_video(url, output_dir, model_size="base", device="cpu", languages=None):
    # 处理在线视频。
    print(f"正在处理在线视频：{url}")

    # 确保输出目录存在。
    output_dir.mkdir(parents=True, exist_ok=True)

    if languages is None:
        languages = ['zh', 'en']

    # 先尝试官方字幕。
    print("正在检查官方字幕...")
    subtitle_dir = Path(tempfile.mkdtemp(prefix=".subtitle-files-", dir=output_dir))
    success, result = download_subtitles_from_url(url, subtitle_dir, languages)

    if success:
        for subtitle_file in result:
            print(f"官方字幕已下载：{subtitle_file}")
            markdown_content = convert_srt_to_markdown(subtitle_file)
            if not markdown_content:
                continue
            md_filename = Path(subtitle_file).stem + '.md'
            md_path = output_dir / md_filename
            try:
                with open(md_path, 'x', encoding='utf-8') as f:
                    f.write(markdown_content)
            except OSError as e:
                print(f"写入 Markdown 失败，原始字幕文件已保留：{e}")
                return False
            shutil.rmtree(subtitle_dir)
            print(f"处理成功：{url} -> {md_path}")
            return True
        if not any(subtitle_dir.iterdir()):
            subtitle_dir.rmdir()
        print(f"字幕转换为 Markdown 失败，原始字幕文件已保留：{result}")
        return False
    else:
        if not any(subtitle_dir.iterdir()):
            subtitle_dir.rmdir()
        print(f"未找到官方字幕：{result}")
        print("正在下载音频流以进行转写...")

        # 下载及转写失败也只清理本次唯一目录，不覆盖/删除输出目录既有音频。
        with tempfile.TemporaryDirectory(prefix=".subtitle-audio-", dir=output_dir) as temporary:
            try:
                success, result = download_audio_from_url(url, Path(temporary))
                if not success:
                    print(f"音频下载失败：{result}")
                    return False
                audio_path = Path(result)
                if not audio_path.resolve().is_relative_to(Path(temporary).resolve()):
                    print("音频下载结果不在本次临时目录，已停止处理")
                    return False
                print(f"音频已下载：{audio_path}")
                transcribe_audio, segments_to_markdown = load_transcription_helpers()
                segments = transcribe_audio(str(audio_path), model_size, device)
                if not segments:
                    print("音频转写失败")
                    return False
                markdown_content = segments_to_markdown(segments)
                md_path = output_dir / (audio_path.stem + '.md')
                with open(md_path, 'x', encoding='utf-8') as f:
                    f.write(markdown_content)
            except Exception as e:
                print(f"音频处理失败：{e}")
                return False
        print(f"处理成功：{url} -> {md_path}")
        return True


def main():
    parser = argparse.ArgumentParser(description="在线视频字幕提取工具")
    parser.add_argument("url", help="在线视频 URL")
    parser.add_argument("-o", "--output", default="output", help="输出目录 (默认: output)")
    parser.add_argument("-m", "--model", default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper 模型大小 (默认: base)")
    parser.add_argument("-d", "--device", default="cpu", choices=["cpu", "cuda"],
                        help="运行设备 (默认: cpu)")
    parser.add_argument("-l", "--languages", nargs='+', default=['zh', 'en'],
                        help="优先下载的字幕语言 (默认: zh en)")

    args = parser.parse_args()

    output_path = Path(args.output)

    success = process_online_video(
        args.url,
        output_path,
        args.model,
        args.device,
        args.languages
    )

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()

