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
        pairs = full_cookie_string.replace(" ", "").split(";")
        for pair in pairs:
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookie_dict[name.strip()] = value.strip()

    extracted = {}
    for cookie_name in required_cookies:
        extracted[cookie_name] = cookie_dict.get(cookie_name, "xxx")

    missing_fields = [f for f, v in extracted.items() if v == "xxx" or not v]
    critical_missing = [f for f in critical_fields if f in missing_fields]
    is_valid = len(critical_missing) == 0

    cookie_pairs = [f"{name}={extracted.get(name, 'xxx')}" for name in required_cookies]
    formatted_cookie = ";".join(cookie_pairs) + ";"

    return formatted_cookie, is_valid, extracted


def extract_and_format_cookies(full_cookie_string: str) -> str:
    formatted_cookie, _, _ = extract_douyin_cookies(full_cookie_string)
    return formatted_cookie
