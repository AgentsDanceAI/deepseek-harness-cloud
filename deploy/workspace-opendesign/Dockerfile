# Open Design 云工作台镜像 (云空间 "open-design" 坑位)。
#
# 上游官方镜像 (ghcr.io/nexu-io/od) 是给**本机**用的: 它只带 daemon+web, 一个
# agent CLI 都不带 —— 27 个 agent 全部 not-on-path, 托管环境里 UI 能开但干不了
# 任何活。而它的支持列表里有 `deepseek-harness (dsh)`, 我们自己的智能体。
#
# 所以衍生镜像只做一件事: 把 dsh CLI 烤进去。dsh 是 npm 包 (node 脚本), od 镜像
# 自带 node 24 —— 直接从 dsh-local 镜像 COPY 包目录, 版本与 dsh 工作台同源,
# 不另拉 npm。
#
#   构建走 ./build.sh —— DSH_IMAGE 从 deploy/prod/.env 的 WORK_IMAGE_REF 取,
#   与 dsh 工作台跑**同一个版本** (版本号只在一处, 有守卫测试盯着不许写死)。
#
# ⚠️ 上游 od 版本浮动很快 (README 说 0.10, latest 已是 0.21)。OD_VERSION 必须
# 钉死 —— "镜像变了就重建工作台"依赖镜像身份稳定。升级时先本机 spike 再换。
ARG OD_VERSION=0.21.0
ARG DSH_IMAGE

FROM ${DSH_IMAGE} AS dsh

FROM ghcr.io/nexu-io/od:${OD_VERSION}
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud Open Design workspace" \
      org.opencontainers.image.revision="${REVISION}"

USER root
COPY --from=dsh /usr/local/lib/node_modules/@deepseek-ai/dsh /usr/local/lib/node_modules/@deepseek-ai/dsh
# dsh 不是软链而是**包装脚本**, 就为了带上 `node --expose-internals`。
# 理由与下面 sharp 那条同源, 都是 musl:
#   dsh 启动 profile 时, 若上下文里没有 hmr 服务就自己注入一个
#   (profile-boot: `if (ctx.get("hmr") === void 0) loader.create(...hmr)`),
#   而 hmr 要拿到 Node 内部模块 —— 两条路: process.execArgv 里有
#   --expose-internals, 或者原生包 node-addon-require-builtin。后者**根本没发
#   musl 版**(optionalDependencies 里只有 gnu/darwin/win), 于是 Alpine 上两条
#   路都断, profile 启动即崩:
#     failed to apply loader entry (@deepseek-ai/cordis-plugin-hmr):
#     --expose-internals is required for HMR service
#   而 NODE_OPTIONS 里放不了这个旗标 (node 明确拒绝), 所以只能落在 bin 上。
#   用户侧的表现是 DSH_PROFILE_MISSING_RESULT —— "跑一半就结束, 不给终态"。
#   2026-08-30 老板在 Open Design 里点"写种"撞到。
# 带引号的 heredoc 定界符 —— Docker 不对内容做变量展开, 于是 `"$@"` 原样落地。
# (printf 那种写法要给 $ 转义, 而转义出来的反斜杠会一起写进文件, 包装脚本当场坏掉。)
RUN <<'MKDSH'
set -e
cat > /usr/local/bin/dsh <<'SH'
#!/bin/sh
exec node --expose-internals /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js "$@"
SH
chmod +x /usr/local/bin/dsh /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js
MKDSH

RUN dsh --version \
 # dsh plugin add 用 pnpm 装 profile 插件 —— 运行时装 (profile 落在 NAS 的
 # /root 上, 按用户持久化), 所以 pnpm 必须在镜像里
 && npm install -g pnpm@10 >/dev/null 2>&1 && pnpm --version \
 # dsh 的包树是从 Debian(glibc) 的 dsh-local 拷来的, 而 od 底是 Alpine(musl):
 # sharp 这类原生模块的二进制不兼容, 会让整棵插件树 boot 失败 (probe 当场炸)。
 # 在 dsh 自己的目录里补装 musl 版本的 sharp 二进制。
 && cd /usr/local/lib/node_modules/@deepseek-ai/dsh \
 && npm install --no-save --force --os=linux --libc=musl --cpu=x64 sharp 2>&1 | tail -1

# OpenDesign 的 dsh profile bundle。上游没发 npm, 从他们 monorepo 构建打包
# (packages/dsh-runtime, 见 deploy/README)。启动脚本在首次进入时装进用户的
# DSH_HOME —— 烤在 /root 会被 NAS 挂载盖掉, 所以只带 tgz, 装在运行时。
COPY open-design-dsh-runtime.tgz /opt/od-profile.tgz

# 保持 root: 数据目录软链到 NAS 卷 (/workspace), 而 NAS 子路径由 ECI 以 root 建。
# 上游镜像的 open-design 用户写不进去; 我们的每用户容器本来就是隔离沙箱, dsh
# 工作台同样以 root 跑。
EXPOSE 7456
# 不写 CMD: 启动命令由 products.py 的 boot 下发 (软链数据目录后 exec daemon)。
