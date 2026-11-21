"""
Cookie 提取与格式化工具
"""


def extract_douyin_cookies(full_cookie_string: str) -> tuple[str, bool, dict[str, str]]:
    required_cookies = [
        "odin_tt",
        "passport_fe_beating_status",
        "sid_guard",
        "uid_tt",
        "uid_tt_ss",
        "sid_tt",
        "sessionid",
        "sessionid_ss",
        "sid_ucp_v1",
        "ssid_ucp_v1",
        "passport_assist_user",
        "ttwid",
    ]
    critical_fields = ["sessionid", "uid_tt", "ttwid", "sid_guard"]

    cookie_dict = {}
    if "=" in full_cookie_string:
        for pair in full_cookie_string.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookie_dict[name.strip()] = value.strip()

    extracted = {}
    for cookie_name in required_cookies:
        extracted[cookie_name] = cookie_dict.get(cookie_name, "xxx")

    missing_fields = [f for f, v in extracted.items() if v == "xxx" or not v]
    critical_missing = [f for f in critical_fields if f in missing_fields]
    is_valid = len(critical_missing) == 0

    # 原样透传，避免丢失 UIFID / s_v_web_id / msToken 等风控字段
    cookie_pairs = [
        f"{name}={value}" for name, value in cookie_dict.items() if name and value
    ]
    formatted_cookie = ";".join(cookie_pairs) + ";" if cookie_pairs else ""

    return formatted_cookie, is_valid, extracted


def extract_and_format_cookies(full_cookie_string: str) -> str:
    formatted_cookie, _, _ = extract_douyin_cookies(full_cookie_string)
    return formatted_cookie
