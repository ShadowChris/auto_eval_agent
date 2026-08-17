from fastapi import FastAPI
from fastapi.testclient import TestClient

from auto_eval.web import server


def test_index_injects_content_version_and_disables_html_cache() -> None:
    response = server.index()
    html = response.body.decode("utf-8")
    version = server._static_asset_version()

    assert server.STATIC_VERSION_TOKEN not in html
    assert f'/static/app.js?v={version}' in html
    assert f'/static/style.css?v={version}' in html
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["pragma"] == "no-cache"


def test_static_cache_depends_on_version_query() -> None:
    app = FastAPI()
    app.mount(
        "/static",
        server.VersionedStaticFiles(directory=str(server.STATIC_DIR)),
        name="static",
    )
    client = TestClient(app)

    versioned = client.get("/static/app.js?v=test-version")
    unversioned = client.get("/static/app.js")

    assert versioned.status_code == 200
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert unversioned.status_code == 200
    assert unversioned.headers["cache-control"] == "no-cache, must-revalidate"
