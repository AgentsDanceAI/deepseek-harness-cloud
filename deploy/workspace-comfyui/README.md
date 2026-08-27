# ComfyUI 编排模式工作台 (验证产物, 尚未上线)

2026-08-27 的可行性验证。**结论: 假设成立** —— ComfyUI 可以在无 GPU 的 CPU
容器里当纯编排器用, 算力全部外挂到远端模型服务, 且**不需要新开资源规格**。

背景: 生图/生视频通常被认为必须自备 GPU, 这让它在按分钟计费的多租户工作台里
成本结构完全不同。这份验证要回答的是: ComfyUI 能不能只做**编排**, 把算力全部
外挂到远端模型服务 —— 如果可以, 这类工作台就能跑在与普通 CPU 工作台相同的规格上。

## 实测数据 (amd64 镜像, 在 arm Mac 上模拟运行)

| 指标 | 值 | 备注 |
|---|---|---|
| 内存峰值 | **583 MB** | 512MB 上限也能跑通 (顶到上限靠回收页缓存, 无 OOM) |
| 冷启动 | 8–9 秒 | 模拟 x86 下的数, 真机只会更快 |
| 镜像 | 2.58 GB | 对比 dsh 的 1.08GB —— 唯一明显的代价 |
| Web UI | HTTP 200 | 前端正常, `/preview/<port>/` 反代可直接吃 |

**关键结论: 现有的 `WORK_MEM_LIMIT_MB=512` / `WORK_CPUS=1.0` 就够。**
建议给到 768MB 留余量, 但不是必须 —— 成本结构不用动。

## torch 必须走 CPU 专用源

`Dockerfile` 里那条 `--index-url https://download.pytorch.org/whl/cpu` 不是洁癖:

| 源 | amd64 torch wheel |
|---|---|
| CPU 专用源 | **175 MB** |
| PyPI 默认源 | 502 MB (还会另外拖进 `nvidia-*` 依赖, 又两个 GB) |

编排模式一张显卡都不用, 那套 CUDA 运行时是纯粹的冷启动税。

## 坑: 节点必须标 `OUTPUT_NODE = True`

ComfyUI **只执行通向输出节点的分支**。不标这个的话整张图被当成死枝跳过 ——
`/prompt` 返回 200、`/history` 里空空如也、日志一个字没有。这种失败模式极难
排查, 改动 `custom_nodes/dsh_cloud/` 时别把它删了。

## 节点为什么不直连厂商

`custom_nodes/dsh_cloud/` 只对着 DSH Cloud 自己的网关说话 (与 `/llm/v1` 同构),
由网关去适配具体厂商 —— 自部署方接谁, 节点无需知道。两个理由:

1. **计量与配额**: 用量必须由部署方的网关统一计量与限额, 节点直连厂商就绕过了
   这一层, 容器里那把凭据也会直接暴露给用户的工作区
2. **换供应商不用重发镜像**: 适配器在服务端, 镜像里那份代码不用动

契约 (异步作业 —— 主流视频厂商基本都是这个形状):

    POST {base}/videos/generations  -> {"id": ..., "task_status": "PROCESSING"}
    GET  {base}/videos/result/{id}  -> {"task_status": "SUCCESS",
                                        "video_result": [{"url": ...}]}

## 怎么跑验证

    docker build -t comfy-orchestrator:spike .
    python3 stub_gateway.py &                    # 顶替网关, 不花钱
    MEM=768m ./verify.sh

`verify.sh` 会走完: 起容器 → 等 ComfyUI → 确认节点注册 → 提交 workflow →
等执行 → 确认 MP4 真的落地 → 量内存峰值。桩刻意让前两次轮询返回 PROCESSING,
所以异步语义是被真的验证过的, 不是"同步取一次就成功"。

## 还没证明的 (别当已验证)

1. **网关侧不存在**。`/videos/generations` 与 `/videos/result/{id}` 是
   `stub_gateway.py` 顶的, `server/app/` 里还没有。
2. **没打过真厂商**。得先探清上游到底供哪些媒体模型、端点什么形状, 再照真实
   报文写适配器 —— 照文档猜形状必然返工。
3. **计量路径不存在**。`model_catalog.charge_credits()` 纯按 token 计价, 而
   媒体模型按件计量 —— `gen_models.py` 的 `SKIP_SUBSTRINGS` 把 `seedance` /
   `seedream` / `kling-` / `-image` 全跳掉, 正是因为这条路没建。
4. **手机上好不好用没测**。ComfyUI 的节点画布在手机上大概率很差, 而"手机能用"
   是这个产品区别于 Gitpod 那批的理由。
5. **本地模型节点是废的**。901 个节点里只有 22 个 (2%) 硬要模型文件, 但那 22 个
   是**入口** —— 没有 checkpoint, 下游 KSampler 就没东西可吃。而且不报错, 是
   下拉框空着、或者能跑但慢到永远转圈。ComfyUI 没有"只留我的节点"这种开关,
   只能靠预置 workflow 模板把人领进正确的路。
