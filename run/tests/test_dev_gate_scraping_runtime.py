"""scraping 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (patch, scrapling_client, sys)

pytestmark = pytest.mark.dev_gate



def test_response_text_repairs_latin1_mislabeled_utf8() -> None:
    """回归：无 charset 资源被 Scrapling 误报 ISO-8859-1 时曾丢失中文强化名。"""

    class MislabeledResponse:
        body = "回归基本功".encode("utf-8")
        encoding = "ISO-8859-1"

    assert scrapling_client._response_text(MislabeledResponse()) == "回归基本功"
    assert scrapling_client._response_html(MislabeledResponse()) == "回归基本功"


def test_response_text_keeps_declared_encoding_for_real_latin1() -> None:
    """真 latin-1 字节（非法 UTF-8）仍按声明编码解码，正常路径不回归。"""

    class Latin1Response:
        body = b"caf\xe9"
        encoding = "ISO-8859-1"

    assert scrapling_client._response_text(Latin1Response()) == "café"
    assert scrapling_client._response_html(Latin1Response()) == "café"


def test_scrapling_fetch_text_keeps_internal_retry() -> None:
    """短文本抓取也必须保留一次 Scrapling 内部 retry，避免 session 被提前释放。"""

    calls = []

    class GoodResponse:
        body = "{}"
        status = 200

    class RecordingFetcher:
        @staticmethod
        def get(*_args, **kwargs):
            calls.append(kwargs)
            return GoodResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = RecordingFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
    ):
        result = scrapling_client.fetch_text("https://example.test/data.json", max_attempts=1)

    assert result.status_code == 200
    assert calls
    assert calls[0]["retries"] == 1


def test_scrapling_success_does_not_call_requests_fallback() -> None:
    class GoodResponse:
        body = b'{"ok":true}'
        status = 200

    class StaticFetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            return GoodResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = StaticFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
        patch("requests.get", side_effect=AssertionError("成功的 Scrapling 请求不得调用 fallback")),
    ):
        result = scrapling_client.fetch_text(
            "https://success.example/data.json",
            fallback_backend="requests",
            max_attempts=1,
        )

    assert result.status_code == 200
    assert result.backend == "http"
    assert result.fallback_used is False


def test_scrapling_tls_failure_uses_one_requests_fallback_with_provenance() -> None:
    scrapling_calls: list[dict[str, object]] = []
    requests_calls: list[dict[str, object]] = []

    class TlsFailingFetcher:
        @staticmethod
        def get(*_args, **kwargs):
            scrapling_calls.append(kwargs)
            raise RuntimeError("curl: (35) BoringSSL SSL_connect: SSL_ERROR_SYSCALL")

    class RequestsResponse:
        status_code = 200
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def iter_content(*, chunk_size):
            assert chunk_size == 64 * 1024
            return iter((b'{"ok":', b"true}"))

    def requests_get(*_args, **kwargs):
        requests_calls.append(kwargs)
        return RequestsResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = TlsFailingFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
        patch("requests.get", side_effect=requests_get),
    ):
        result = scrapling_client.fetch_text(
            "https://tls.example/data.json",
            timeout_ms=2_000,
            fallback_backend="requests",
            max_attempts=2,
            retry_backoff_seconds=0,
            max_response_bytes=1024,
        )

    assert len(scrapling_calls) == 2
    assert len(requests_calls) == 1
    assert result.status_code == 200
    assert result.text == '{"ok":true}'
    assert result.backend == "requests_fallback"
    assert result.fallback_used is True
    assert result.fallback_from == "tls_error"
    assert result.attempts == 3


@pytest.mark.parametrize("status_code", (403, 429))
def test_scrapling_blocked_response_never_uses_requests_fallback(status_code: int) -> None:
    class BlockedResponse:
        body = b"blocked"
        status = status_code

    class StaticFetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            return BlockedResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = StaticFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
        patch("requests.get", side_effect=AssertionError("403/429 不得调用 fallback")),
    ):
        result = scrapling_client.fetch_text(
            f"https://blocked-{status_code}.example/data.json",
            fallback_backend="requests",
            max_attempts=1,
        )

    assert result.status_code == status_code
    assert result.error_kind == f"http_{status_code}"
    assert result.fallback_used is False


def test_scrapling_response_size_gate_applies_to_primary_and_fallback() -> None:
    class OversizeResponse:
        body = b"12345"
        status = 200

    class StaticFetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            return OversizeResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = StaticFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
        patch("requests.get", side_effect=AssertionError("过大主响应不得调用 fallback")),
    ):
        primary = scrapling_client.fetch_text(
            "https://large-primary.example/data.json",
            fallback_backend="requests",
            max_attempts=1,
            max_response_bytes=4,
        )

    assert primary.error == "response_too_large"
    assert primary.fallback_used is False

    class TlsFailingFetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            raise RuntimeError("curl: (35) TLS connect failed")

    class OversizeRequestsResponse:
        status_code = 200
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def iter_content(*, chunk_size):
            assert chunk_size == 64 * 1024
            return iter((b"123", b"45"))

    fetchers_module.Fetcher = TlsFailingFetcher
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
        patch("requests.get", return_value=OversizeRequestsResponse()),
    ):
        fallback = scrapling_client.fetch_text(
            "https://large-fallback.example/data.json",
            timeout_ms=2_000,
            fallback_backend="requests",
            max_attempts=1,
            max_response_bytes=4,
        )

    assert fallback.error == "response_too_large"
    assert fallback.error_kind == "invalid_payload"
    assert fallback.backend == "requests_fallback"
    assert fallback.fallback_used is True
    assert fallback.fallback_from == "tls_error"

def test_scrapling_fetch_page_get_is_static_only_and_timeout_uses_seconds() -> None:
    """静态 get 不得导入 DynamicFetcher，timeout 仍必须使用秒。"""

    calls = []

    class GoodResponse:
        html = "<html></html>"
        status = 200

        def css(self, _selector):
            raise AssertionError("未传 selector 时不应读取 css")

    class RecordingFetcher:
        @staticmethod
        def get(*_args, **kwargs):
            calls.append(kwargs)
            return GoodResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = RecordingFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
    ):
        result = scrapling_client.fetch_page("https://example.test", timeout_ms=30_000, max_attempts=1)

    assert result.status_code == 200
    assert calls
    assert calls[0]["timeout"] == 30.0
    assert calls[0]["retries"] == 1
