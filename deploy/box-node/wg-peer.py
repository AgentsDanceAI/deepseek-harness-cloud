#!/usr/bin/env python3
"""wg 配置里按**名字**增删对端 —— 一个接口挂多个盒子时用。

  dsh-wg-peer.py <conf> set <名字> <公钥> <AllowedIPs>
  dsh-wg-peer.py <conf> rm  <名字>
  dsh-wg-peer.py <conf> ls

为什么要按名字而不是按公钥: 盒子重装会换公钥, 而"这台是谁的"不变。名字写成
`# dsh-peer:<名字>` 注释钉在 [Peer] 块里。

⚠️ 老配置里的对端没有这行注释 —— 第一次跑时把它认成 standby 补上标记。不补的话按名字
删会漏掉它, 结果是加出第二个对端, 而 `wg show` 不会告诉你哪个才是活的 (两个都列着,
只有握手时间能看出端倪)。
"""
from __future__ import annotations

import re
import sys

MARK = "# dsh-peer:"


def blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
    """拆成 (Interface 段, [(名字, 整块文本), ...])。"""
    parts = re.split(r"(?m)^\[Peer\]\s*$", text)
    head, peers = parts[0], []
    for body in parts[1:]:
        m = re.search(r"(?m)^%s(\S+)\s*$" % re.escape(MARK), body)
        peers.append((m.group(1) if m else "standby", body))  # 没标记的当 standby
    return head, peers


def render(head: str, peers: list[tuple[str, str]]) -> str:
    out = head.rstrip("\n") + "\n"
    for name, body in peers:
        body = body.strip("\n")
        if MARK not in body:
            body = f"{MARK}{name}\n" + body
        out += "\n[Peer]\n" + body + "\n"
    return out


def main() -> int:
    conf, op = sys.argv[1], sys.argv[2]
    text = open(conf).read()
    head, peers = blocks(text)

    if op == "ls":
        if not peers:
            print("  (没有对端)")
        for name, body in peers:
            m = re.search(r"(?m)^AllowedIPs\s*=\s*(.+)$", body)
            print("  %-16s %s" % (name, m.group(1).strip() if m else "?"))
        return 0

    name = sys.argv[3]
    peers = [(n, b) for n, b in peers if n != name]          # 同名先删, 保证幂等
    if op == "set":
        pub, allowed = sys.argv[4], sys.argv[5]
        peers.append((name, f"{MARK}{name}\nPublicKey = {pub}\nAllowedIPs = {allowed}\n"))
    elif op == "rm":
        print("  已删除" if len(peers) < len(blocks(text)[1]) else "  没找到叫这个名字的对端")
    else:
        print(f"未知操作 {op}", file=sys.stderr)
        return 2

    open(conf, "w").write(render(head, peers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
