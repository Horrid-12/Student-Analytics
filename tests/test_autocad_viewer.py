from fastapi.testclient import TestClient

from app.main import app
from test_pages_36 import make_user


def test_autocad_page_renders_the_viewer_bundle_url():
    with TestClient(app) as client:
        make_user(client, role="student")
        response = client.get("/autocad")

    assert response.status_code == 200
    assert 'var BUNDLE_URL = "http://testserver/static/cad-viewer/cad-viewer.js";' in response.text
    assert 'id="cadViewerModal"' in response.text
