#!/usr/bin/env bash
# 构建并发布 OpenMausBot 那一格的镜像。  ./build.sh [tag]   SKIP_PUSH=1 只建不推
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/../.." && pwd)"
OMB_COMMIT="${OMB_COMMIT:-acf88c4}"
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/openmausbot}"; TAG="${1:-${OMB_COMMIT}-r1}"; REF="$IMAGE:$TAG"
docker build --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
             --build-arg OMB_COMMIT="$OMB_COMMIT" -t "$REF" "$here"

echo "==> 镜像内自检"
docker run --rm --entrypoint bash "$REF" -c '
  set -e
  H=maus.example.com
  mkdir -p /root/.openmausbot
  node /app/dist-server/index.js >/tmp/omb.log 2>&1 &
  for i in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:8799/api/health 2>/dev/null && break; sleep 1; done
  curl -fsS -o /dev/null http://127.0.0.1:8799/api/health || { echo "!! 服务端没起来" >&2; tail -20 /tmp/omb.log >&2; exit 1; }
  echo "  ✓ 服务端起来了 (${i} 秒), /api/health 应答"

  # 配对墙必须**真的存在** —— 这是我们要拆的东西, 拆之前先证明它在。
  # 只有 Host 是公网域名且带代理头时才触发 (见 Dockerfile 顶部注释)。
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: $H" -H "X-Forwarded-Proto: https" \
         -H "X-Forwarded-For: 203.0.113.9" http://127.0.0.1:8799/api/auth/session)
  [ "$code" = "403" ] || { echo "!! 经代理未配对时应是 403, 实得 $code —— 上游改了鉴权模型, 外壳的假设要重看" >&2; exit 1; }
  echo "  ✓ 经代理未配对 = 403 (配对墙在)"

  # 工作台代持: 回环建码 -> 换会话 -> 拿到 Set-Cookie
  C=$(curl -fsS -X POST -H "content-type: application/json" \
        -d "{\"label\":\"DSH Cloud\",\"scopes\":[\"admin\",\"client\"]}" \
        http://127.0.0.1:8799/api/auth/pairing | node -e "let s=\"\";process.stdin.on(\"data\",d=>s+=d).on(\"end\",()=>console.log(JSON.parse(s).code))")
  [ -n "$C" ] || { echo "!! 没拿到配对码" >&2; exit 1; }
  CK=$(curl -fsS -i -X POST -H "content-type: application/json" \
        -d "{\"code\":\"$C\",\"label\":\"DSH Cloud\",\"cookie\":true}" \
        http://127.0.0.1:8799/api/auth/pair | grep -i "^set-cookie:" | sed "s/^[Ss]et-[Cc]ookie: //; s/;.*//" | tr -d "\r")
  case "$CK" in omb_session_*) ;; *) echo "!! 会话 cookie 形状不对: ${CK:0:40}" >&2; exit 1;; esac
  echo "  ✓ 回环配对拿到会话 (${CK%%=*})"

  # 注入之后, 同一条代理路径必须放行, 且拿到 admin + client 全量
  body=$(curl -s -H "Host: $H" -H "X-Forwarded-Proto: https" -H "X-Forwarded-For: 203.0.113.9" \
         -H "Origin: https://$H" -H "Cookie: $CK" http://127.0.0.1:8799/api/auth/session)
  echo "$body" | grep -q "\"kind\":\"session\"" || { echo "!! 注入会话后仍不放行: $body" >&2; exit 1; }
  echo "$body" | grep -q "admin" || { echo "!! 会话没有 admin 权限: $body" >&2; exit 1; }
  echo "  ✓ 注入会话后经代理放行, 权限 admin+client —— 无登录墙"

  claude --version >/dev/null && codex --version >/dev/null
  echo "  ✓ 引擎 CLI: $(claude --version), $(codex --version)"
'
if [ "${SKIP_PUSH:-0}" = "1" ]; then echo "==> SKIP_PUSH=1, 不推"; else docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"; fi
echo "下一步: deploy/prod/.env 设 OPENMAUSBOT_IMAGE_REF=$REF 与 OPENMAUSBOT_DOMAIN, safe_deploy"
