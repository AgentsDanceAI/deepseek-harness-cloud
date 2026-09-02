# CrewAI 那一格: 老板 2026-09-02 拍板换社区的 CrewAI-Studio (strnad, 1348★, MIT,
# Streamlit) 当前端 —— 它的形态正是 CrewAI 的特色: 用表单搭队伍、定角色和任务、
# 一键 kickoff 看结果, 有简体中文。之前我们自己套的聊天框"太难看"(老板原话)。
#
# 作者标了"低维护模式" (只修安全问题), 所以**钉死提交**, 升级前先 spike。
#
# ⚠️ 它 requirements.txt 把 torch/torchvision 钉成 CUDA 版: 探针实测装完 8.6GB,
# 其中 nvidia 4.3G + torch 1.7G + triton 0.6G —— 而 torch 只被 docling (解析文档做
# 知识库) 间接依赖, 这个容器里没有 GPU。换成 CPU 版 (+cpu 后缀 + CPU 索引), 其余
# 照它的钉。**构建期要当场证明换完还能起**, 见最后那行自检。
FROM python:3.12-slim
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud CrewAI workspace (CrewAI-Studio)" \
      org.opencontainers.image.revision="${REVISION}"

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      git ca-certificates build-essential curl tini \
 && rm -rf /var/lib/apt/lists/*

ARG STUDIO_COMMIT=8b123b3
RUN git clone -q https://github.com/strnad/CrewAI-Studio /opt/cs \
 && cd /opt/cs && git checkout -q "${STUDIO_COMMIT}" \
 && sed -i -E 's/^(torch==[0-9.]+)$/\1+cpu/; s/^(torchvision==[0-9.]+)$/\1+cpu/' requirements.txt \
 && grep -E '^torch(vision)?==' requirements.txt \
 && pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt \
 && python -c "import torch; assert not torch.cuda.is_available(); print('[build] torch', torch.__version__, '(CPU)')"

COPY patch_studio.py seed_demo.py /opt/dsh/
RUN python /opt/dsh/patch_studio.py \
 && ! grep -q 'ss.page = "Kickoff!"' /opt/cs/app/pg_crew_run.py \
 && cd /opt/cs && env -i PATH="$PATH" OPENAI_API_KEY=x OPENAI_API_BASE=http://x/v1 OPENAI_PROXY_MODELS=m1,m2 \
      python -c "import sys; sys.path.insert(0,'app'); import llms; ms=llms.llm_providers_and_models(); assert ms==['DSH: m1','DSH: m2'], ms; print('[build] 提供方只剩 DSH:', ms)"

# 状态落 NAS: DB_URL 指到 /root 下 (启动脚本设)。
WORKDIR /opt/cs
EXPOSE 8501
ENTRYPOINT ["/usr/bin/tini","--"]
