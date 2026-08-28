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

## 官方 API 节点接了哪几家 (2026-08-28 逐家核过)

ComfyUI 自带 **40 个厂商**的官方节点, 全部打 `--comfy-api-base` 那个地址,
路径是 `/proxy/<厂商>/...`。`api_shim.py` 在容器里顶掉这个地址, 把报文转译成
我们网关的形状。

**接一家的前提有两条, 缺一不可**:

1. 网关真的卖那家的媒体模型 (`media_models.json` 里定过价);
2. **官方节点下拉里写死的型号名, 和我们在售的 id 对得上** —— 节点的模型下拉是
   硬编码在节点源码里的, 我们过滤不了它。名字对不上, 接了也只能每次回「未在售」。

第 2 条是大多数厂商接不了的真正原因, 不是工作量。逐家核过的结果:

| 厂商 | 网关有的媒体模型 | 节点下拉里的名字 | 结论 |
|---|---|---|---|
| byteplus / seedance | doubao-seedance-2.5 / 2.0 / fast / mini | dreamina-seedance-… | ✅ 已接 (去厂商前缀后匹配) |
| openai | gpt-image-2 / gpt-image-1.5 | `gpt-image-1 / 1.5 / 2` | ✅ 已接 (名字一字不差) |
| wan | wan2.7-t2v/i2v (720p/1080p)、wan3.0-video/-prime、wanx2.1-\* | `wan2.5-… / 2.6-… / 2.7-t2v / 2.7-i2v / 3.0-video(-prime)` | ✅ 已接 (2.7 与 3.0 都对得上) |
| qwen | qwen-image-3.0 / -pro | `qwen-image-3.0 / -pro` | ✅ 已接 (名字一字不差) |
| kling | kling-v3 / v3.0-std / v3.0-pro / v2-6 | `kling-3.0-turbo / v3-omni / video-o1 / v2-5-turbo` | ❌ 名字全对不上 |
| gemini / vertexai | 无 (只有 gemini 对话模型) | gemini-\*-image | ❌ 网关没有这些图模型 |
| xai | grok-imagine-video (**未定价**) | grok-… | ❌ 未定价 |
| 其余 33 家 | 无 | — | ❌ 网关一个型号都没有 |

> Kling 那行别顺手"映射一下": `kling-3.0-turbo` 与 `kling-v3` 是**不同型号、
> 不同价钱**, 悄悄替换等于按错档计价。要接就先让网关上架 turbo/omni 本身。

图像按张计价时**要按尺寸分档**: `qwen-image-3.0-pro` 的 1K 是 ¥0.25、2K 是 ¥0.5,
一口价要么让 1K 的人多付一倍, 要么 2K 单单亏本。分档判的是**像素面积**
(<= 2,250,000 算 1K), 不是边长 —— 按边长会把 2560x800 这种宽幅错判成 2K。

各家的形状差异 (转译就是在抹平这个):

| 厂商 | 建任务 | 轮询 | 失败怎么表达 |
|---|---|---|---|
| Ark (byteplus) | `content[]` 数组 | `{status, content.video_url}` | HTTP 4xx |
| DashScope multimodal (qwen) | `input.messages[].content[]` 混着 text/image | 无 (**同步**) | HTTP 200 + code/message |

### 百炼视频有**三代报文**, 用错那代不会报错

|  | 尺寸怎么写 | 首帧放哪 |
|---|---|---|
| wan2.5 / 2.6 | `parameters.size = "1920*1080"` | `input.img_url` |
| wan2.7 | `parameters.resolution` + `ratio` | `input.img_url` |
| wan3.0 | `parameters.resolution` + `ratio` | `input.media[{type:"first_frame"}]` |

2026-08-28 实测: 给 wan2.7-t2v 发 `size="1280*720"`, 它**照样按 1080P 出片**
(`usage` 回 `SR: 1080`), 字段被静默忽略。我们按 720p 收 10 积分/秒, 成本却是
1080P 的 $0.1434/秒 —— **每单亏七成, 两边都不报错**。所以哪个模型用哪代写在
`media_models.json` 的 `video_params` 里, 不靠型号名去猜; 漏标有守卫测试会红。

`ratio` 也必须转达: `size` 那张表全是 16:9, 用户在节点里选 9:16 竖屏, 走老写法
只会拿回横屏。

Wan 3.0 的节点还有个 **auto 时长** (发过来 `duration: -1`)。按秒计价的东西不能按
未知长度卖 —— 网关的 `max(1, duration)` 会把它算成 1 秒, 而上游可能出到 30 秒。
垫片在转发前就拦下, 并告诉用户去 duration 里选一个具体秒数。
| OpenAI | 与网关同构, 近乎直通 | 无 (同步) | HTTP 4xx |
| DashScope (wan/qwen) | `{input:{}, parameters:{}}` | `{output:{task_status, video_url}}` | **HTTP 200 + 顶层 code/message** |

最后一格是坑: DashScope 的节点在 `output` 缺席时才会把 `code - message` 抛给
用户。回 4xx 只会变成一句「请求失败」, 用户看不到该换哪个型号 —— 所以 Wan 的
未在售走的是 200。

改完跑 `shim_check.py` (23 项), 它对着 `stub_gateway.py` 走完四家的全部转译路径。

## 怎么跑验证

    docker build -t comfy-orchestrator:spike .
    python3 stub_gateway.py &                    # 顶替网关, 不花钱
    MEM=768m ./verify.sh

`verify.sh` 会走完: 起容器 → 等 ComfyUI → 确认节点注册 → 提交 workflow →
等执行 → 确认 MP4 真的落地 → 量内存峰值。桩刻意让前两次轮询返回 PROCESSING,
所以异步语义是被真的验证过的, 不是"同步取一次就成功"。

## 还没证明的 (别当已验证)

1. ~~网关侧不存在~~ **已建**: `server/app/media.py` (千面 + 百炼双通道)。
2. **没打过真厂商**。得先探清上游到底供哪些媒体模型、端点什么形状, 再照真实
   报文写适配器 —— 照文档猜形状必然返工。
3. ~~计量路径不存在~~ **已建**: 视频按秒预扣 + 失败退款, 图像按 image token
   结算 (`media.py` 的 `quote` / `image_credits` / `_refund_once`)。
4. **手机上好不好用没测**。ComfyUI 的节点画布在手机上大概率很差, 而"手机能用"
   是这个产品区别于 Gitpod 那批的理由。
5. **本地模型节点是废的**。901 个节点里只有 22 个 (2%) 硬要模型文件, 但那 22 个
   是**入口** —— 没有 checkpoint, 下游 KSampler 就没东西可吃。而且不报错, 是
   下拉框空着、或者能跑但慢到永远转圈。ComfyUI 没有"只留我的节点"这种开关,
   只能靠预置 workflow 模板把人领进正确的路。
