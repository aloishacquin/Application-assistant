import httpx
import pytest

from jobapply.ingest.fetch import FetchError, fetch_offer_text

OFFER_HTML = """<html><head><title>Data Engineer</title></head><body>
<nav>Home | Jobs | About</nav>
<article><h1>Data Engineer at Example Pte Ltd</h1>
<p>We are looking for a Data Engineer to build batch and streaming pipelines with Airflow
and PySpark. You will work with analysts across Singapore to deliver reliable data.</p>
<p>Requirements: strong Python and SQL, experience with Spark, good communication skills,
and a degree in Computer Science or Engineering. Salary SGD 7,000 to 9,000 per month.</p>
</article><footer>Copyright</footer></body></html>"""


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_extracts_main_text() -> None:
    client = client_for(lambda request: httpx.Response(200, html=OFFER_HTML))
    text = fetch_offer_text("https://jobs.example.com/123", client=client)
    assert "Data Engineer" in text
    assert "PySpark" in text


def test_follows_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "short.example":
            return httpx.Response(302, headers={"Location": "https://jobs.example.com/1"})
        return httpx.Response(200, html=OFFER_HTML)

    assert "PySpark" in fetch_offer_text("https://short.example/x", client=client_for(handler))


def test_http_error() -> None:
    client = client_for(lambda request: httpx.Response(404))
    with pytest.raises(FetchError, match="404"):
        fetch_offer_text("https://jobs.example.com/missing", client=client)


def test_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(FetchError, match="Impossible"):
        fetch_offer_text("https://jobs.example.com/1", client=client_for(handler))


def test_empty_page() -> None:
    client = client_for(lambda request: httpx.Response(200, html="<html><body></body></html>"))
    with pytest.raises(FetchError, match="Pas assez de texte"):
        fetch_offer_text("https://jobs.example.com/js-only", client=client)


@pytest.mark.parametrize("url", ["ftp://example.com/offer", "jobs.example.com/1", "file:///etc/x"])
def test_invalid_url(url: str) -> None:
    with pytest.raises(FetchError, match="URL invalide"):
        fetch_offer_text(url)
