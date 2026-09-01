# AutoGen Studio —— 微软的多智能体搭建台。上游没有官方镜像, 这是我们打的。
#
# 它本身**没有登录墙** (进去就是 Guest User 的 Playground), 唯一要动的是模型:
# 默认图库里写的是 gpt-4o / claude-3-7 这些厂商自己的名字, 而我们的网关只放行
# 在售目录里的型号 —— 不改就是每次 404。图库在首次启动时按用户建进 SQLite,
# 所以改法是启动脚本里灌 (见 products._autogen_boot), 不是烤进镜像。
FROM python:3.12-slim

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir autogenstudio

# 把写死的模型名换成读环境变量 (见 patch_models.py 的说明)。**构建期就自检**:
# 上游一旦改了那几行常量, 这里当场构建失败 —— 而不是等上线后每次对话 404。
COPY patch_models.py /tmp/patch_models.py
RUN python /tmp/patch_models.py && rm /tmp/patch_models.py

# 状态落 /data —— NAS 挂在这里, 闲置回收把容器删掉后用户的队伍和会话还在。
ENV AUTOGENSTUDIO_APPDIR=/data
EXPOSE 8081
CMD ["autogenstudio","ui","--host","0.0.0.0","--port","8081","--appdir","/data"]
