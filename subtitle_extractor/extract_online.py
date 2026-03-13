#!/usr/bin/env python3
"""
在线视频字幕提取工具
基于 yt-dlp 实现在线视频 URL 解析，优先下载官方字幕，若无官方字幕则下载音频流并使用 faster-whisper 进行转录
"""

import os
import sys
import argparse
import tempfile
from pathlib import Path
import yt_dlp
from extract_subs import transcribe_audio, segments_to_markdown


def download_subtitles_from_url(url, output_dir, languages=None):
    """
    从在线视频 URL 下载官方字幕

    Args:
        url (str): 视频 URL
        output_dir (Path): 输出目录
        languages (list): 优先下载的语言列表，默认为 ['zh', 'en']

    Returns:
        tuple: (bool, str) - (是否成功下载字幕, 字幕文件路径或错误信息)
    """
    if languages is None:
        languages = ['zh', 'en']

    # yt-dlp 配置
    ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'subtitleslangs': languages,
        'subtitlesformat': 'srt/best',
        'outtmpl': str(output_dir / '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # 检查是否下载了字幕
            if 'requested_subtitles' in info and info['requested_subtitles']:
                for lang, sub_info in info['requested_subtitles'].items():
                    if 'filepath' in sub_info:
                        subtitle_file = sub_info['filepath']
                        if os.path.exists(subtitle_file):
                            return True, subtitle_file

                return False, "No subtitles found for the specified languages"
            else:
                return False, "No subtitles available for this video"

    except Exception as e:
        return False, f"Error downloading subtitles: {e}"


def convert_srt_to_markdown(srt_path):
    """
    将 SRT 字幕文件转换为 Markdown 格式

    Args:
        srt_path (str): SRT 文件路径

    Returns:
        str: Markdown 格式的字幕内容
    """
    try:
        with open(srt_path, 'r', encoding='utf-8') as f:
            content = f.read()

        markdown_content = "# 视频字幕\n\n"

        # 解析 SRT 格式
        blocks = content.strip().split('\n\n')
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                # 第一行是序号，跳过
                # 第二行是时间戳
                timestamp_line = lines[1]
                if ' --> ' in timestamp_line:
                    start_time, end_time = timestamp_line.split(' --> ')
                    # 转换时间格式为 HH:MM:SS.mmm
                    start_formatted = convert_srt_timestamp(start_time)
                    end_formatted = convert_srt_timestamp(end_time)

                    # 剩余行是文本
                    text_lines = lines[2:]
                    text = ' '.join(text_lines).strip()

                    if text:  # 只有非空文本才添加
                        markdown_content += f"## [{start_formatted} - {end_formatted}]\n\n{text}\n\n"

        return markdown_content

    except Exception as e:
        print(f"Error converting SRT to Markdown: {e}")
        return None


def convert_srt_timestamp(srt_time):
    """
    将 SRT 时间戳格式 (HH:MM:SS,mmm) 转换为 HH:MM:SS.mmm 格式

    Args:
        srt_time (str): SRT 时间戳

    Returns:
        str: 格式化的时间戳
    """
    # SRT 格式: HH:MM:SS,mmm
    # 目标格式: HH:MM:SS.mmm
    if ',' in srt_time:
        return srt_time.replace(',', '.')
    return srt_time


def download_audio_from_url(url, output_dir):
    """
    从在线视频 URL 下载最优音频流

    Args:
        url (str): 视频 URL
        output_dir (Path): 输出目录

    Returns:
        tuple: (bool, str) - (是否成功下载音频, 音频文件路径或错误信息)
    """
    # yt-dlp 配置 - 只下载最佳音频
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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # 获取下载的音频文件路径
            audio_file = ydl.prepare_filename(info)
            # yt-dlp 会自动添加 .wav 扩展名
            audio_file = os.path.splitext(audio_file)[0] + '.wav'

            if os.path.exists(audio_file):
                return True, audio_file
            else:
                return False, "Audio file not found after download"

    except Exception as e:
        return False, f"Error downloading audio: {e}"


def process_online_video(url, output_dir, model_size="base", device="cpu", languages=None):
    """
    处理在线视频 URL

    Args:
        url (str): 视频 URL
        output_dir (Path): 输出目录
        model_size (str): Whisper 模型大小
        device (str): 运行设备
        languages (list): 优先下载的字幕语言
    """
    print(f"Processing online video: {url}")

    # 确保输出目录存在
    output_dir.mkdir(parents=True, exist_ok=True)

    if languages is None:
        languages = ['zh', 'en']

    # 首先尝试下载官方字幕
    print("Checking for official subtitles...")
    success, result = download_subtitles_from_url(url, output_dir, languages)

    if success:
        print(f"Official subtitles downloaded: {result}")
        # 转换为 Markdown 格式
        markdown_content = convert_srt_to_markdown(result)
        if markdown_content:
            # 生成输出文件名
            md_filename = Path(result).stem + '.md'
            md_path = output_dir / md_filename

            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)

            # 清理临时 SRT 文件
            os.remove(result)

            print(f"Successfully processed: {url} -> {md_path}")
            return True
        else:
            print("Failed to convert subtitles to Markdown format")
            return False
    else:
        print(f"No official subtitles found: {result}")
        print("Downloading audio stream for transcription...")

        # 下载音频流
        success, result = download_audio_from_url(url, output_dir)
        if not success:
            print(f"Failed to download audio: {result}")
            return False

        audio_path = result
        print(f"Audio downloaded: {audio_path}")

        # 使用 faster-whisper 进行转录
        segments = transcribe_audio(audio_path, model_size, device)

        if not segments:
            print("Failed to transcribe audio")
            # 清理音频文件
            if os.path.exists(audio_path):
                os.remove(audio_path)
            return False

        # 生成 Markdown
        markdown_content = segments_to_markdown(segments)

        # 生成输出文件名
        md_filename = Path(audio_path).stem + '.md'
        md_path = output_dir / md_filename

        # 写入 Markdown 文件
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(markdown_content)

        # 清理音频文件
        if os.path.exists(audio_path):
            os.remove(audio_path)

        print(f"Successfully processed: {url} -> {md_path}")
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