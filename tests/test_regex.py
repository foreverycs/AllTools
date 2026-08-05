"""正则测试工具 — 核心逻辑与页面 / API。"""

import pytest

from coding import RegexError, generate_regex, replace_regex
from coding import test_regex as run_regex


def test_basic_match_count_and_span():
    r = run_regex(r"\d+", "abc 123 def 456")
    assert r["ok"] is True
    assert r["match_count"] == 2
    assert r["matches"][0]["span"] == [4, 7]
    assert r["matches"][0]["text"] == "123"


def test_capture_groups():
    r = run_regex(r"(\d{4})-(\d{2})", "日期 2024-01 和 2025-02")
    assert r["match_count"] == 2
    first = r["matches"][0]
    assert first["groups"][1]["text"] == "2024"
    assert first["groups"][2]["text"] == "01"
    assert r["group_count"] == 2


def test_named_groups():
    r = run_regex(r"(?P<year>\d{4})", "2024")
    assert r["capture_names"] == ["year"]
    assert r["matches"][0]["groups"][1]["name"] == "year"


def test_flags_ignore_case():
    no_flag = run_regex(r"hello", "Hello HELLO")
    with_flag = run_regex(r"hello", "Hello HELLO", flags_raw="i")
    assert no_flag["match_count"] == 0
    assert with_flag["match_count"] == 2


def test_flags_multiline():
    r = run_regex(r"^\w+", "foo\nbar", flags_raw="m")
    assert r["match_count"] == 2


def test_count_limit():
    r = run_regex(r"\d+", "1 2 3 4 5", count=2)
    assert r["match_count"] == 2


def test_replace_basic():
    r = replace_regex(r"\d+", "a1 b22 c333", "#")
    assert r["result"] == "a# b# c#"
    assert r["replace_count"] == 3


def test_replace_with_backreference():
    r = replace_regex(r"(\w+)@(\w+)\.(\w+)", "a@b.com", r"$2.$1@$3")
    assert r["result"] == "b.a@com"


def test_invalid_pattern_raises():
    with pytest.raises(RegexError):
        run_regex(r"(", "abc")


def test_unknown_flag_raises():
    with pytest.raises(RegexError):
        run_regex(r"a", "abc", flags_raw="z")


def test_registry_has_regex():
    from tools import TOOL_REGISTRY, get_tool_by_slug

    slugs = [t["slug"] for t in TOOL_REGISTRY]
    assert "regex" in slugs
    tool = get_tool_by_slug("regex")
    assert tool["category"] == "text"
    assert tool["route"] == "/tools/regex"


def test_regex_page_and_test_api():
    from fastapi.testclient import TestClient

    from app import app

    client = TestClient(app)
    page = client.get("/tools/regex")
    assert page.status_code == 200
    assert "正则" in page.text

    r = client.post("/tools/regex/test", data={"pattern": r"\d+", "text": "a1 b2"})
    assert r.status_code == 200
    assert r.json()["match_count"] == 2

    rep = client.post(
        "/tools/regex/replace",
        data={"pattern": r"\d+", "text": "a1 b2", "replacement": "X"},
    )
    assert rep.status_code == 200
    assert rep.json()["result"] == "aX bX"

    bad = client.post("/tools/regex/test", data={"pattern": r"(", "text": "abc"})
    assert bad.status_code == 400


def test_generate_digits():
    g = generate_regex("订单号：123456，金额：￥1,234.56", "123456")
    assert g["pattern"] == r"(\d{6})"
    assert g["prefix"] == "：" or g["prefix"] != ""


def test_generate_date():
    g = generate_regex("日期 2024-01-15 记录", "2024-01-15")
    assert g["pattern"] == r"(\d{4}\-\d{2}\-\d{2})"
    # Generated pattern should match the target and generalize the digits.
    check = run_regex(g["pattern"], "今天 2025-12-31 结束")
    assert check["match_count"] == 1


def test_generate_phone():
    g = generate_regex("电话 13800138000 分机", "13800138000")
    assert g["pattern"] == r"(\d{11})"
    assert run_regex(g["pattern"], "手机 13900000000 联系")["match_count"] == 1


def test_generate_identifier_prefix_lines():
    sample = """_DSP28x
_ECanaMailBoxCfg
_ECanbMailBoxCfg
_Get
_Get
_GpioDataRegs
_Init8435
_InitECana
_InitECanaGpio
_InitECanb
_InitECanbGpio
_Init
_ad7490
_bpcu
_bpcus"""
    g = generate_regex(sample, "_Init8435")
    assert g["pattern"] == r"^(_Init\w*)"
    assert g["suggest_flags"] == ["m"]
    r = run_regex(g["pattern"], sample, flags_raw="m")
    texts = [m["text"] for m in r["matches"]]
    assert texts == [
        "_Init8435",
        "_InitECana",
        "_InitECanaGpio",
        "_InitECanb",
        "_InitECanbGpio",
        "_Init",
    ]


def test_generate_bare_prefix():
    g = generate_regex("A_Init99\nA_Initx\nA_Other", "A_Init")
    assert g["pattern"].startswith("^(A_Init")
    r = run_regex(g["pattern"], "A_Init99\nA_Initx\nA_Other", flags_raw="m")
    assert [m["text"] for m in r["matches"]] == ["A_Init99", "A_Initx"]


def test_generate_email_not_prefix_generalized():
    g = generate_regex("联系 support@example.com 或 sales@company.cn", "support@example.com")
    assert g["pattern"] == r"(\w+@\w+\.\w+)"


def test_generate_lone_identifier_is_literal():
    # A bare word like `Init` must not generalize to `\w+` (over-match).
    sample = "A_Init99\nB_Other\nC_Initx"
    g = generate_regex(sample, "Init", anchor="contains")
    assert g["pattern"] == r"(Init)"
    r = run_regex(g["pattern"], sample)
    assert len(r["matches"]) == 2


def test_generate_anchor_contains():
    sample = "A_Init99\nB_Other\nC_Initx"
    g = generate_regex(sample, "Init", anchor="contains")
    assert g["pattern"] == r"(Init)"
    assert g["suggest_flags"] == []


def test_generate_anchor_start():
    sample = "A_Init99\nB_Other\nC_Initx"
    g = generate_regex(sample, "A_Init99", anchor="start")
    assert g["pattern"].startswith("^")
    assert g["suggest_flags"] == ["m"]
    r = run_regex(g["pattern"], sample, flags_raw="m")
    assert [m["text"] for m in r["matches"]] == ["A_Init99"]


def test_generate_anchor_line():
    sample = "AA\nBB\nAA"
    g = generate_regex(sample, "BB", anchor="line")
    assert g["pattern"] == r"^(BB)$"
    assert g["suggest_flags"] == ["m"]
    r = run_regex(g["pattern"], sample, flags_raw="m")
    assert [m["text"] for m in r["matches"]] == ["BB"]


def test_generate_anchor_end():
    sample = "XX99\nYY88"
    g = generate_regex(sample, "99", anchor="end")
    assert g["pattern"].endswith("$")
    r = run_regex(g["pattern"], sample, flags_raw="m")
    assert [m["text"] for m in r["matches"]] == ["99", "88"]


def test_generate_bad_anchor_raises():
    from coding import RegexError as RErr

    with pytest.raises(RErr):
        generate_regex("abc", "a", anchor="bogus")


def test_generate_missing_target_raises():
    with pytest.raises(RegexError):
        generate_regex("订单号：123456", "999999")
    with pytest.raises(RegexError):
        generate_regex("订单号：123456", "")


def test_generate_api():
    from fastapi.testclient import TestClient

    from app import app

    client = TestClient(app)
    r = client.post(
        "/tools/regex/generate",
        data={"text": "订单号：123456", "target": "123456", "flags": ""},
    )
    assert r.status_code == 200
    assert r.json()["pattern"] == r"(\d{6})"

    bad = client.post(
        "/tools/regex/generate",
        data={"text": "订单号：123456", "target": "888888", "flags": ""},
    )
    assert bad.status_code == 400

