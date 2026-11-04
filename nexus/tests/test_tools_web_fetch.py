"""Tests for orchestrator/tools/web_fetch.py"""
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.tools.web_fetch import _TextExtractor, run_web_fetch


class TestTextExtractor:
    def test_extracts_visible_text(self) -> None:
        ex = _TextExtractor()
        ex.feed("<html><body><p>Hello world</p></body></html>")
        assert "Hello" in ex.text
        assert "world" in ex.text

    def test_extracts_title(self) -> None:
        ex = _TextExtractor()
        ex.feed("<html><head><title>My Page</title></head><body>content</body></html>")
        assert ex.title == "My Page"
        assert "My Page" not in ex.text

    def test_skips_script_content(self) -> None:
        ex = _TextExtractor()
        ex.feed("<html><body><script>var x = 1;</script><p>visible</p></body></html>")
        assert "var x" not in ex.text
        assert "visible" in ex.text

    def test_skips_style_content(self) -> None:
        ex = _TextExtractor()
        ex.feed("<html><body><style>body { color: red; }</style><p>text</p></body></html>")
        assert "color" not in ex.text
        assert "text" in ex.text

    def test_skips_noscript_content(self) -> None:
        ex = _TextExtractor()
        ex.feed("<body><noscript>JS required</noscript><p>content</p></body>")
        assert "JS required" not in ex.text
        assert "content" in ex.text

    def test_empty_document_returns_empty_strings(self) -> None:
        ex = _TextExtractor()
        ex.feed("")
        assert ex.title == ""
        assert ex.text == ""

    def test_whitespace_only_nodes_are_ignored(self) -> None:
        ex = _TextExtractor()
        ex.feed("<body>   \n\t   <p>real</p>   </body>")
        assert ex.text.strip() == "real"

    def test_nested_skip_tags_handled(self) -> None:
        ex = _TextExtractor()
        ex.feed("<body><script><style>inner</style></script><p>ok</p></body>")
        assert "inner" not in ex.text
        assert "ok" in ex.text


class TestRunWebFetch:
    def _make_response(
        self,
        text: str = "<html><head><title>T</title></head><body>body text</body></html>",
        content_type: str = "text/html; charset=utf-8",
        url: str = "https://example.com",
        status_code: int = 200,
    ) -> MagicMock:
        resp = MagicMock()
        resp.text = text
        resp.headers = {"content-type": content_type}
        resp.url = url
        resp.raise_for_status = MagicMock()
        return resp

    async def test_html_response_returns_title_and_text(self) -> None:
        resp = self._make_response()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await run_web_fetch("https://example.com")

        assert result["title"] == "T"
        assert "body text" in result["text"]
        assert result["url"] == "https://example.com"

    async def test_non_html_returns_raw_text(self) -> None:
        resp = self._make_response(
            text='{"key": "value"}',
            content_type="application/json",
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await run_web_fetch("https://example.com/data.json")

        assert result["title"] == ""
        assert '{"key": "value"}' in result["text"]

    async def test_http_error_returns_error_dict_without_raising(self) -> None:
        import httpx

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await run_web_fetch("https://badhost.invalid")

        assert result["url"] == "https://badhost.invalid"
        assert "Fetch failed" in result["text"]
        assert result["title"] == ""

    async def test_text_truncated_at_max_chars(self) -> None:
        long_body = "x" * 20_000
        resp = self._make_response(
            text=f"<html><body><p>{long_body}</p></body></html>"
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await run_web_fetch("https://example.com", max_chars=100)

        assert len(result["text"]) <= 100

    async def test_http_status_error_returns_error_dict(self) -> None:
        import httpx

        resp = self._make_response(status_code=404)
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=MagicMock(), response=MagicMock()
            )
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await run_web_fetch("https://example.com/missing")

        assert "Fetch failed" in result["text"]
