# ComfyUI 云工作台镜像 (WORK 产品 "comfyui" 的 COMFY_IMAGE)。
#
# 以**纯编排器**模式运行: 不带 GPU, 算力全部外挂到远端模型服务。实测内存峰值
# 583MB、冷启动 8-9 秒, 与 dsh 工作台同量级 —— 详见本目录 README.md。
#
#   docker build -t ghcr.io/agentsdancepro/comfy-local:v0.34.1 .
#   docker push  ghcr.io/agentsdancepro/comfy-local:v0.34.1
#
# 生产是 ECI 后端, 实例从 registry 拉 —— 本机 tag 对它没意义, COMFY_IMAGE_REF
# 必须是完整仓库地址。推完记得重建 ECI 镜像缓存 (server/scripts/eci_image_cache.py),
# 否则冷启动要从头拉一遍这 2.5GB。
FROM --platform=linux/amd64 python:3.12-slim-bookworm

# 钉死版本: master 每次构建拿到的都不一样, 而"镜像变了就重建工作台"这套机制
# 依赖镜像身份稳定 —— 浮动 ref 会让每次构建都把所有人的工作台判成过期。
ARG COMFY_REF=v0.34.1
ARG REVISION=unknown

LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud ComfyUI workspace" \
      org.opencontainers.image.version="${COMFY_REF}" \
      org.opencontainers.image.revision="${REVISION}"

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch "${COMFY_REF}" https://github.com/comfyanonymous/ComfyUI.git

WORKDIR /opt/ComfyUI
# torch 走 CPU 专用源。默认源的 linux/amd64 wheel 是 502MB 且会再拖进整套
# nvidia-* 依赖 (~2GB) —— 编排模式一张显卡都不用, 那是纯粹的冷启动税。
# 分两步装, 否则 requirements.txt 里的 torch 会从默认源再拉一遍带 CUDA 的。
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision torchaudio \
 && pip install --no-cache-dir -r requirements.txt \
 && rm -rf /root/.cache

# 自有节点烤进镜像 —— 验证阶段是挂载进去的, 生产不能靠挂载: ECI 实例上没有
# 宿主目录可挂。节点只认我们自己的网关, 由网关去适配具体厂商。
COPY custom_nodes/dsh_cloud /opt/ComfyUI/custom_nodes/dsh_cloud

# 把 .pyc 预编译进镜像。ComfyUI 是 git clone 进来的, 793 个 .py 一个 .pyc 都没有
# (pip 装的那 15000 个反倒有), 于是**每次冷启动都在现场编译一遍**。
# 实测 (2026-08-28, 5 次取中位数): 7008ms -> 5526ms, 省 1.48 秒; 构建期只花 0.33 秒。
# 放在 COPY 自有节点之后, 好让节点自己那几个文件也一起编上。
RUN python -m compileall -q -j 4 /opt/ComfyUI

# 让 ComfyUI **自带的官方 API 节点**也能跑我们的模型 —— 启动脚本把
# --comfy-api-base 指向它。见 api_shim.py 开头的说明。
COPY api_shim.py /opt/dsh-api-shim.py

EXPOSE 8188
# 不写 CMD: 应用创建实例时自己下发启动命令 (products.py 的 _comfyui_boot),
# 那里会把 output/input 指到持久卷上。烤进 CMD 的话改一次要重发镜像。
