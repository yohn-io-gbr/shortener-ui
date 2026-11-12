import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_local_app_is_available(page: Page):
    response = page.goto("http://localhost:8501/")

    assert response is not None
    assert response.status == 200
    expect(
        page.get_by_role("heading", name="Staging shortener console", exact=True)
    ).to_be_visible(timeout=15000)
