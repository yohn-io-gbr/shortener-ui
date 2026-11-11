import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_sign_in_rejects_bad_password_and_sign_out_closes_console(page: Page, app_url):
    page.goto(app_url)
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible(timeout=15000)
    expect(page.get_by_role("tab", name="Auth", exact=True)).to_have_count(0)

    page.get_by_label("Username", exact=True).fill("browser-test")
    page.get_by_label("Password", exact=True).fill("wrong-password")
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_text("Username or password is wrong.", exact=True)).to_be_visible()
    expect(page.get_by_role("tab", name="Auth", exact=True)).to_have_count(0)

    page.get_by_label("Password", exact=True).fill("local-test-password")
    # Commit the text input and wait for Streamlit's rerun before submitting.
    page.get_by_label("Password", exact=True).press("Enter")
    expect(page.get_by_text("Username or password is wrong.", exact=True)).to_be_hidden()
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("tab")).to_have_text(
        ["Auth", "Session", "Links", "Recorded stats", "Sampled analytics", "Members"]
    )
    page.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.get_by_label("Password", exact=True)).to_be_visible()
    expect(page.get_by_role("tab", name="Auth", exact=True)).to_have_count(0)
