"""字幕回归：所有视频、下载、模型及文件样本均为隔离替身。"""
import importlib
import io
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

with patch.dict("sys.modules", {
    "moviepy": types.SimpleNamespace(VideoFileClip=None),
    "faster_whisper": types.SimpleNamespace(WhisperModel=None),
}):
    local = importlib.import_module("extract_subs")
online = importlib.import_module("extract_online")


class ExtractorRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="subtitle-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_srt_and_vtt_preserve_text_and_timestamps(self):
        for name, content in {
            "fake.srt": "1\n00:00:00,000 --> 00:00:01,000\nhello synthetic\n",
            "fake.vtt": "WEBVTT\n\n00:00.000 --> 00:01.000 align:start\nhello synthetic\n",
        }.items():
            with self.subTest(name=name):
                source = self.root / name
                source.write_text(content, encoding="utf-8")
                result = online.convert_srt_to_markdown(source)
                self.assertIn("hello synthetic", result or "")
                self.assertIn("00:00:00.000 - 00:00:01.000", result or "")

    def test_invalid_subtitles_are_not_success_or_deleted(self):
        source = self.output / "bad.srt"
        source.write_text("not a subtitle", encoding="utf-8")
        with patch.object(online, "download_subtitles_from_url", return_value=(True, [str(source)])):
            result = online.process_online_video("https://example.invalid", self.output)
        self.assertFalse(result)
        self.assertTrue(source.exists())
        self.assertFalse((self.output / "bad.md").exists())

    def test_vtt_pipeline_writes_body_and_preserves_external_source(self):
        source = self.output / "fake.en.vtt"
        source.write_text("WEBVTT\n\nNOTE synthetic\n\ncue-1\n00:00:00.000 --> 00:00:01.000\nfirst\n\n00:01.000 --> 00:02.000\nsecond\n", encoding="utf-8")
        with patch.object(online, "download_subtitles_from_url", return_value=(True, [str(source)])):
            self.assertTrue(online.process_online_video("https://example.invalid", self.output))
        output = (self.output / "fake.en.md").read_text(encoding="utf-8")
        self.assertIn("first", output)
        self.assertIn("second", output)
        self.assertTrue(source.exists())

    def test_unknown_subtitle_format_is_preserved(self):
        source = self.output / "fake.ass"
        source.write_text("synthetic unsupported subtitle", encoding="utf-8")
        with patch.object(online, "download_subtitles_from_url", return_value=(True, [str(source)])):
            self.assertFalse(online.process_online_video("https://example.invalid", self.output))
        self.assertTrue(source.exists())

    def test_download_returns_unique_existing_candidates_in_language_order(self):
        en = self.root / "fake.en.vtt"
        zh = self.root / "fake.zh.srt"
        en.write_text("WEBVTT", encoding="utf-8")
        zh.write_text("synthetic", encoding="utf-8")
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download):
                return {"requested_subtitles": {
                    "en": {"filepath": str(en)}, "zh": {"filepath": str(zh)},
                    "zh-Hans": {"filepath": str(zh)}, "fr": {"filepath": str(en)},
                }}
        with patch.object(online, "load_yt_dlp", return_value=types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)):
            success, candidates = online.download_subtitles_from_url(
                "https://example.invalid", self.output, ["zh", "en"]
            )
        self.assertTrue(success)
        self.assertEqual(candidates, [str(zh), str(en)])

    def test_subtitle_candidates_continue_after_invalid_or_unreadable_file(self):
        invalid = self.root / "invalid.zh.srt"
        unreadable = self.root / "missing.zh.vtt"
        valid = self.root / "valid.en.vtt"
        invalid.write_text("not a subtitle", encoding="utf-8")
        valid.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nEnglish fallback\n", encoding="utf-8")
        with patch.object(online, "download_subtitles_from_url", return_value=(True, [str(invalid), str(unreadable), str(valid)])), patch.object(online, "download_audio_from_url") as audio:
            self.assertTrue(online.process_online_video("https://example.invalid", self.output))
        audio.assert_not_called()
        self.assertIn("English fallback", (self.output / "valid.en.md").read_text(encoding="utf-8"))
        self.assertTrue(invalid.exists())

    def test_reported_missing_subtitle_does_not_trigger_audio_fallback(self):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.folder = Path(options['outtmpl']).parent
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download):
                return {'requested_subtitles': {'zh': {'filepath': str(self.folder / 'missing.zh.srt')}}}
        with patch.object(online, 'load_yt_dlp', return_value=types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)), patch.object(online, 'download_audio_from_url', return_value=(False, 'not expected')) as audio:
            self.assertFalse(online.process_online_video('https://example.invalid', self.output))
        audio.assert_not_called()

    def test_audio_fallback_does_not_overwrite_existing_markdown(self):
        existing = self.output / 'video.md'
        existing.write_bytes(b'user-owned')
        def download(url, folder):
            audio = folder / 'video.wav'
            audio.write_bytes(b'synthetic')
            return True, str(audio)
        with patch.object(online, 'download_subtitles_from_url', return_value=(False, 'none')), patch.object(online, 'download_audio_from_url', side_effect=download), patch.object(online, 'load_transcription_helpers', return_value=(lambda *args: [object()], lambda segments: 'new text')):
            self.assertFalse(online.process_online_video('https://example.invalid', self.output))
        self.assertEqual(existing.read_bytes(), b'user-owned')

    def test_all_invalid_subtitles_fail_without_audio_fallback_and_keep_sources(self):
        created = []
        def download(url, folder, languages):
            invalid = folder / "invalid.zh.srt"
            invalid.write_text("not a subtitle", encoding="utf-8")
            created.append(invalid)
            return True, [str(invalid)]
        with patch.object(online, "download_subtitles_from_url", side_effect=download), patch.object(online, "download_audio_from_url") as audio:
            self.assertFalse(online.process_online_video("https://example.invalid", self.output))
        audio.assert_not_called()
        self.assertTrue(created[0].exists())

    def test_success_cleans_only_own_subtitle_directory_and_stops_at_first_valid(self):
        existing = self.output / "fake.zh.srt"
        existing.write_bytes(b"user-owned")
        created = []
        def download(url, folder, languages):
            first = folder / "fake.zh.srt"
            second = folder / "fake.en.vtt"
            first.write_text("1\n00:00:00,000 --> 00:00:01,000\nfirst\n", encoding="utf-8")
            second.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nsecond\n", encoding="utf-8")
            created.extend([first, second])
            return True, [str(first), str(second)]
        with patch.object(online, "download_subtitles_from_url", side_effect=download):
            self.assertTrue(online.process_online_video("https://example.invalid", self.output))
        self.assertEqual(existing.read_bytes(), b"user-owned")
        self.assertIn("first", (self.output / "fake.zh.md").read_text(encoding="utf-8"))
        self.assertFalse((self.output / "fake.en.md").exists())
        self.assertFalse(created[0].parent.exists())

    def test_existing_markdown_is_preserved_and_failed_source_retained(self):
        existing = self.output / "fake.zh.md"
        existing.write_text("user-owned", encoding="utf-8")
        created = []
        def download(url, folder, languages):
            source = folder / "fake.zh.srt"
            source.write_text("1\n00:00:00,000 --> 00:00:01,000\nfirst\n", encoding="utf-8")
            created.append(source)
            return True, [str(source)]
        with patch.object(online, "download_subtitles_from_url", side_effect=download):
            self.assertFalse(online.process_online_video("https://example.invalid", self.output))
        self.assertEqual(existing.read_text(encoding="utf-8"), "user-owned")
        self.assertTrue(created[0].exists())

    def test_directory_preserves_relative_paths_and_extensions_without_duplicates(self):
        inputs = self.root / "inputs"
        for name in ("one/video.mp4", "two/video.mp4", "one/video.mkv", "one/Mixed.Mp4"):
            path = inputs / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        calls = []

        def process(path, output, *args, **kwargs):
            calls.append((path.relative_to(inputs).as_posix(), output.relative_to(self.output).as_posix(), kwargs))

        with patch.object(local, "process_video_file", side_effect=process):
            local.process_directory(inputs, self.output)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({name for name, _, _ in calls}), 4)
        self.assertEqual({(rel, folder, opts.get("output_name")) for rel, folder, opts in calls}, {
            ("one/video.mp4", "one", "video.mp4"), ("two/video.mp4", "two", "video.mp4"),
            ("one/video.mkv", "one", "video.mkv"), ("one/Mixed.Mp4", "one", "Mixed.Mp4"),
        })

    def test_extraction_failure_closes_video(self):
        clip = Mock()
        clip.audio.write_audiofile.side_effect = RuntimeError("synthetic")
        with patch.object(local, "VideoFileClip", return_value=clip):
            self.assertFalse(local.extract_audio_from_video("synthetic.mp4", "synthetic.wav"))
        clip.close.assert_called_once()

    def test_batch_outputs_are_separate_files(self):
        inputs = self.root / "inputs"
        names = ("one/video.mp4", "one/video.mkv", "two/video.mp4")
        for name in names:
            path = inputs / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        segment = types.SimpleNamespace(start=0, end=1, text="synthetic transcript")
        with patch.object(local, "extract_audio_from_video", return_value=True), patch.object(local, "transcribe_audio", return_value=[segment]):
            local.process_directory(inputs, self.output)
        self.assertEqual({p.relative_to(self.output).as_posix() for p in self.output.rglob("*.md")}, {name + ".md" for name in names})

    def test_partial_audio_failure_cleans_only_owned_path(self):
        existing = self.output / "video_temp.wav"
        existing.write_bytes(b"user-owned")
        created = []

        def extract(video, audio):
            created.append(Path(audio))
            Path(audio).write_bytes(b"partial synthetic")
            return False

        with patch.object(local, "extract_audio_from_video", side_effect=extract):
            local.process_video_file(Path("video.mp4"), self.output)
        self.assertEqual(existing.read_bytes(), b"user-owned")
        self.assertNotEqual(created[0], existing)
        self.assertFalse(created[0].exists())

    def test_local_success_keeps_single_file_output_name_and_cleans_audio(self):
        created = []

        def extract(video, audio):
            created.append(Path(audio))
            Path(audio).write_bytes(b"synthetic audio")
            return True

        segment = types.SimpleNamespace(start=0, end=1, text="synthetic transcript")
        with patch.object(local, "extract_audio_from_video", side_effect=extract), patch.object(local, "transcribe_audio", return_value=[segment]):
            local.process_video_file(Path("video.mp4"), self.output)
        self.assertIn("synthetic transcript", (self.output / "video.md").read_text(encoding="utf-8"))
        self.assertFalse(created[0].exists())

    def test_markdown_write_failure_cleans_local_audio(self):
        created = []

        def extract(video, audio):
            created.append(Path(audio))
            Path(audio).write_bytes(b"synthetic audio")
            return True

        segment = types.SimpleNamespace(start=0, end=1, text="synthetic")
        with patch.object(local, "extract_audio_from_video", side_effect=extract), patch.object(local, "transcribe_audio", return_value=[segment]), patch("builtins.open", side_effect=OSError("synthetic write failure")):
            try:
                local.process_video_file(Path("video.mp4"), self.output)
            except OSError:
                pass
        self.assertTrue(created)
        self.assertFalse(created[0].exists())

    def test_online_transcription_and_output_failures_clean_owned_audio(self):
        for failure in ("transcribe", "write", "success"):
            with self.subTest(failure=failure):
                created = []

                def download(url, folder):
                    path = folder / "online.wav"
                    created.append(path)
                    path.write_bytes(b"synthetic audio")
                    return True, str(path)

                transcribe = Mock(return_value=[object()])
                if failure == "transcribe":
                    transcribe.side_effect = RuntimeError("synthetic")
                writer = patch("builtins.open", side_effect=OSError("synthetic")) if failure == "write" else nullcontext()
                with patch.object(online, "download_subtitles_from_url", return_value=(False, "none")), patch.object(online, "download_audio_from_url", side_effect=download), patch.object(online, "load_transcription_helpers", return_value=(transcribe, lambda segments: "synthetic markdown")), writer:
                    result = online.process_online_video("https://example.invalid", self.output)
                self.assertEqual(result, failure == "success")
                self.assertFalse(created[0].exists())
                self.assertFalse(created[0].parent.exists())
        self.assertEqual((self.output / "online.md").read_text(encoding="utf-8"), "synthetic markdown")

    def test_online_dependency_failure_cleans_only_downloaded_temp_audio(self):
        existing = self.output / "video.wav"
        existing.write_bytes(b"user-owned")
        created = []

        def download(url, folder):
            path = folder / "video.wav"
            created.append(path)
            path.write_bytes(b"synthetic download")
            return True, str(path)

        with patch.object(online, "download_subtitles_from_url", return_value=(False, "none")), patch.object(online, "download_audio_from_url", side_effect=download), patch.object(online, "load_transcription_helpers", side_effect=ImportError("synthetic")):
            try:
                result = online.process_online_video("https://example.invalid", self.output)
            except ImportError:
                result = False
        self.assertFalse(result)
        self.assertEqual(existing.read_bytes(), b"user-owned")
        self.assertNotEqual(created[0], existing)
        self.assertFalse(created[0].exists())

    def _assert_cli_exit(self, input_path, expected):
        with patch.object(sys, "argv", ["extract_subs.py", str(input_path), "-o", str(self.output)]):
            with self.assertRaises(SystemExit) as exit_result:
                sys.exit(local.main())
        self.assertEqual(exit_result.exception.code, expected)

    def test_single_file_write_failure_exits_nonzero(self):
        source = self.root / "fake.mp4"
        source.touch()
        segment = types.SimpleNamespace(start=0, end=1, text="synthetic")
        with patch.object(local, "extract_audio_from_video", return_value=True), patch.object(local, "transcribe_audio", return_value=[segment]), patch("builtins.open", side_effect=OSError("synthetic disk full")):
            self._assert_cli_exit(source, 1)
        self.assertFalse((self.output / "fake.md").exists())

    def test_mixed_batch_write_failure_continues_and_exits_nonzero(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        names = ["a_fail.mp4", "b_ok.mp4", "c_ok.mp4"]
        for name in names:
            (inputs / name).touch()
        original_open = open
        processed = []

        def extract(video, audio):
            processed.append(Path(video).name)
            return True

        def write_with_one_failure(path, *args, **kwargs):
            if Path(path).name == "a_fail.mp4.md":
                raise PermissionError("synthetic write denied")
            return original_open(path, *args, **kwargs)

        segment = types.SimpleNamespace(start=0, end=1, text="synthetic")
        with patch.object(local, "extract_audio_from_video", side_effect=extract), patch.object(local, "transcribe_audio", return_value=[segment]), patch("builtins.open", side_effect=write_with_one_failure):
            self._assert_cli_exit(inputs, 1)
        self.assertEqual(processed, names)
        self.assertFalse((self.output / "a_fail.mp4.md").exists())
        self.assertTrue((self.output / "b_ok.mp4.md").exists())
        self.assertTrue((self.output / "c_ok.mp4.md").exists())

    def test_batch_setup_error_does_not_skip_remaining_files(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        for name in ("a.mp4", "b.mp4"):
            (inputs / name).touch()
        with patch.object(local, "process_video_file", side_effect=[OSError("synthetic mkdir failure"), True]) as process:
            self._assert_cli_exit(inputs, 1)
        self.assertEqual(process.call_count, 2)

    def test_successful_single_and_batch_exit_zero(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        source = inputs / "fake.mp4"
        source.touch()
        with patch.object(local, "process_video_file", return_value=True):
            self._assert_cli_exit(source, 0)
            self._assert_cli_exit(inputs, 0)


if __name__ == "__main__":
    unittest.main()
