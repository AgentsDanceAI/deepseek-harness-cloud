# Coze Studio 的**资产镜像** —— 只装文件, 不跑进程。
#
# 上游 compose 有 17 处 bind mount (建库 SQL、atlas schema、ES 中文分词器与索引
# 模板、MinIO 图标、nginx 配置、后端 model/plugin/prompt 配置目录)。ECI 没有
# bind mount, 而这些文件合计 ~8MB —— ConfigFileVolume 走 base64 塞不下。
#
# 于是: 烤进这个镜像, 由**初始化容器**铺到组内共享的 EmptyDir 卷上, 各服务
# 容器再从那里取。上游的 mysql/es/minio/milvus/nginx 镜像一个都不用改。
FROM alpine:3.20
ARG COZE_REF=unknown
COPY assets/ /assets/
# 初始化容器的默认动作。ECI 那边会显式覆盖 Command, 这里留着是为了本机能
# `docker run` 单独验一遍铺设结果。
CMD ["sh", "-c", "cp -a /assets/. /seed/ && ls -R /seed | head -40"]

# ── 构建元数据放最后 ──────────────────────────────────────────────────────────
# **别把这两行挪回文件开头。** REVISION 的值是 git sha, 每次构建都变; BuildKit 会把
# 作用域内的 ARG 带进后面每一条 RUN 的缓存键 (层历史里能看到 `RUN |1 REVISION=...`),
# 于是 ARG 之后的所有层每次构建全部失效 —— apt、npm i -g、venv 这些几百 MB 的重活
# 层一个都复用不上。实测 (2026-09-07, 比对 registry 里相邻两版的层摘要):
# agentui 连续三版每次重下 326 MB (81%), 而真正变的只有 COPY 进去的 0.1 MB;
# comfy-local 95.8% 等于整包重拉。放到末尾之后, 前面的层跨版本全部复用。
# LABEL 不产生文件系统层, 放最后零成本。
ARG REVISION=unknown
LABEL org.opencontainers.image.title="coze-studio assets" \
      org.opencontainers.image.description="Coze Studio 容器组的种子资产 (由初始化容器铺到 EmptyDir)" \
      org.opencontainers.image.version="$COZE_REF" \
      org.opencontainers.image.revision="$REVISION"
