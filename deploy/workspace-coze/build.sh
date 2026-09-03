#!/usr/bin/env bash
# 构建并发布 Coze Studio 的资产镜像。
#
#   ./build.sh                # 取上游资产 -> 建镜像 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh    # 只建不推
#
# 两个必须一起动的版本:
#   COZE_REF     coze-studio 源码 tag。schema.sql / opencoze_latest_schema.hcl /
#                ES 索引模板 / backend/conf 都从这里取。
#   COZE_VERSION cozedev/coze-studio-{server,web} 的镜像 tag。
# **这两个错位 = 建出来的库跟二进制对不上**, 表现是 coze-server 起来后一堆
# "table doesn't exist", 而每个组件自己看都正常。所以脚本强制它们同源:
# COZE_REF 默认就是 v$COZE_VERSION, 要拆开必须显式指定。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$here"

COZE_VERSION="${COZE_VERSION:-0.5.1}"
COZE_REF="${COZE_REF:-v$COZE_VERSION}"
# atlas 社区版。上游是在 **mysql 容器启动时** `curl atlasgo.sh | sh` 现装的 ——
# 冷启动多一次外网往返, 且外网一抖就是"库建起来了但没有 schema", 而症状会
# 显示在 coze-server 那边 (一堆找不到表), 排查方向完全指错。烤进来。
ATLAS_URL="${ATLAS_URL:-https://release.ariga.io/atlas/atlas-community-linux-amd64-latest}"
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/coze-assets}"
TAG="${1:-${COZE_VERSION}-r1}"
REF="$IMAGE:$TAG"

# --- 1. 取上游资产 -----------------------------------------------------------
rm -rf assets && mkdir -p assets/bin
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo "==> 取 coze-studio @ $COZE_REF"
git clone -q --depth 1 --branch "$COZE_REF" https://github.com/coze-dev/coze-studio.git "$work/src"

d="$work/src/docker"
cp -a "$d/volumes/mysql"          assets/mysql
cp -a "$d/volumes/elasticsearch"  assets/elasticsearch
cp -a "$d/volumes/minio"          assets/minio
cp -a "$d/atlas"                  assets/atlas
cp -a "$d/nginx"                  assets/nginx
cp -a "$work/src/backend/conf"    assets/conf
# etcd.conf.yml 上游是**空文件** —— 不铺, 少一个挂载点少一处能出错的地方。

echo "==> 取 atlas CLI"
curl -fsSL -o assets/bin/atlas "$ATLAS_URL"
chmod +x assets/bin/atlas

# 资产完整性: 少一样都是"起来了但坏了", 而症状全在别的容器上。
for f in assets/mysql/schema.sql \
         assets/atlas/opencoze_latest_schema.hcl \
         assets/elasticsearch/analysis-smartcn.zip \
         assets/elasticsearch/setup_es.sh \
         assets/elasticsearch/elasticsearch.yml \
         assets/nginx/nginx.conf \
         assets/nginx/conf.d/default.conf \
         assets/bin/atlas; do
  [ -s "$f" ] || { echo "!! 资产缺失或为空: $f" >&2; exit 1; }
done
[ -d assets/elasticsearch/es_index_schema ] || { echo "!! 缺 es_index_schema" >&2; exit 1; }
[ -d assets/conf/model/template ] || { echo "!! 缺 backend/conf/model/template" >&2; exit 1; }
echo "    资产 $(du -sh assets | cut -f1)"

# nginx 的 proxy_pass 用的是**静态**主机名 (coze-server / minio), nginx 只在
# 启动时解析一次, 走 /etc/hosts —— 于是 ECI 的 HostAliase 兜得住, 配置原样可用。
# 一旦上游改成 `resolver` + 变量式 proxy_pass, HostAliase 就**不再生效**
# (nginx 的 resolver 不读 /etc/hosts, Dify 那边栽过), 必须改成写死回环。
# 对象存储直链的改写规则: 启动脚本 (products._coze_boot) 会把它 sed 成 https 版,
# 否则 https 页面里出现 http:// 的图片就是混合内容, 浏览器直接拦 —— 表现是头像和
# 附件一片空白, 服务端一切正常。sed 匹配不上就是**静默失效**, 所以在这里断言:
# 上游哪天改了这一行, 构建就红, 而不是等用户看到碎图。
# 这个字面量必须与 server/app/products.py 的 _COZE_SUBFILTER_FROM 一致。
if ! grep -qF "sub_filter 'minio:9000' '\$http_host/local_storage';" assets/nginx/conf.d/default.conf; then
  echo "!! 上游改了对象存储直链的 sub_filter 规则 —— products._COZE_SUBFILTER_FROM 要跟着改," >&2
  echo "   否则启动脚本那条 sed 静默失效, 线上头像/附件会因混合内容被浏览器拦掉。" >&2
  echo "   当前 conf 里的相关行:" >&2
  grep -n 'sub_filter' assets/nginx/conf.d/default.conf | sed 's/^/     /' >&2
  exit 1
fi

# 免登录那行 proxy_set_header 要插在 API 那个 location 里, 锚点是下面这一行
# (它在整份配置里唯一 —— /local_storage/ 用的是 `Host minio:9000`)。
# 匹配不上是**静默失效**: nginx 照常起, 用户照常看到 Coze 自己的登录墙, 而他
# 根本不知道密码 (是我们随机生成的)。这个字面量与 products._COZE_APIHOST_ANCHOR 一致。
n=$(grep -cF 'proxy_set_header Host $http_host;' assets/nginx/conf.d/default.conf)
if [ "$n" != "1" ]; then
  echo "!! 免登录的插入锚点在上游配置里出现了 $n 次 (要求恰好 1 次) —— " >&2
  echo "   products._COZE_APIHOST_ANCHOR 要跟着改, 否则会话注入静默失效, 用户又会看到登录墙。" >&2
  grep -n 'proxy_set_header Host' assets/nginx/conf.d/default.conf | sed 's/^/     /' >&2
  exit 1
fi

# 就绪探针的 location (/__dsh_ready, 见 products._coze_boot) 由 sed 锚在**行首的**
# `server {` 后面插入。上游 conf 里另有一个注释掉的 `# server {` (HTTPS 样板), 所以
# 只数行首的; 不是恰好 1 个就说明上游改了结构, 插入位置要重新看。
n=$(grep -c '^server {' assets/nginx/conf.d/default.conf)
if [ "$n" != "1" ]; then
  echo "!! 行首的 'server {' 在上游配置里出现了 $n 次 (要求恰好 1 次) —— " >&2
  echo "   products._coze_boot 里 /__dsh_ready 那条 sed 的锚点要跟着改, 否则探针要么没有、要么 nginx 起不来。" >&2
  grep -n 'server {' assets/nginx/conf.d/default.conf | sed 's/^/     /' >&2
  exit 1
fi

# 后端 API 那条 location (上游 `location ~ ^/(api|v[1-3]|admin)`): 代登录脚本要经它
# 打 coze-server (探针本身已改探 /__dsh_ready)。上游哪天改了 API 前缀, /api/ 就会落回 `location /` 那个静态首页 ——
# 而静态首页**永远回 200**, 判据 <500 当场失效: 探针在 coze-server 起来前就放人
# 进去, 用户看到的是前端自己的报错页 (2026-08-30 Dify 那个洞的翻版, coze 实测坏
# 窗口 96 秒)。这个错法完全静默, 所以在这里断言。
if ! grep -qE 'location\s+~\s+\^/\(api\|v\[1-3\]\|admin\)' assets/nginx/conf.d/default.conf; then
  echo "!! 上游改了 API 那条 location 的匹配式 —— products.registry() 里 coze 的 ready_path 要跟着改," >&2
  echo "   否则探针会落到静态首页上, 那东西永远回 200, 冷启动又会把人放进坏页面。" >&2
  grep -n 'location' assets/nginx/conf.d/default.conf | sed 's/^/     /' >&2
  exit 1
fi

if grep -qE '^\s*resolver\s' assets/nginx/nginx.conf assets/nginx/conf.d/default.conf; then
  echo "!! 上游 nginx 配置改用 resolver 了 —— HostAliase 兜不住 (见 products._dify_boot)," >&2
  echo "   要么在这里把 upstream 改写成 127.0.0.1, 要么自己生成 conf。" >&2
  exit 1
fi

# --- 2. 建镜像 ---------------------------------------------------------------
docker build \
  --build-arg COZE_REF="$COZE_REF" \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  -t "$REF" .

# --- 3. 自检: 真跑一遍铺设, 确认落到卷上的是完整的一份 ------------------------
echo "==> 镜像内自检"
docker run --rm --entrypoint sh "$REF" -c '
  mkdir -p /seed && cp -a /assets/. /seed/
  for f in /seed/mysql/schema.sql /seed/atlas/opencoze_latest_schema.hcl \
           /seed/elasticsearch/analysis-smartcn.zip /seed/nginx/conf.d/default.conf \
           /seed/conf/model/template/model_template_basic.yaml; do
    [ -s "$f" ] || { echo "!! 铺设后缺 $f" >&2; exit 1; }
  done
  /seed/bin/atlas version >/dev/null 2>&1 || { echo "!! atlas 跑不起来 (架构/依赖不对?)" >&2; exit 1; }
  echo "  ✓ 铺设完整, atlas: $(/seed/bin/atlas version 2>&1 | head -1)"'

# --- 4. 推 -------------------------------------------------------------------
if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推"
else
  docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"
fi
echo "下一步: deploy/prod/.env 设 COZE_ASSETS_IMAGE_REF=$REF, 跑 scripts/safe_deploy.sh"
