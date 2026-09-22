#!/usr/bin/env python3
# 视频字幕批量提取工具。
# 提取音频并转录，再整理为 Markdown。

import os
import sys
import argparse
import tempfile
from pathlib import Path
from moviepy import VideoFileClip
from faster_whisper import WhisperModel


def extract_audio_from_video(video_path, audio_path):
    # 提取视频音频。
    video = None
    try:
        video = VideoFileClip(video_path)
        video.audio.write_audiofile(audio_path, logger=None)
        return True
    except Exception as e:
        print(f"从 {video_path} 提取音频失败：{e}")
        return False
    finally:
        if video is not None:
            video.close()


def transcribe_audio(audio_path, model_size="base", device="cpu"):
    # 使用转录模型处理音频。
    if device == "cuda":
        compute_type = "float16"
    else:  # device == "cpu"
        compute_type = "int8"

    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments, _ = model.transcribe(audio_path, word_timestamps=True)
        return list(segments)
    except Exception as e:
        print(f"音频转写失败：{audio_path}，原因：{e}")
        return []


def segments_to_markdown(segments):
    # 将转录段落转换为 Markdown。
    markdown_content = "# 视频字幕\n\n"

    for segment in segments:
        start_time = format_timestamp(segment.start)
        end_time = format_timestamp(segment.end)
        text = segment.text.strip()

        markdown_content += f"## [{start_time} - {end_time}]\n\n{text}\n\n"

    return markdown_content


def format_timestamp(seconds):
    # 格式化为 HH:MM:SS.mmm。
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)

    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def process_video_file(video_path, output_dir, model_size="base", device="cpu", *, output_name=None):
    # 处理单个视频文件。
    print(f"正在处理：{video_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{output_name or video_path.stem}.md"
    # 唯一临时目录只承载本次生成的音频；绝不覆盖或清理用户同名 WAV。
    with tempfile.TemporaryDirectory(prefix=".subtitle-audio-", dir=output_dir) as temporary:
        audio_path = Path(temporary) / "audio.wav"
        try:
            if not extract_audio_from_video(str(video_path), str(audio_path)):
                print(f"从 {video_path} 提取音频失败")
                return False
            segments = transcribe_audio(str(audio_path), model_size, device)
            if not segments:
                print(f"音频转写失败：{video_path}")
                return False
            markdown_content = segments_to_markdown(segments)
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
        except Exception as e:
            print(f"处理失败：{video_path}，原因：{e}")
            return False
    print(f"处理成功：{video_path} -> {md_path}")
    return True


def process_directory(input_dir, output_dir, model_size="base", device="cpu", extensions=None):
    # 处理目录中的所有视频文件。
    if extensions is None:
        extensions = ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm']

    # 确保输出目录存在。
    output_dir.mkdir(parents=True, exist_ok=True)

    # 收集视频文件。
    allowed_extensions = {ext.lower() for ext in extensions}
    video_files = sorted(
        (path for path in input_dir.rglob("*") if path.is_file() and path.suffix.lower() in allowed_extensions),
        key=lambda path: path.relative_to(input_dir).as_posix().casefold(),
    )

    if not video_files:
        print(f"未在 {input_dir} 中找到视频文件")
        return True

    print(f"找到 {len(video_files)} 个视频文件待处理")

    # 逐个处理并汇总，单个失败不能跳过后续输入，也不能被最终退出码掩盖。
    failed_count = 0
    for video_file in video_files:
        relative = video_file.relative_to(input_dir)
        try:
            success = process_video_file(
                video_file, output_dir / relative.parent, model_size, device,
                output_name=video_file.name,
            )
        except Exception as e:
            print(f"处理失败：{video_file}，原因：{e}")
            success = False
        if not success:
            failed_count += 1
    print(f"批次完成：成功 {len(video_files) - failed_count}，失败 {failed_count}")
    return failed_count == 0


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
        print(f"错误：输入路径 {input_path} 不存在")
        sys.exit(1)

    if input_path.is_file():
        # 处理单个文件。
        output_path.mkdir(parents=True, exist_ok=True)
        success = process_video_file(input_path, output_path, args.model, args.device)
    else:
        # 处理目录。
        success = process_directory(input_path, output_path, args.model, args.device)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
