from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def _app() -> AppTest:
    app = AppTest.from_file(str(APP))
    # Unit tests must not inherit the developer's local console credentials.
    app.secrets["AUTH_USERNAME"] = ""
    app.secrets["AUTH_PASSWORD"] = ""
    app.secrets["SHORTENER_BASE_URL"] = ""
    return app


def test_console_stays_closed_without_basic_auth_secrets():
    app = _app()
    app.run()
    assert not app.tabs
    assert any("AUTH_USERNAME" in error.value for error in app.error)


def test_basic_auth_rejects_a_wrong_password_and_opens_after_the_right_one():
    app = _app()
    app.secrets["AUTH_USERNAME"] = "operator"
    app.secrets["AUTH_PASSWORD"] = "temp-gate-check"
    app.run()
    app.text_input(key="basic_username").set_value("operator")
    app.text_input(key="basic_password").set_value("nope")
    app.button[0].click().run()
    assert not app.tabs
    assert any("wrong" in error.value for error in app.error)

    app.text_input(key="basic_password").set_value("temp-gate-check")
    app.button[0].click().run()
    assert [tab.label for tab in app.tabs] == [
        "Auth",
        "Session",
        "Links",
        "Recorded stats",
        "Sampled analytics",
        "Members",
    ]

    next(button for button in app.button if button.label == "Sign out").click().run()
    assert not app.tabs
    assert any(field.label == "Password" for field in app.text_input)
