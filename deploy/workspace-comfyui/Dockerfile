# ComfyUI 纯编排模式 (无 GPU) 的可行性验证镜像。
#
# 要点: 装 CPU 专用的 torch wheel。默认源的 linux/amd64 wheel 会把整套 CUDA
# 运行时 (~2.5GB) 一起拖进来 —— 而编排模式一张显卡都不用, 那是纯粹的冷启动税。
FROM --platform=linux/amd64 python:3.12-slim-bookworm

ARG COMFY_REF=master

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch "${COMFY_REF}" https://github.com/comfyanonymous/ComfyUI.git

WORKDIR /opt/ComfyUI
# torch 走 CPU 源, 其余依赖走 PyPI。分两步装, 否则 requirements.txt 里的 torch
# 会从默认源再拉一遍带 CUDA 的。
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision torchaudio \
 && pip install --no-cache-dir -r requirements.txt \
 && rm -rf /root/.cache

EXPOSE 8188
CMD ["python", "main.py", "--cpu", "--listen", "0.0.0.0", "--port", "8188"]
