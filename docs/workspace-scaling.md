# 云工作台的弹性伸缩：现在别拆，要拆走 ECI

写在真的有人问「量大了自动扩、量小了自动缩、要不要上 k8s」的时候。

## 一、先看数字：现在没有钱可省

弹性伸缩省钱的前提是**峰谷比大且绝对量可观**。当前实况：

| | |
|---|---|
| 并发上限 `WORK_MAX_CONCURRENT` | 8（× 512MB = 4G） |
| 实际在跑的工作台 | 个位数 |
| 全部用户卷合计 | 不到 200MB |
| 承载机 | 已付费的固定成本 ECS，且**同机还跑着另一套生产栈** |

搬到任何按量计费的方案上，这个量级大概率**比现在更贵**：按秒计费的实例、NAS、
跨可用区流量都要钱，而现在这些的边际成本是零。

所以**拆机的正当理由不是省钱，是可用性**——别让工作台把同机的另一套生产压垮。
两个动机会导向不同的方案，先想清楚是哪个。

在真出现容量压力之前，已有的两道防护就够：

- `HostConfig.OomScoreAdj`：内存到悬崖时先死工作台，不是别人的数据库；
- 起容器前查宿主 `MemAvailable`，不足就返回 capacity。

## 二、核心约束：工作台是**有状态**的

每个用户两个持久卷，停机不删、下次接着用：

    dshwork-home-<hex>  →  /root         （dsh 的配置与历史）
    dshwork-ws-<hex>    →  /workspace    （用户的产物）

这一条决定了所有方案的形状：**卷跟着人走**。谁被调度到哪台机器，要么卷能跟过去
（网络存储），要么这个人被钉死在那台机器上（粘性调度）。不能假装它是无状态的。

还有一处容易忽略：`_ws_volume_dir()` **直接读宿主文件系统**上的卷目录，这是
「个人成品」在容器停着时仍能显示内容的原因。任何把卷挪走的方案都要回答它怎么办。

## 三、四条路

| 方案 | k8s | 弹性粒度 | 主要改动 |
|---|---|---|---|
| **ECI 弹性容器实例** | 否 | 每工作台一实例，按秒计费 | `_docker()` → ECI OpenAPI；卷 → NAS |
| ASK 无服务器 k8s | 是（无节点） | 同上，pod 跑在 ECI 上 | → k8s API |
| ACK + 节点池弹性 | 是 | 按节点扩缩，节点内装箱 | → k8s API + 调度策略 |
| ECS 弹性伸缩组 | 否 | 按整机扩缩 | 自己写「挑哪台机」的调度器 |

**推荐 ECI**。理由不是它时髦，是它和这个负载的形状几乎重合：工作台本来就是
「用户来了起一个、闲置 15 分钟就停」（`WORK_IDLE_STOP_MIN`），这正是 serverless
容器的教科书场景。按秒计费让成本严格跟着用量走，且**没有节点池要预热、没有
autoscaler 要调参、没有控制面要养**。

顺带解决一个原以为最麻烦的问题：卷换成 NAS 之后，只要应用所在机器也挂同一个 NAS
（只读即可），`_ws_volume_dir()` 那处直读**不用改**。

## 四、ECI 方案的工作量

| 改动 | 说明 |
|---|---|
| `_docker()` → ECI OpenAPI | 创建/查询/删除实例，替换 Docker API 调用 |
| 卷 → NAS 子目录 | `dshwork-home-<hex>` / `dshwork-ws-<hex>` 变成 NAS 上的路径 |
| `_upstream()` → VPC IP | ECI 实例有 VPC IP，比现在的容器名**更简单**（不再依赖 docker DNS） |
| 镜像冷启动 | 镜像约 1.4GB，必须配 ImageCache，否则每次冷启都要拉镜像 |
| 反代可达性 | 终止 TLS 的那一层要能访问 ECI 的 VPC IP |

`DOCKER_PROXY_URL` 已经是 env，所以「换一个后端」这件事架构上是留了口子的——
真正的工作量在卷和网络，不在调用方式。

## 四·五、已确认的阿里云资源（新加坡）

接入过程中逐项确认下来的，散在对话里容易丢，记这儿：

| 资源 | ID / 值 | 备注 |
|---|---|---|
| 地域 | `ap-southeast-1`（新加坡） | 与应用机同地域，跨地域内网不通 |
| VPC | `vpc-t4npjm6foh2kbdg59poy9` | |
| 交换机 | `vsw-t4naki832gpc5r6fs3sxy` | |
| 安全组 | `sg-t4ncj2p8oqqwhurroa2q` | **ECI 专用**，只放行 3081 ← `172.29.181.212/32` |
| 应用机内网 IP | `172.29.181.212` | 反代与 `forward_auth` 所在 |
| 镜像仓库 | `ghcr.io/agentsdancepro/dsh-local` | tag `rc8`; 选它是因为公开包免费不限量, 且镜像里只有官方 node 镜像 + Debian 公共包 + npm 上本就公开的 dsh, 没有我们的代码或密钥。**包必须设为 public**, 否则 ECI 每次建实例都要带仓库凭据 |

镜像压缩后 **218 MB**（`docker images` 显示的 1.08GB 是解压后的, 不是要传的量）。

⛔ **不要复用应用机的安全组**（那个开了 80/443 到 `0.0.0.0/0`，且没有 3081）。
套上去的后果是：容器状态照样 Running、看不出错，但**应用连不上、公网连得上**——
工作台跑的是 `danger-full-access` 的智能体沙箱，等于把它挂到公网。

计价（经济型 0.5 vCPU / 1 GiB，2026-08 新加坡）：`¥0.00001963/秒` = **¥0.0707/小时**，
不含 EIP。

## 五、动代码之前先做冷启动实测

**这是 go / no-go 的判据**：用户点开工作台要等多久，直接决定这个方案能不能用。
现在本地起容器几秒就绪（镜像在本地磁盘），ECI 冷启是另一回事。

前置条件（当前部署机上都不具备）：

- `aliyun` CLI；
- 一个**有 ECI 权限**的 RAM 用户 AK（现有的那个只授了 OSS）；
- 镜像推到 ACR，并按该镜像建好 ImageCache。

步骤与判据：

1. `docker build -t <registry>/dsh-local:rc8 -f deploy/open-search/Dockerfile.dsh deploy/open-search` 并推送；
2. 对该镜像创建 ImageCache，等其 Available；
3. 用 ECI OpenAPI 创建一个 2 vCPU / 1GB 的实例，挂一个 NAS 子目录到 `/workspace`；
4. 从「发起创建」到「`dsh web` 可访问」计时，重复 5 次取中位数与最差值。

判据：**中位数 30 秒以内、最差 60 秒以内**可以接受（用户点开工作台时有加载态）；
超过就得考虑预热池（保持 N 个空闲实例待命），而预热池会把「按用量计费」的好处
吃掉一部分——那时要重新算账。

### 实测：第 1 次（无镜像缓存），2026-08-19 新加坡

实例 `eci-t4nbtigms4kep48urk8n`，0.5 vCPU / 1 GiB / economy，EIP 自动创建 100 Mbps。

| 阶段 | 耗时 |
|---|---|
| 调度 + 镜像缓存判定（`Missed image cache`） | 18s |
| 拉取 `ghcr.io/agentsdancepro/dsh-local:rc8`（228,551,691 字节） | 32.4s |
| 创建 + 启动容器 | <1s |
| **合计** | **50s** |

**50 秒未达中位数目标，但在最差线以内。** 关键是 ECI 在 miss 之后**自动创建了镜像
缓存** `imc-t4nbtigms4kep48urk8o`，所以这 32 秒只有第一个实例付——见下一节。

功能验证（从应用机 `172.29.181.212` 经安全组探 `172.29.181.214:3081`）：dsh web
返回 200 / 14549 字节，**与本地 `dsh-local:rc8` 容器逐字节一致**。

⚠️ 同时测出来一件事：**不改 Host 的请求同样返回 200**。dsh 的可达性围栏在这条
链路上不起保护作用——socat 是从容器回环连向 dsh 的，dsh 看到的来源永远是
127.0.0.1，围栏恒真；Caddy 的 Host 重写不是安全边界。也就是说**能连上 3081 的就
能完全操作该用户的工作台，这一跳没有应用层鉴权**。本机部署里挡住它的是隔离的
docker 网络；换到 ECI，挡住它的只有安全组，没有第二层。安全组只放行
`172.29.181.212/32 → 3081`，不是保守，是唯一那层。

### 实测：第 2 次，以及镜像缓存为什么没生效

第 2 次（`eci-t4nd3f1bjw509v9vewvu`，配置与第 1 次相同）：**52 秒**，其中拉镜像
32.7s —— 和第 1 次的 32.4s 几乎相同。**镜像缓存完全没起作用。**

事件里写着原因：

```
[eci.imagecache]Image cache auto create failed for failed to pull images.
[eci.imagecache]Missed image cache.
```

第 1 次那条 "Image cache ... is auto created" 只是**发起**了创建，并没有建成；
`DescribeImageCaches` 返回 `TotalCount: 0` 就是证据。

根因用显式 `CreateImageCache` 复现并定位到了：缓存构建会在你指定的
vSwitch/安全组里起一个**独立的容器组**来拉镜像，查它 `InternetIp` 是
**空的**——构建任务不共享我们挂在业务容器组上的 EIP，因此没有公网出口，
拉不到 ghcr.io。它会一直卡在 `Preparing` / 进度 0% / "start to pull images"
直到超时。不是 manifest 兼容性问题：同一个镜像、同一个仓库，业务容器组自己
拉是成功的（32s），只有缓存构建这条路没有出口。

`CreateImageCache` 有 `--EipInstanceId`，但**没有** `AutoCreateEip` ——
必须先有一个现成的 EIP。

三条出路：

| 方案 | 代价 | 说明 |
|---|---|---|
| **A. 单独一个 EIP，建缓存时用 `--EipInstanceId`** | 一个 EIP | **推荐**。缓存只在镜像版本变更时重建，EIP 可以用完即释放 |
| B. 镜像迁到 ACR（企业版，有 VPC 域名） | 固定月费 | 构建任务走内网，无需公网；但要为此买 ACR |
| C. 交换机挂 NAT 网关 | 固定月费 | 同时解决工作台出网；但在中等规模下, 按实例分配 EIP 更便宜——NAT 是固定成本, 要跑满才划算 |

（B/C 的具体价格以阿里云价格页为准, 这里不写死。）

选 A 的话, 生产上工作台仍然各自自动创建 EIP（`npm install` 需要出网）,
只有"重建镜像缓存"这一步需要那个独立 EIP。

### 实测：方案 A 落地，冷启动判据通过

按方案 A 做了一遍：申请一个 EIP → `CreateImageCache --EipInstanceId <eip>` →
构建容器组这次 `InternetIp` 有值, 事件出现
`Successfully pulled image ghcr.io/agentsdancepro/dsh-local:rc8` → 缓存 Ready
（盘只用了 3.52GB, 但 `ImageCacheSize` 传 10 也会被 ECI 抬回 20 —— 有最小值, 压不下去）→ 测完释放 EIP。

带缓存冷启动（`AutoMatchImageCache true`, 每台自动创建 EIP, 从发起创建到
状态 Running）：

| 轮次 | 耗时 |
|---|---|
| 1 | 19s |
| 2 | 23s |
| 3 | 17s |
| **中位数** | **19s** |
| **最差** | **23s** |

**判据（中位数 ≤30s、最差 ≤60s）通过。** 对比无缓存的 50-52s, 省下的正是那
≈32s 拉取。三台的 dsh web 都返回 200 / 14549 字节, 与本地 `dsh-local:rc8` 一致。

19 秒里几乎全是 ECI 自身的调度开销, **再优化镜像也压不下去**。要继续提速只剩
预热池, 而那会侵蚀按量计费的好处 —— 也就是说 19s 基本就是这条路线的下限。

运维含义：**每次 bump 工作台镜像版本, 都要重建镜像缓存**, 否则退回 50s。步骤是
申请 EIP → CreateImageCache → 等 Ready → 释放 EIP, 应当脚本化并挂进发版流程。

### RAM 权限：逐个动作实测过的最小集

单个 RAM 用户 `dshcloud-eci`, 只挂一条自定义策略。**十个动作全部逐个调用验证过**,
不多不少：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "eci:CreateContainerGroup",
        "eci:DeleteContainerGroup",
        "eci:DescribeContainerGroups",
        "eci:DescribeContainerLog",
        "eci:CreateImageCache",
        "eci:DescribeImageCaches",
        "eci:DeleteImageCache"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "vpc:AllocateEipAddress",
        "vpc:ReleaseEipAddress",
        "vpc:DescribeEipAddresses"
      ],
      "Resource": "*"
    }
  ]
}
```

**刻意不用 `AliyunECIFullAccess`**：那是 `eci:*`, 含 `ExecContainerCommand`
（控制台「Workbench 远程连接」走的就是它）—— 等于这把钥匙能钻进任意用户的工作台
执行命令。生产服务器上的凭据不该有这个能力。

两条实测出来的细节：

- `AutoCreateEip=true` 建实例**不需要调用方有 EIP 权限**, ECI 自己分配。证据是在
  授予 EIP 权限之前, 用 API 带 `--AutoCreateEip true` 建的实例照样拿到公网并拉下了
  镜像。`vpc:AllocateEipAddress` 只有**给镜像缓存构建任务显式要 EIP** 时才用得上。
- 不含 ECS 任何权限, 所以读不了安全组规则。不影响运行。

## 六、什么时候才轮到 k8s

k8s 买的是声明式调度、自愈、生态；代价是一整个新的运维面。而这个应用**已经在
自己做调度**（直接创建容器），换成 k8s 等于把 `workspace.py` 重写一遍——工作量
和换 ECI 相当，之后却要长期养一个集群。

真正会把你推向 k8s（具体是 ACK + ECI 混合）的信号只有一个：**并发到了几百个
工作台，纯 ECI 的单实例开销开始比自己装箱贵**。在那之前，ECI 更省事也更省钱。
