# 视频字幕批量提取工具

![Subtitle Extractor](https://via.placeholder.com/800x400/1e1e1e/ffffff?text=Subtitle+Extractor)

**视频字幕批量提取工具** 是一个功能强大的自动化工具，用于从无字幕视频中提取音频并生成带时间戳的 Markdown 格式字幕。支持本地视频文件批量处理和在线视频URL解析，适用于内容创作者、研究人员、教育工作者和需要为视频添加字幕的用户。

## 📋 项目概览

### 核心功能
- **🎬 本地视频批量处理**：支持多种视频格式的批量字幕提取
- **🌐 在线视频URL解析**：支持主流视频平台（YouTube, Bilibili, Vimeo等）
- **📝 官方字幕优先**：自动检测并下载官方字幕（中文/英文优先）
- **🤖 AI语音转录**：无官方字幕时使用 faster-whisper 进行高质量AI转录
- **📄 Markdown输出**：生成带精确时间戳的Markdown格式字幕文件
- **⚡ 高效处理**：支持CPU/GPU加速，优化内存使用

### 技术栈
- **核心依赖**:
  - `faster-whisper` (Whisper模型的高效实现)
  - `yt-dlp` (在线视频解析和下载)
  - `moviepy` (视频音频处理)
- **编程语言**: Python 3.8+
- **硬件支持**: CPU + CUDA GPU加速

## 📂 目录结构

```
subtitle_extractor/
├── PROJECT.md                  # 项目文档（本文档）
├── extract_subs.py            # 本地视频字幕提取主模块
├── extract_online.py          # 在线视频字幕提取主模块
├── requirements.txt           # Python依赖包列表
└── test_output/               # 测试输出目录
    └── {video_title}.md      # 生成的字幕文件示例
```

## 🔧 功能特性详解

### 1. 本地视频处理 (extract_subs.py)
**支持格式**
- 视频格式: `.mp4`, `.avi`, `.mov`, `.mkv`, `.wmv`, `.flv`, `.webm`
- 音频提取: 自动从视频中分离音频流，避免直接处理高显存视频

**Whisper模型选项**
- `tiny`: 最小模型，速度快，精度较低
- `base`: 基础模型，平衡速度和精度（默认）
- `small`: 小型模型，较好精度
- `medium`: 中型模型，高精度
- `large`: 大型模型，最高精度（需要更多内存）

**设备支持**
- **CPU模式**: 使用int8量化，节省内存，兼容性好
- **GPU模式**: 使用float16计算，显著加速处理（需要CUDA）

### 2. 在线视频处理 (extract_online.py)
**平台支持**
- YouTube (包括官方字幕检测)
- Bilibili (包括弹幕和字幕)
- Vimeo
- 其他支持yt-dlp的平台

**智能处理流程**
```
在线视频URL
   ↓
检测官方字幕 (zh/en优先)
   ↓ 是
下载官方字幕 → 转换为Markdown
   ↓ 否
下载最佳音频流 → faster-whisper转录 → 生成Markdown
```

**字幕语言优先级**
1. 中文 (`zh`)
2. 英文 (`en`)
3. 其他可用语言

### 3. 输出格式
**Markdown结构**
```markdown
# 视频字幕

## [00:00:00.000 - 00:00:05.123]

这是第一段字幕内容。

## [00:00:05.124 - 00:00:10.456]

这是第二段字幕内容。
```

**时间戳格式**
- `HH:MM:SS.mmm` (小时:分钟:秒.毫秒)
- 精确到毫秒级别
- 与SRT格式兼容

## 🚀 使用指南

### 安装依赖
```bash
pip install -r requirements.txt
```

### 本地视频处理
```bash
# 处理单个视频文件
python extract_subs.py input_video.mp4 -o output_dir

# 处理整个目录
python extract_subs.py /path/to/videos -o output_dir

# 使用GPU加速（如果可用）
python extract_subs.py input_video.mp4 -d cuda

# 使用更大的模型（更高精度）
python extract_subs.py input_video.mp4 -m large
```

### 在线视频处理
```bash
# 处理单个在线视频
python extract_online.py "https://youtube.com/watch?v=..." -o output_dir

# 指定字幕语言优先级
python extract_online.py "URL" -l zh en ja

# 使用GPU加速在线处理
python extract_online.py "URL" -d cuda -m medium
```

### 命令行参数
**extract_subs.py 参数**
- `input`: 输入视频文件或目录路径
- `-o, --output`: 输出目录（默认: output）
- `-m, --model`: Whisper模型大小（默认: base）
- `-d, --device`: 运行设备（默认: cpu）

**extract_online.py 参数**
- `url`: 在线视频URL
- `-o, --output`: 输出目录（默认: output）
- `-m, --model`: Whisper模型大小（默认: base）
- `-d, --device`: 运行设备（默认: cpu）
- `-l, --languages`: 字幕语言优先级（默认: zh en）

## ⚙️ 配置说明

### 内存管理
- **临时文件清理**: 自动清理处理过程中的临时音频文件
- **内存优化**: CPU模式使用int8量化，减少内存占用
- **批处理**: 支持大目录批量处理，逐个文件处理避免内存溢出

### 性能调优
- **GPU加速**: CUDA设备可显著提升处理速度
- **模型选择**: 根据精度需求和硬件能力选择合适的模型
- **并发处理**: 可以并行运行多个实例处理不同视频

## 🐞 故障排除

### 常见问题
1. **"No video files found"**: 确认输入路径正确，文件扩展名受支持
2. **CUDA out of memory**: 降低模型大小或使用CPU模式
3. **Audio extraction failed**: 确认视频文件完整，codec受支持
4. **Online video not accessible**: 检查网络连接，确认视频未被限制

### 日志信息
- 错误信息会直接输出到控制台
- 成功处理的文件会显示完整的输入→输出路径
- 处理进度会实时显示

## 🤝 贡献指南

### 功能扩展
- 添加更多视频平台支持
- 支持更多字幕格式输出（SRT, VTT等）
- 添加多语言转录支持
- 优化内存使用和处理速度

### 代码贡献
- 保持模块化设计，遵循现有代码风格
- 添加适当的错误处理和日志记录
- 确保向后兼容性
- 提供完整的测试用例

## 📦 依赖要求

### Python包
- `faster-whisper>=0.5.0`
- `moviepy>=1.0.3`
- `yt-dlp>=2023.0.0`
- `torch>=1.13.0` (GPU支持需要)

### 系统要求
- **操作系统**: Windows, macOS, Linux
- **Python版本**: 3.8+
- **内存**: 至少4GB（large模型需要8GB+）
- **GPU**: 可选，CUDA 11.0+（推荐）

## 📝 版本信息

- **当前版本**: v1.0
- **最后更新**: 2026-03-14
- **许可证**: MIT License
- **作者**: Hextech Nexus Team

---

*让每一部视频都有声音，让每一个故事都能被听见！*