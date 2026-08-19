"""测试用的建号/登录辅助 —— 与生产走同一条验证码路径。

2026-08-18 之前测试都打 /api/auth/register。那条路由不验证邮箱就建号并当场发放
免费额度, 已作为薅羊毛敞口移除 (实测一条 curl 用 @example.com 假邮箱就能建号拿
500 积分 + 180 分钟机时)。

设计要点:
  · 首次: /email/send 拿验证码 → 直接把库里的 code_hash 改成已知值 → /email/login。
    验证码是单向哈希存的, 取不回原文; 测试环境也没有 SMTP (生产代码在 DEV_MODE
    下只把邮件打到 stdout), 从库里改比解析 stdout 稳。
  · 重复调用: **跳过发码**。/email/send 有"每邮箱每天 10 次"的限流, 而 fixture
    往往每个用例都调一次, 同一邮箱很快撞上限 (实测报 send code failed)。用户已
    存在时直接写一条验证码记录再登录, 不碰限流。
"""
import time

from app import db, security

_CODE = "123456"


def signup_with_password(client, email: str, password: str = "password123") -> None:
    """建号并给它设上密码 —— 供专门测试密码登录 / 改密吊销的用例使用。

    验证码注册的号没有 password_hash。/api/auth/password 在 password_hash 为空时
    不校验旧密码 (见 accounts.change_password), 所以建完号直接设一次即可。
    ⚠️ 设密码会 session_epoch+1 并吊销该用户全部设备, 所以必须在建号后、
    签发设备令牌之前调用。
    """
    signup(client, email)
    r = client.post("/api/auth/password", json={"old": "", "new": password})
    assert r.status_code == 200, f"set password failed: {r.status_code} {r.text}"
    # 改密吊销了会话, 重新登录拿新 epoch 的 cookie
    signup(client, email)


def signup(client, email: str) -> None:
    """建号(或登录)并让 client 持有会话 cookie。对同一邮箱可重复调用。"""
    # 完全不调 /email/send: 那条接口有三层限流 (每 IP 5/10分钟、每邮箱 10/天、
    # 全局 500/天), 跨测试文件累积会让靠后的用例莫名其妙地失败, 而发码本身
    # 不是被测对象 —— 被测的是 /email/login 的校验与建号逻辑。直接落一条已知
    # 验证码, 走与真实用户完全相同的 /email/login 校验路径。
    db.query("DELETE FROM email_codes WHERE email=? AND purpose=?", (email, "login"))
    now = time.time()
    db.query("INSERT INTO email_codes (email, code_hash, purpose, expires, created) "
             "VALUES (?,?,?,?,?)",
             (email, security.token_hash(_CODE), "login", now + 600, now))
    r = client.post("/api/auth/email/login", json={"email": email, "code": _CODE})
    assert r.status_code == 200, f"code login failed: {r.status_code} {r.text}"
