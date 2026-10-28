"""scraping 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (patch, scrapling_client, sys)

pytestmark = pytest.mark.dev_gate











def test_scrapling_tls_error_contract() -> None:
    """curl TLS 错误必须被分类为 tls_error，并带上下文向上抛出。"""

    error_text = "curl: (35) TLS connect error: error:00000000:OPENSSL_internal:invalid library (0)"
    assert scrapling_client.classify_fetch_error(error_text) == "tls_error"

    class BadFetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            raise RuntimeError(error_text)

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = BadFetcher
    fetchers_module.DynamicFetcher = BadFetcher
    fetchers_module.StealthyFetcher = BadFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
    ):
        page_result = scrapling_client.fetch_page("https://example.test")
    assert page_result.error_kind == "tls_error"
