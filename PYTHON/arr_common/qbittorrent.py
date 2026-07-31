import json
import urllib.parse
import urllib.request

from . import config


def login(timeout=30):
    data = urllib.parse.urlencode({"username": config.QBIT_USER, "password": config.QBIT_PASS}).encode()
    req = urllib.request.Request(f"{config.QBIT_BASE}/api/v2/auth/login", data=data, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.headers.get("Set-Cookie", "").split(";")[0]


def get(path, cookie, timeout=30):
    req = urllib.request.Request(f"{config.QBIT_BASE}{path}", headers={"Cookie": cookie})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def post_form(path, cookie, data_dict, timeout=30):
    body = urllib.parse.urlencode(data_dict).encode()
    req = urllib.request.Request(
        f"{config.QBIT_BASE}{path}", data=body, method="POST", headers={"Cookie": cookie},
    )
    return urllib.request.urlopen(req, timeout=timeout)
