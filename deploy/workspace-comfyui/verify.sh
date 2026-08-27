#!/usr/bin/env bash
# ComfyUI 编排模式验证: 无 GPU 容器里跑通一条真 workflow, 并量出内存峰值。
set -uo pipefail

# 按脚本自身定位, 不按 cwd —— 2026-08-27 实测: 从别的目录跑会挂载到不存在的
# 路径, 于是节点静默不加载, 而报错只说"节点没加载", 根因完全看不出来。
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME=comfy-spike-run
MEM="${MEM:-2g}"
CPUS="${CPUS:-1.0}"

docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "=== 起容器 (mem=$MEM cpus=$CPUS, 无 GPU) ==="
docker run -d --name "$NAME" \
  --platform linux/amd64 \
  --memory "$MEM" --cpus "$CPUS" \
  -p 8188:8188 \
  -e DSH_CLOUD_VIDEO_BASE="http://host.docker.internal:9797" \
  -e DSH_CLOUD_TOKEN="spike-token-not-a-real-secret" \
  -e DSH_CLOUD_VIDEO_POLL_S=1 \
  -v "$HERE/custom_nodes/dsh_cloud:/opt/ComfyUI/custom_nodes/dsh_cloud:ro" \
  comfy-orchestrator:spike >/dev/null || { echo "启动失败"; exit 1; }

echo "=== 等 ComfyUI 起来 ==="
boot_start=$(date +%s)
for i in $(seq 1 120); do
  if curl -sf http://localhost:8188/object_info >/dev/null 2>&1; then
    boot_end=$(date +%s)
    echo "✓ 冷启动 $((boot_end - boot_start)) 秒"
    break
  fi
  if [ "$i" = 120 ]; then echo "✗ 120 秒没起来"; docker logs "$NAME" | tail -30; exit 1; fi
  sleep 1
done

echo "=== 节点注册了吗 ==="
if curl -sf http://localhost:8188/object_info | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if {"DSHCloudVideo","DSHCloudImage"} <= set(d) else 1)'; then
  echo "✓ DSHCloudVideo / DSHCloudImage 已注册"
else
  echo "✗ 节点没加载"; docker logs "$NAME" 2>&1 | grep -i -A5 "dsh_cloud\|error" | tail -30; exit 1
fi

echo "=== 提交 workflow ==="
CID=$(python3 -c 'import uuid;print(uuid.uuid4().hex)')
RESP=$(curl -sf -X POST http://localhost:8188/prompt -H 'Content-Type: application/json' -d "{
  \"client_id\": \"$CID\",
  \"prompt\": {\"1\": {\"class_type\": \"DSHCloudVideo\", \"inputs\": {
      \"prompt\": \"一只猫在雪地里奔跑\", \"model\": \"cogvideox-3\",
      \"size\": \"1920x1080\", \"duration\": 5, \"fps\": 30}}}
}")
PID=$(printf '%s' "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("prompt_id",""))')
[ -n "$PID" ] || { echo "✗ 提交失败: $RESP"; exit 1; }
echo "✓ prompt_id=$PID"

echo "=== 等执行完成 ==="
for i in $(seq 1 60); do
  H=$(curl -sf "http://localhost:8188/history/$PID")
  if printf '%s' "$H" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d else 1)' 2>/dev/null; then
    echo "$H" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for pid, rec in d.items():
    st = rec.get("status", {})
    print("  状态:", st.get("status_str"), "完成:", st.get("completed"))
    for nid, out in rec.get("outputs", {}).items():
        print("  输出:", json.dumps(out, ensure_ascii=False))
    for m in st.get("messages", [])[-3:]:
        print("  消息:", json.dumps(m, ensure_ascii=False)[:300])
'
    break
  fi
  [ "$i" = 60 ] && { echo "✗ 执行超时"; docker logs "$NAME" 2>&1 | tail -30; exit 1; }
  sleep 1
done

echo "=== MP4 真的落地了吗 ==="
docker exec "$NAME" sh -c 'ls -l /opt/ComfyUI/output/*.mp4 2>/dev/null && for f in /opt/ComfyUI/output/*.mp4; do head -c 12 "$f" | od -c | head -1; done' \
  || { echo "✗ 没有 mp4"; exit 1; }

echo "=== 内存峰值 ==="
docker exec "$NAME" sh -c 'cat /sys/fs/cgroup/memory.peak 2>/dev/null || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null' \
  | awk '{printf "  峰值 %.0f MB\n", $1/1024/1024}'
docker stats --no-stream --format '  当前 {{.MemUsage}}  CPU {{.CPUPerc}}' "$NAME"

echo "=== 镜像大小 ==="
docker images comfy-orchestrator:spike --format '  {{.Size}}'

echo "=== 生图链路: DSHCloudImage -> SaveImage ==="
# 生图节点返回 IMAGE 而非输出节点, 所以必须接一个原生输出节点才会被执行 ——
# 这一步同时验证了它确实是个能和 ComfyUI 原生节点串起来的一等节点, 而不是
# 只会往磁盘写文件的死胡同。
CID2=$(python3 -c 'import uuid;print(uuid.uuid4().hex)')
RESP2=$(curl -sf -X POST http://localhost:8188/prompt -H 'Content-Type: application/json' -d "{
  \"client_id\": \"$CID2\",
  \"prompt\": {
    \"1\": {\"class_type\": \"DSHCloudImage\", \"inputs\": {
        \"prompt\": \"一只柴犬\", \"model\": \"gpt-image-2\",
        \"size\": \"1024x1024\", \"n\": 1}},
    \"2\": {\"class_type\": \"SaveImage\", \"inputs\": {
        \"images\": [\"1\", 0], \"filename_prefix\": \"dshcloud\"}}
  }
}")
PID2=$(printf '%s' "$RESP2" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("prompt_id",""))')
[ -n "$PID2" ] || { echo "✗ 生图提交失败: $RESP2"; exit 1; }

for i in $(seq 1 60); do
  H2=$(curl -sf "http://localhost:8188/history/$PID2")
  if printf '%s' "$H2" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin) else 1)' 2>/dev/null; then
    echo "$H2" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for _, rec in d.items():
    st=rec.get("status",{})
    print("  状态:", st.get("status_str"), "完成:", st.get("completed"))
    for nid,out in rec.get("outputs",{}).items():
        for img in out.get("images",[]):
            print("  出图:", img.get("filename"), img.get("type"))
    if st.get("status_str") != "success":
        for m in st.get("messages",[])[-3:]:
            print("  消息:", json.dumps(m, ensure_ascii=False)[:300])
        sys.exit(1)
'
    break
  fi
  [ "$i" = 60 ] && { echo "✗ 生图执行超时"; docker logs "$NAME" 2>&1 | tail -20; exit 1; }
  sleep 1
done

echo "=== 图片真的落地了吗 ==="
docker exec "$NAME" sh -c 'ls -l /opt/ComfyUI/output/*.png 2>/dev/null | head -3' \
  || { echo "✗ 没有 png"; exit 1; }
