#!/usr/bin/env bash
# 构建并发布 CrewAI 那一格的镜像 (CrewAI-Studio)。  ./build.sh [tag]   SKIP_PUSH=1 只建不推
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/../.." && pwd)"
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/crewai-studio}"; TAG="${1:-8b123b3-r1}"; REF="$IMAGE:$TAG"
docker build --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" -t "$REF" "$here"
echo "==> 镜像内自检"
docker run --rm -u 0 --entrypoint bash "$REF" -c '
  set -e
  echo "  · 镜像里 site-packages: $(du -sh /usr/local/lib/python3.12/site-packages | cut -f1) (探针的 CUDA 版是 8.6G)"
  mkdir -p /root/crewai-studio
  export DB_URL=sqlite:////root/crewai-studio/crewai.db DEFAULT_LANGUAGE=zh OPENAI_API_KEY=x OPENAI_API_BASE=http://127.0.0.1:1/v1 OPENAI_PROXY_MODELS=gpt-5.6-luna DSH_MODEL=gpt-5.6-luna AGENTOPS_ENABLED=False USER_AGENT=dsh-cloud
  python /opt/dsh/seed_demo.py 2>/dev/null | tail -1
  python /opt/dsh/seed_demo.py 2>/dev/null | tail -1   # 第二次不该再种
  cd /opt/cs && nohup streamlit run app/app.py --server.port 18501 --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false --client.toolbarMode minimal >/tmp/st.log 2>&1 &
  for i in $(seq 1 120); do curl -fsS -o /dev/null http://127.0.0.1:18501/_stcore/health 2>/dev/null && break; sleep 1; done
  test "$(curl -s http://127.0.0.1:18501/_stcore/health)" = "ok" || { echo "!! /_stcore/health 不是 ok" >&2; tail -20 /tmp/st.log >&2; exit 1; }
  echo "  ✓ Streamlit 起来了 ($i 秒), /_stcore/health = ok"
  for p in / /_stcore/health; do code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:18501$p"); case "$code" in 401|403) echo "!! $p 返回 $code —— 冒出了登录墙" >&2; exit 1;; esac; done
  echo "  ✓ 无登录墙"
'
if [ "${SKIP_PUSH:-0}" = "1" ]; then echo "==> SKIP_PUSH=1, 不推"; else docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"; fi
echo "下一步: deploy/prod/.env 设 CREWAI_STUDIO_IMAGE_REF=$REF, 建 ECI 缓存, safe_deploy"
