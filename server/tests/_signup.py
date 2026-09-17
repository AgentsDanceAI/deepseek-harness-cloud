"""测试用的建号/登录辅助，覆盖与应用相同的验证码登录路径。

验证码只存单向哈希，测试环境也不依赖 SMTP。因此辅助函数写入一条已知验证码，
再调用 /email/login；这样既验证真实校验逻辑，也不会让测试套件消耗发码限额。
"""

import time

from app import db, security

_CODE = "123456"


def _put_code(email: str) -> None:
    """写入一条已知验证码。发码限流不是这些用例的测试对象, 但校验与建号走的仍是
    真实路径。"""
    db.query("DELETE FROM email_codes WHERE email=? AND purpose=?", (email, "login"))
    now = time.time()
    db.query(
        "INSERT INTO email_codes (email, code_hash, purpose, expires, created) VALUES (?,?,?,?,?)",
        (email, security.token_hash(_CODE), "login", now + 600, now),
    )


def signup_with_password(client, email: str, password: str = "password123") -> None:
    """建号并给它设上密码 —— 供专门测试密码登录 / 改密吊销的用例使用。

    验证码注册的账号没有 password_hash，首次设置密码允许空旧密码。设置密码会
    递增 session_epoch 并吊销已有设备，因此必须在签发测试设备令牌之前调用。
    """
    signup(client, email)
    # 2026-09-16 起, 无密码账号首次设密码要一封现拿的邮箱验证码 (只凭会话就能设上
    # 长期密码的话, 一个泄漏的会话等于一条永久入口)。这里照真实路径写一条码再用掉。
    _put_code(email)
    r = client.post("/api/auth/password", json={"old": "", "new": password, "code": _CODE})
    assert r.status_code == 200, f"set password failed: {r.status_code} {r.text}"
    # 改密吊销了会话, 重新登录拿新 epoch 的 cookie
    signup(client, email)


def signup(client, email: str) -> None:
    """建号(或登录)并让 client 持有会话 cookie。对同一邮箱可重复调用。"""
    # 发码限流不是这些用例的测试对象。写入已知验证码后仍通过真实
    # /email/login 校验与建号路径，避免跨测试共享限流状态。
    _put_code(email)
    r = client.post("/api/auth/email/login", json={"email": email, "code": _CODE})
    assert r.status_code == 200, f"code login failed: {r.status_code} {r.text}"
