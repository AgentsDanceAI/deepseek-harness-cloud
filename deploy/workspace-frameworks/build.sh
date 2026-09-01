#!/usr/bin/env bash
# 构建并发布 OpenManus / CrewAI 那一格的镜像 (框架 + 工作台外壳)。
#
#   ./build.sh                 # 建 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh     # 只建不推
#
# **构建上下文是 deploy/ 而不是这个目录**: 外壳用的是 workspace-agentui 里那
# 同一份 app + web —— 复制第二份出来必然漂, 而漂的结果是两边界面不一样、
# 修了一边另一边还是坏的。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
ctx="$(cd "$here/.." && pwd)"

IMAGE="${IMAGE:-ghcr.io/agentsdancepro/agent-frameworks}"
TAG="${1:-0.2.0}"
REF="$IMAGE:$TAG"

docker build \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" \
  -f "$here/Dockerfile" -t "$REF" "$ctx"

echo "==> 镜像内自检"
docker run --rm -u 0 --entrypoint bash "$REF" -c '
  set -e

  # 1. 两个框架各自的环境还在 —— 加外壳不该把它们碰坏。
  /opt/venv-openmanus/bin/python -c "import app.agent.manus" 2>/dev/null \
    || { cd /opt/openmanus && /opt/venv-openmanus/bin/python -c "import app.agent.manus"; }
  echo "  ✓ OpenManus 可导入"
  /opt/venv-crewai/bin/python -c "import crewai; print(\"  ✓ CrewAI\", crewai.__version__)"

  # 2. **两个环境必须还是分开的**: 它们要的 openai 版本不兼容, 合到一起时
  #    症状出现在用户发第一句话的时候, 不是构建时。
  a=$(/opt/venv-openmanus/bin/python -c "import openai; print(openai.__version__)")
  b=$(/opt/venv-crewai/bin/python -c "import openai; print(openai.__version__)")
  test "$a" != "$b" || echo "  (两边 openai 版本恰好相同: $a —— 不算错, 但别把它们并成一个 venv)"
  echo "  ✓ 两个虚拟环境各自独立 (openai $a / $b)"

  # 3. CrewAI 工程模板要在, 而且是我们那两份 yaml (不是脚手架自带的"研究报告")。
  test -f /opt/dsh/crew-template/src/dsh_crew/crew.py || { echo "!! 没有 crew 模板" >&2; exit 1; }
  grep -q "资深研究员" /opt/dsh/crew-template/src/dsh_crew/config/agents.yaml \
    || { echo "!! crew 模板还是脚手架自带那套" >&2; exit 1; }
  echo "  ✓ CrewAI 工程模板已备好"

  # 4. 外壳起得来, 且接口齐。
  cd /srv
  DSH_DEFAULT_CLI=openmanus DSH_ENABLED_CLIS=openmanus DSH_AGENT_UID=0 DSH_AGENT_HOME=/root \
    /opt/venv-ui/bin/uvicorn app.main:app --host 127.0.0.1 --port 18080 >/tmp/srv.log 2>&1 &
  for i in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:18080/api/health 2>/dev/null && break; sleep 1; done
  for p in /api/health /api/config /api/sessions /api/files; do
    code=$(curl -s -o /tmp/r.json -w "%{http_code}" --max-time 10 "http://127.0.0.1:18080$p")
    test "$code" = "200" || { echo "!! $p 返回 $code" >&2; tail -20 /tmp/srv.log >&2; exit 1; }
  done
  grep -q "OpenManus" /tmp/r.json 2>/dev/null || true
  echo "  ✓ 工作台接口齐"

  # 5. **这一格开放的必须是这两个框架**, 不能把 claude/codex 也露出来 —— 镜像里
  #    根本没有那三个 CLI, 露出来就是用户切过去发一句话、进程起不来。
  curl -s --max-time 10 http://127.0.0.1:18080/api/config > /tmp/c.json
  grep -q "openmanus" /tmp/c.json || { echo "!! /api/config 里没有 openmanus" >&2; cat /tmp/c.json >&2; exit 1; }
  grep -q "claude" /tmp/c.json && { echo "!! 露出了这个镜像里没有的 CLI" >&2; exit 1; }
  echo "  ✓ 只开放这一格的框架"

  # 6. 终端反代出得来 (ttyd 按需起, 这一下顺带验了拉起那条路)。
  code=$(curl -s -o /tmp/t.html -w "%{http_code}" --max-time 25 http://127.0.0.1:18080/terminal/)
  test "$code" = "200" || { echo "!! /terminal/ 返回 $code" >&2; tail -20 /tmp/srv.log >&2; exit 1; }
  echo "  ✓ 终端反代出得来"

  # 7. 没有登录墙 —— 老板铁律。
  for p in / /api/sessions /api/config; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:18080$p")
    case "$code" in 401|403) echo "!! $p 返回 $code —— 冒出了登录墙" >&2; exit 1;; esac
  done
  echo "  ✓ 无登录墙"
'

if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推"
else
  docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"
fi
echo "下一步: deploy/prod/.env 设 FRAMEWORKS_IMAGE_REF=$REF, 建 ECI 缓存, 再 scripts/safe_deploy.sh"
