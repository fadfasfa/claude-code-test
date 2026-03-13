#!/usr/bin/env python3
"""
视频字幕批量提取工具
基于 faster-whisper 实现无字幕视频自动分离音频并转录为 Markdown 格式
"""

import os
import sys
import argparse
from pathlib import Path
from moviepy import VideoFileClip
from faster_whisper import WhisperModel


def extract_audio_from_video(video_path, audio_path):
    """
    从视频文件中提取音频

    Args:
        video_path (str): 视频文件路径
        audio_path (str): 输出音频文件路径
    """
    try:
        video = VideoFileClip(video_path)
        video.audio.write_audiofile(audio_path, verbose=False, logger=None)
        video.close()
        return True
    except Exception as e:
        print(f"Error extracting audio from {video_path}: {e}")
        return False


def transcribe_audio(audio_path, model_size="base", device="cpu"):
    """
    使用 faster-whisper 转录音频

    Args:
        audio_path (str): 音频文件路径
        model_size (str): 模型大小 (tiny, base, small, medium, large)
        device (str): 运行设备 (cpu, cuda)

    Returns:
        list: 包含时间戳和文本的段落列表
    """
    # 根据设备类型选择合适的计算类型
    if device == "cuda":
        compute_type = "float16"
    else:  # device == "cpu"
        compute_type = "int8"  # CPU 推荐使用 int8，更节省内存且兼容性好

    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments, _ = model.transcribe(audio_path, word_timestamps=True)
        return list(segments)
    except Exception as e:
        print(f"Error transcribing audio {audio_path}: {e}")
        return []


def segments_to_markdown(segments):
    """
    将转录段落转换为 Markdown 格式

    Args:
        segments (list): 转录段落列表

    Returns:
        str: Markdown 格式的文本
    """
    markdown_content = "# 视频字幕\n\n"

    for segment in segments:
        start_time = format_timestamp(segment.start)
        end_time = format_timestamp(segment.end)
        text = segment.text.strip()

        markdown_content += f"## [{start_time} - {end_time}]\n\n{text}\n\n"

    return markdown_content


def format_timestamp(seconds):
    """
    将秒数格式化为 HH:MM:SS.mmm 格式

    Args:
        seconds (float): 秒数

    Returns:
        str: 格式化的时间戳
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)

    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def process_video_file(video_path, output_dir, model_size="base", device="cpu"):
    """
    处理单个视频文件

    Args:
        video_path (Path): 视频文件路径
        output_dir (Path): 输出目录
        model_size (str): 模型大小
        device (str): 运行设备
    """
    print(f"Processing: {video_path}")

    # 创建临时音频文件
    audio_path = output_dir / f"{video_path.stem}_temp.wav"
    md_path = output_dir / f"{video_path.stem}.md"

    # 提取音频
    if not extract_audio_from_video(str(video_path), str(audio_path)):
        print(f"Failed to extract audio from {video_path}")
        return

    # 转录音频
    segments = transcribe_audio(str(audio_path), model_size, device)

    if not segments:
        print(f"Failed to transcribe audio from {video_path}")
        # 清理临时音频文件
        if audio_path.exists():
            audio_path.unlink()
        return

    # 生成 Markdown
    markdown_content = segments_to_markdown(segments)

    # 写入 Markdown 文件
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    # 清理临时音频文件
    if audio_path.exists():
        audio_path.unlink()

    print(f"Successfully processed: {video_path} -> {md_path}")


def process_directory(input_dir, output_dir, model_size="base", device="cpu", extensions=None):
    """
    处理目录中的所有视频文件

    Args:
        input_dir (Path): 输入目录
        output_dir (Path): 输出目录
        model_size (str): 模型大小
        device (str): 运行设备
        extensions (list): 支持的视频文件扩展名
    """
    if extensions is None:
        extensions = ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm']

    # 确保输出目录存在
    output_dir.mkdir(parents=True, exist_ok=True)

    # 获取所有视频文件
    video_files = []
    for ext in extensions:
        video_files.extend(input_dir.rglob(f"*{ext}"))
        video_files.extend(input_dir.rglob(f"*{ext.upper()}"))

    if not video_files:
        print(f"No video files found in {input_dir}")
        return

    print(f"Found {len(video_files)} video files to process")

    # 处理每个视频文件
    for video_file in video_files:
        process_video_file(video_file, output_dir, model_size, device)


def main():
    parser = argparse.ArgumentParser(description="视频字幕批量提取工具")
    parser.add_argument("input", help="输入视频文件或目录")
    parser.add_argument("-o", "--output", default="output", help="输出目录 (默认: output)")
    parser.add_argument("-m", "--model", default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper 模型大小 (默认: base)")
    parser.add_argument("-d", "--device", default="cpu", choices=["cpu", "cuda"],
                        help="运行设备 (默认: cpu)")

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Error: Input path {input_path} does not exist")
        sys.exit(1)

    if input_path.is_file():
        # 处理单个文件
        output_path.mkdir(parents=True, exist_ok=True)
        process_video_file(input_path, output_path, args.model, args.device)
    else:
        # 处理目录
        process_directory(input_path, output_path, args.model, args.device)


if __name__ == "__main__":
    main()