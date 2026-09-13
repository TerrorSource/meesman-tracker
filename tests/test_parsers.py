"""Pure functies: geld-parsing/-formattering, berichtopbouw, stub-detectie."""
import pytest

from app.core import fmt_eur, fmt_eur_delta, fmt_local, fmt_pct, parse_form_amount, to_float
from app.scraper import _is_redirect_stub, is_browser_launch_failure, parse_eur_text
from app.telegram import build_balance_change_message


class _Acc:
    def __init__(self, number, label, value):
        self.account_number, self.label, self.value_eur = number, label, value


@pytest.mark.parametrize("raw, expected", [
    ("€ 29.869,81", 29869.81),
    ("1.234", 1234.0),          # alleen punten in groepen van 3 → duizendtallen
    ("1.234.567", 1234567.0),
    ("1.23", 1.23),             # echte decimaal blijft decimaal
    ("4987,50", 4987.50),
    ("-1.234", -1234.0),
])
def test_parse_eur_text(raw, expected):
    assert parse_eur_text(raw) == expected


def test_to_float_handles_legacy_text_rows():
    assert to_float(123.45) == 123.45
    assert to_float("29.869,81") == 29869.81
    assert to_float(None) == 0.0


@pytest.mark.parametrize("raw, expected", [
    ("29.869,81", 29869.81),
    ("€ 4.987,50", 4987.5),
    ("-123,45", -123.45),
])
def test_parse_form_amount_ok(raw, expected):
    assert parse_form_amount(raw) == expected


@pytest.mark.parametrize("bad", ["500", "1,2,3", "", "abc"])
def test_parse_form_amount_rejects(bad):
    with pytest.raises((ValueError, AttributeError)):
        parse_form_amount(bad)


def test_formatting():
    assert fmt_eur(30180.36) == "€ 30.180,36"
    assert fmt_eur_delta(87.93) == "+€ 87,93"
    assert fmt_eur_delta(-435.54) == "-€ 435,54"
    assert fmt_pct(0.27) == "+0,27%"
    assert fmt_pct(-1.297) == "-1,30%"


def test_fmt_local_converts_to_amsterdam():
    # 07:14 UTC in augustus = 09:14 Amsterdam (zomertijd)
    assert fmt_local("2026-08-13T07:14:05.534117+00:00") == "13-08-2026 09:14"
    assert fmt_local("") == ""
    assert fmt_local("geen datum") == "geen datum"


def test_balance_message_layout_positive_and_negative():
    msg = build_balance_change_message(
        [_Acc("22404586", "Beleggingen", 32655.62), _Acc("25110311", "Pensioen", 56487.84), _Acc("", "", 0.0)],
        {"22404586": 32567.69, "25110311": 56335.74},
    )
    assert "Δ:   +€ 87,93" in msg and "(+0,27%)" in msg and "(+€ 240,03, +0,27%)" in msg
    assert "➡️" not in msg  # spookregel gefilterd

    msg2 = build_balance_change_message(
        [_Acc("22404586", "Beleggingen", 33192.87), _Acc("25110311", "Pensioen", 57417.17)],
        {"22404586": 33628.41, "25110311": 58170.58},
    )
    assert "Δ:   -€ 435,54" in msg2 and "(-€ 1.188,95, -1,30%)" in msg2


def test_redirect_stub_detection():
    stub = ('<script>\n    window.addEventListener("load", () => window.location = '
            "'https://public-api.meesman.nl/v1/user/signInWithMeesmanAuthentication?redirect=x');\n</script>")
    assert _is_redirect_stub(stub) is True
    assert _is_redirect_stub("<html>" + "x" * 5000 + "</html>") is False


def test_launch_failure_detection():
    assert is_browser_launch_failure(Exception("BrowserType.launch: Timeout 180000ms exceeded")) is True
    assert is_browser_launch_failure(Exception("Page.wait_for_selector: Timeout")) is False


def test_balance_message_thresholds_and_exclude():
    accs = [_Acc("22404586", "Beleggingen", 32655.62), _Acc("25110311", "Pensioen", 56487.84)]
    prev = {"22404586": 32567.69, "25110311": 56335.74}   # totaal +240,03 (+0,27%)
    assert build_balance_change_message(accs, prev) is not None
    assert build_balance_change_message(accs, prev, min_eur=1000) is None        # onder €-drempel
    assert build_balance_change_message(accs, prev, min_pct=1.0) is None         # onder %-drempel
    assert build_balance_change_message(accs, prev, min_eur=200, min_pct=0.2) is not None
    # nieuwe rekening wordt altijd gemeld, ongeacht drempels
    assert build_balance_change_message(accs, {"22404586": 32567.69}, min_eur=99999) is not None
    # gearchiveerde rekening blijft buiten het bericht en het totaal
    msg = build_balance_change_message(accs, prev, exclude={"25110311"})
    assert "Pensioen" not in msg and "€ 32.655,62" in msg
