"""云工作台支持的产品。

工作台原本只有一种形态 (dsh), 于是"哪个用户"和"哪个工作台"是同一件事 ——
workspace.py 与 workbackend.py 里到处以 user_id 为键: 容器名、卷名、上游地址、
并发锁、回收器。要让 ComfyUI 之类以同样方式接进来, 不必给那些函数逐个加参数,
把**键**换掉就够了。

    wskey(user, "dsh")      -> "usr_abc"            (原样, 见下)
    wskey(user, "comfyui")  -> "usr_abc~comfyui"

**dsh 必须保持原键**: 线上已经有按 user_id 命名的容器与卷 (dshwork-<hexid>、
dshwork-home-<hexid>、dshwork-ws-<hexid>)。换了键就等于把这些用户的工作台和
历史文件全部弃养 —— 容器变成孤儿, 卷还在磁盘上但再没人引用。

真正需要按产品区分的只有四样: 镜像、容器内应答端口、资源规格、访问域名。
ComfyUI 的前端用绝对路径引资源, 塞不进子路径, 所以只能一个产品一个域名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config, model_catalog

logger = logging.getLogger(__name__)

# dsh 的工作台键就是 user_id 本身 —— 见模块说明, 这是兼容性要求, 不是风格选择。
DEFAULT = "dsh"
_SEP = "~"

# 静态预览服务的端口。dsh 工作台用它把 /workspace 里的产物直接开出来。
# workspace.py 从这里 import —— 两处各写一份必然漂: 启动脚本按它起服务,
# 预览索引按它生成链接, 对不上就是「产物点开是 502」。
PREVIEW_STATIC_PORT = 8088

# 官方 API 节点垫片的端口。只在容器回环上, 不经反代 —— 它带着容器的凭据。
SHIM_PORT = 8199

# OpenClaw 网关 (API + 控制台 UI + 频道入口) 的端口。
OPENCLAW_PORT = 18789


@dataclass(frozen=True)
class Sidecar:
    """主容器旁边的一个伴随容器 (中间件): 数据库、缓存、向量库这类。

    Coze/Dify/Penpot 都是 compose 栈, 不是 ComfyUI 那种单容器。塞进一个
    all-in-one 镜像意味着**每次上游发版都要重打包维护** —— 所以走 ECI 容器组的
    多容器: 伴随容器用**上游原生镜像**, 我们只写编排, 一个镜像都不自己维护。

    组内所有容器共享网络命名空间 (k8s pod 语义), 互相用 127.0.0.1 访问 ——
    所以栈里的服务发现全部改成回环地址, 不存在 compose 的服务名 DNS。
    资源不按容器划分: 组给总的 cpu/mem, 容器之间自己挤 (与 compose 默认一致)。
    """

    name: str  # 组内容器名
    image_ref: str  # 上游原生镜像的完整地址
    cmd: tuple[str, ...] = ()  # 空 = 用镜像默认 entrypoint
    # 传给 entrypoint 的参数。**cmd 会顶掉 entrypoint, args 不会** —— 有些镜像的
    # entrypoint 是一整套初始化 (Hermes 是 s6 监督树), 顶掉它服务就起不来, 而它
    # 只在 stderr 抱怨一句、进程照常跑, 于是"起来了但什么都不工作"。
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    # NAS 卷上的挂载: (用户子目录下的相对路径, 容器内路径)。中间件的数据要
    # 跟着用户走 —— 实例回收重建后, 库还在。
    mounts: tuple[tuple[str, str], ...] = ()
    # 种子卷上的挂载: (卷内相对路径, 容器内路径); 相对路径为空 = 挂整卷。
    # 见 InitContainer —— 这是把 compose 的 bind mount 搬进 ECI 的那条路。
    seeds: tuple[tuple[str, str], ...] = ()
    # 以哪个 uid 跑。None = 用镜像自己的 USER。
    # bitnami 那一系 (redis/es/etcd) 镜像里 USER 是 1001, 而 NAS 挂进来的目录
    # 是 root 的 —— 它们启动脚本里的 chown 会失败, 然后进程写不进数据目录。
    # 上游 compose 靠 `user: root` 解决, 这里是等价物 (0 = root)。
    run_as_user: int | None = None


@dataclass(frozen=True)
class InitContainer:
    """在**全部**常规容器之前跑完的一次性容器 (k8s/ECI 的 initContainer)。

    存在的理由只有一个: compose 栈普遍靠 bind mount 送配置文件进去 (Coze 有 17
    处: 建库 SQL、ES 中文分词器、MinIO 图标、nginx 配置、后端配置目录)。ECI 没有
    bind mount, 而 ConfigFileVolume 走 base64 塞不下 MB 级的资产。

    于是: 资产烤进一个只装文件的小镜像, 初始化容器把它铺到组内共享的 EmptyDir
    卷 (SEED_VOLUME) 上, 各服务容器用 Sidecar.seeds / Product.seeds 从那里取。
    **上游镜像一个都不用改**, 与 Sidecar 的设计前提一致。

    为什么不用"伴随容器铺 + 各容器自旋等标记文件": 那要给每个上游镜像的启动
    命令加等待循环, 而它们的 entrypoint 各不相同 (bitnami 三件套、mysql、minio
    各有各的规矩), 每加一个产品就多几处能写错的地方。初始化容器是现成的顺序
    保证 —— 铺不完, 常规容器根本不会起。
    """

    name: str
    image_ref: str
    cmd: tuple[str, ...] = ()
    # 种子卷挂在初始化容器内的哪个路径 (它要往这里写)。
    seed_mount: str = "/seed"
    # NAS 上的挂载: (用户子目录下的相对路径, 容器内路径)。用来在常规容器起来
    # **之前**往用户的持久目录里写东西 —— 比如给一个 entrypoint 顶不得的产品
    # 预置配置 (见 Sidecar.args)。
    mounts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    image: str
    image_ref: str  # ECI 拉取用的完整仓库地址; 留空则回落到 image
    port: int  # 容器内应答的端口, 会写进 X-Work-Upstream
    mem_mb: int
    cpus: float
    domain: str  # 这个产品的工作台域名; 留空 = 该产品未启用
    # 探活打哪个路径 (见 workspace._ready)。默认首页就够: 单容器产品的端口一通,
    # 就是应用本身在应答。
    #
    # **前置 nginx + 独立后端的栈产品必须改掉**。那种形态下首页答的是前端 (静态
    # 资源或 SSR), 而前端起得比后端早得多 —— 首页早早回 200, 应用却是坏的。判据
    # 只看状态码, 分不出"应用好了"和"前端把自己的错误页渲染出来了"。
    #
    # 2026-08-30 Dify 事故: api 要先跑完数据库迁移才 bind 5001 (约 75 秒), 而
    # nginx 和 Next.js 几秒就起来。用户在 api 就绪前 44 秒被放进去, 吃到 Dify 的
    # React 错误边界「渲染此组件时发生了意外错误。」—— SSR 的错误页 **HTTP 仍然
    # 是 200**, 旧探针一路绿灯。而且不自愈: 后端 44 秒后好了, 那张已经渲染出来的
    # 页面还是坏的, 只能用户自己刷新。
    #
    # 指到一条**代理去后端**的路径, 后端没起来时 nginx 给 502, 判据自然做对。
    ready_path: str = "/"
    # 前端会不会主动上报「人还在」(dsh 调 /api/work/active)。False 表示没有
    # 上报器, 回收器改用请求流量当在场信号 —— 否则容器起来十分钟就被当成
    # 空闲杀掉, 不管人在不在用。见 workspace.reaper_tick。
    reports_presence: bool = True
    # 标签页关掉后再留多久 (分钟)。0 = 用全局的 WORK_TAB_GONE_MIN。
    #
    # 按产品分开, 因为"多留一会儿"的划算程度取决于**冷启动有多贵**:
    # ComfyUI 的冷启动实测 ~26 秒 (ECI 调度 16s + 建 EIP 4.8s + ComfyUI 启动 5.5s),
    # 关一次页再回来就要重等一遍, 所以宁可多留几分钟机时。dsh 没这个包袱,
    # 保持全局的 3 分钟 —— 一刀切成 10 分钟等于替 dsh 用户白烧机时。
    tab_grace_min: int = 0
    # 伴随容器 (中间件)。非空 = 这是一个多容器栈产品:
    #   · 只有 ECI 后端能跑 (容器组多容器); docker 后端遇到会直接报错, 不静默降级
    #   · 组的 RestartPolicy 用 Always —— ECI 没有 depends_on, 应用容器在中间件
    #     就绪前会崩溃退出, 靠重启拉起来 (k8s 的标准做法)
    sidecars: tuple[Sidecar, ...] = ()
    # 这些主机名全部解析到 127.0.0.1 (ECI 的 HostAliase, 即写 /etc/hosts)。
    # compose 栈里的服务互相用服务名找对方 (proxy_pass http://api:5001 这类,
    # 往往写死在镜像的配置模板里改不了) —— 把服务名指回环, **上游镜像一个都
    # 不用改**就能在共享网络命名空间里跑。
    host_aliases: tuple[str, ...] = ()
    # 初始化容器 (见 InitContainer)。非空 = 组里会多一个 EmptyDir 种子卷。
    init_containers: tuple[InitContainer, ...] = ()
    # 主容器要从种子卷上取的东西: (卷内相对路径, 容器内路径)。
    seeds: tuple[tuple[str, str], ...] = ()
    # 主容器以哪个 uid 跑。None = 用镜像自己的 USER。
    # 与 Sidecar.run_as_user 同理: NAS 挂进来的目录是 root 的, 镜像里 USER 不是
    # root 的话 (OpenClaw 是 node) 就写不进自己的状态目录, 而它只会在日志里抱怨
    # 一句数据库打不开, 照常起来 —— 于是用户的东西全落在容器内, 一回收就没了。
    run_as_user: int | None = None


def wskey(user_id: str, product_id: str = DEFAULT) -> str:
    return user_id if product_id == DEFAULT else f"{user_id}{_SEP}{product_id}"


def split_key(key: str) -> tuple[str, str]:
    if _SEP not in key:
        return key, DEFAULT
    user_id, _, product_id = key.partition(_SEP)
    return user_id, product_id


# 栈产品 env 里的占位符: create 时替换成该用户的确定性密钥 (security.stack_secret)。
# Sidecar 是静态数据, 而密钥按用户走 —— 不能把密钥写死在产品定义里。
STACK_SECRET_PLACEHOLDER = "__DSH_STACK_SECRET__"
#: 同理的网关凭据占位符。栈产品的应用容器 (Coze 的 coze-server 这类) 要拿它
#: 直连我们的模型网关, 好让用户开箱就有模型可选、不用自己填 API Key。
GATEWAY_TOKEN_PLACEHOLDER = "__DSH_GATEWAY_TOKEN__"
#: 同一个每用户密钥, 截到 16 字节。AES-128 的 key 长度是**硬性**的, 传满长度
#: 的十六进制串进去会在应用侧报一个跟密钥毫无关系的错 (Coze 的插件 OAuth 就
#: 要这个长度)。
STACK_SECRET16_PLACEHOLDER = "__DSH_STACK_SECRET16__"
#: 同一把密钥推成**口令形状**的那个值 (见 autologin_password)。伴随容器 env 里
#: 用它, 主容器那边直接调函数 —— 两边必须是同一个值, 否则替用户登录永远失败,
#: 而症状只是"页面能开、接口全 401", 看不出是口令对不上。
STACK_PASSWORD_PLACEHOLDER = "__DSH_STACK_PASSWORD__"


def resolve_sidecars(sidecars: tuple[Sidecar, ...], secret: str, token: str = "") -> tuple[Sidecar, ...]:
    """把伴随容器 env 里的占位符换成真值 (每用户密钥、网关凭据)。"""
    from dataclasses import replace

    def sub(v: str) -> str:
        return (
            v.replace(STACK_PASSWORD_PLACEHOLDER, autologin_password(secret))
            .replace(STACK_SECRET16_PLACEHOLDER, secret[:16])
            .replace(STACK_SECRET_PLACEHOLDER, secret)
            .replace(GATEWAY_TOKEN_PLACEHOLDER, token)
        )

    return tuple(replace(sc, env=tuple((k, sub(v)) for k, v in sc.env)) for sc in sidecars)


def resolve_init_containers(
    ics: tuple[InitContainer, ...], secret: str = "", token: str = ""
) -> tuple[InitContainer, ...]:
    """初始化容器的**命令行**里也可能带占位符。

    Hermes 就是: 它的模型配置由初始化容器用 `hermes config set` 写进 NAS, 而
    api_key 要换成该用户的网关令牌。不替换的话会把字面量 `__DSH_GATEWAY_TOKEN__`
    原样写进 config.yaml —— 配置文件看着好好的, 一发消息就 401。
    """
    from dataclasses import replace

    def sub(v: str) -> str:
        return (
            v.replace(STACK_PASSWORD_PLACEHOLDER, autologin_password(secret))
            .replace(STACK_SECRET16_PLACEHOLDER, secret[:16])
            .replace(STACK_SECRET_PLACEHOLDER, secret)
            .replace(GATEWAY_TOKEN_PLACEHOLDER, token)
        )

    return tuple(replace(ic, cmd=tuple(sub(a) for a in ic.cmd)) for ic in ics)


# ---- Dify (LLM 应用搭建, 10 容器栈) ----------------------------------------
#
# 组内所有容器共享网络命名空间, 全部走 127.0.0.1。三个坑是 2026-08-29 用
# `docker run --network=container:` (与 ECI 容器组同款语义) 逐个跑出来的:
#
#   1. api 与 api_websocket 都默认监听 5001 —— compose 里各有 IP 所以不冲突,
#      共享命名空间里会撞。给 websocket 那个 DIFY_PORT=5011。
#      (同理 sandbox/local_sandbox、两个 ssrf_proxy 也撞, 各只保留一个。)
#   2. web 是 Next.js standalone, 不设 HOSTNAME 就绑容器 IP 而不是 0.0.0.0 ——
#      回环够不着, nginx 侧表现为 502。
#   3. 官方 nginx 配置用 `set $up api:5001; proxy_pass http://$up;` +
#      `resolver 127.0.0.11` (Docker 内嵌 DNS)。nginx 的 resolver **不读
#      /etc/hosts**, 所以 host_aliases 兜不住它 —— 我们自己生成 conf, upstream
#      写死回环, 绕开 resolver。
#
# 还有一个不报错的坑: compose 的 .env.example 把 SECRET_KEY 留空等运维填。
# 空着不影响启动, 但**注册成功、登录报 Invalid encrypted data** —— 所以它走
# STACK_SECRET_PLACEHOLDER, 按用户确定性推导 (实例重建后账号还能登)。
_DIFY_DB_PASSWORD = "difyai123456"
_DIFY_REDIS_PASSWORD = "difyai123456"
_DIFY_WEAVIATE_KEY = "WVF5YThaHlkYwhGUSmCRgsX3tD5ngdN8pkih"
_DIFY_SANDBOX_KEY = "dify-sandbox"
_DIFY_INNER_KEY = "QaHbTe77CtuXmsfyhR7+vRjI/+XbV1AaFy691iy+kGDv2Jvy0/eAh8Y1"
_DIFY_PLUGIN_KEY = "lYkiYYT6owG+71oLerGzA7GXCgOT++6ovaezWAjpCjf+Sjc3ZtU+qUEi"
# 上面这些是**组内回环**上的凭据: 库、缓存、向量库、沙箱都只在容器组内可达,
# 跨用户隔离靠网络边界与 NAS 子路径, 不靠这几个常量 (与 Penpot 同一判断)。
# 只有 SECRET_KEY 加密的是**落库的用户数据**, 所以它必须按用户走。

# 下面这一段是**表**, 不是代码块: 相关的键成对排在一行上读。交给 formatter
# 会拆成一项一行, 分组关系随之消失。
# fmt: off
_DIFY_DB_ENV = (
    ("DB_HOST", "127.0.0.1"), ("DB_PORT", "5432"), ("DB_USERNAME", "postgres"),
    ("DB_PASSWORD", _DIFY_DB_PASSWORD), ("DB_DATABASE", "dify"),
    ("REDIS_HOST", "127.0.0.1"), ("REDIS_PORT", "6379"),
    ("REDIS_PASSWORD", _DIFY_REDIS_PASSWORD), ("REDIS_DB", "0"), ("REDIS_USE_SSL", "false"),
    ("CELERY_BROKER_URL", f"redis://:{_DIFY_REDIS_PASSWORD}@127.0.0.1:6379/1"),
    # CELERY_BACKEND 是**后端类型**不是主机名 —— 早先做文本改写时把它连同
    # `redis://` 的 scheme 一起换成了 127.0.0.1, celery 报 `No such transport: ''`。
    ("CELERY_BACKEND", "redis"),
)
# fmt: on

# 下面这一段是**表**, 不是代码块: 相关的键成对排在一行上读。交给 formatter
# 会拆成一项一行, 分组关系随之消失。
# fmt: off
_DIFY_APP_ENV = _DIFY_DB_ENV + (
    ("SECRET_KEY", STACK_SECRET_PLACEHOLDER),
    ("VECTOR_STORE", "weaviate"),
    ("WEAVIATE_ENDPOINT", "http://127.0.0.1:8080"),
    ("WEAVIATE_API_KEY", _DIFY_WEAVIATE_KEY),
    ("STORAGE_TYPE", "opendal"), ("OPENDAL_SCHEME", "fs"), ("OPENDAL_FS_ROOT", "storage"),
    ("MIGRATION_ENABLED", "true"),
    ("PLUGIN_DAEMON_URL", "http://127.0.0.1:5002"),
    ("PLUGIN_DAEMON_KEY", _DIFY_PLUGIN_KEY),
    ("INNER_API_KEY_FOR_PLUGIN", _DIFY_INNER_KEY),
    ("PLUGIN_MAX_PACKAGE_SIZE", "52428800"),
    ("CODE_EXECUTION_ENDPOINT", "http://127.0.0.1:8194"),
    ("CODE_EXECUTION_API_KEY", _DIFY_SANDBOX_KEY),
    ("LOG_LEVEL", "INFO"), ("DEPLOY_ENV", "PRODUCTION"),
    # 上游默认连错 5 次密码就锁 24 小时。单用户工作台里这把锁只锁得住我们自己
    # (能走到这个域的人早过了我们那层鉴权), 而一旦锁上, 整个产品一天不能用。
    # 收到 5 分钟: 防爆破的意义还在, 又不会因为一次自动重试把人关在门外。
    ("LOGIN_LOCKOUT_DURATION", "300"),
    # 不跑 squid: 它要挂配置文件, 而我们的每用户容器组本来就是隔离边界。
    # 留空 = 不经代理直出; 留着指向不存在的 3128 会让 HTTP 请求节点全挂。
    ("SSRF_PROXY_HTTP_URL", ""), ("SSRF_PROXY_HTTPS_URL", ""),
    ("MARKETPLACE_ENABLED", "true"),
    ("MARKETPLACE_API_URL", "https://marketplace.dify.ai"),
)
# fmt: on


# 下面这一段是**表**, 不是代码块: 相关的键成对排在一行上读。交给 formatter
# 会拆成一项一行, 分组关系随之消失。
# fmt: off
def _dify_stack() -> tuple[Sidecar, ...]:
    v = config.DIFY_VERSION
    api_img = f"langgenius/dify-api:{v}"
    return (
        Sidecar(name="api", image_ref=api_img,
                env=_DIFY_APP_ENV + (("MODE", "api"), ("DIFY_PORT", "5001")),
                mounts=(("dify/storage", "/app/api/storage"),)),
        # 同一个镜像的第二份, 只为 websocket —— 必须换端口, 否则与 api 撞 5001
        Sidecar(name="api-ws", image_ref=api_img,
                env=_DIFY_APP_ENV + (("MODE", "api"), ("DIFY_PORT", "5011")),
                mounts=(("dify/storage", "/app/api/storage"),)),
        Sidecar(name="worker", image_ref=api_img,
                env=_DIFY_APP_ENV + (("MODE", "worker"),),
                mounts=(("dify/storage", "/app/api/storage"),)),
        Sidecar(name="web", image_ref=f"langgenius/dify-web:{v}", env=(
            # 空 URL = 同源。整站在一个域上, 前端拼相对路径即可。
            ("CONSOLE_API_URL", ""), ("APP_API_URL", ""),
            ("SERVER_CONSOLE_API_URL", "http://127.0.0.1:5001"),
            ("MARKETPLACE_API_URL", "https://marketplace.dify.ai"),
            ("MARKETPLACE_URL", "https://marketplace.dify.ai"),
            ("NEXT_TELEMETRY_DISABLED", "1"),
            ("TEXT_GENERATION_TIMEOUT_MS", "60000"),
            # Next.js standalone 不设这个就绑容器 IP, 回环打不进去 -> 502
            ("HOSTNAME", "0.0.0.0"), ("PORT", "3000"),
        )),
        Sidecar(name="plugind", image_ref=f"langgenius/dify-plugin-daemon:{config.DIFY_PLUGIN_DAEMON_VERSION}",
                env=_DIFY_DB_ENV + (
                    # 它自己另建一个库 (不需要 initdb 脚本, 实测会自建)
                    ("DB_DATABASE", "dify_plugin"), ("DB_SSL_MODE", "disable"),
                    ("SERVER_PORT", "5002"), ("SERVER_KEY", _DIFY_PLUGIN_KEY),
                    ("DIFY_INNER_API_URL", "http://127.0.0.1:5001"),
                    ("DIFY_INNER_API_KEY", _DIFY_INNER_KEY),
                    ("MAX_PLUGIN_PACKAGE_SIZE", "52428800"),
                    ("PLUGIN_STORAGE_TYPE", "local"),
                    ("PLUGIN_STORAGE_LOCAL_ROOT", "/app/storage"),
                    ("PLUGIN_WORKING_PATH", "/app/storage/cwd"),
                    ("PLUGIN_INSTALLED_PATH", "plugin"),
                    ("FORCE_VERIFYING_SIGNATURE", "true"),
                    ("PYTHON_ENV_INIT_TIMEOUT", "120"),
                    ("PLUGIN_MAX_EXECUTION_TIMEOUT", "600"),
                    # 下面这些**不是可选项**: plugin daemon 在启动时逐个校验,
                    # 缺一个就 exit 1 并 CrashLoopBackOff。2026-08-29 首次上
                    # ECI 就栽在 PLUGIN_REMOTE_INSTALLING_HOST 上 —— 日志只有
                    # 一句 "plugin remote installing host is empty"。
                    # 教训: 这个容器的 env 要照抄验证过的那套, 不能凭"看起来
                    # 像默认值"精简。
                    ("DB_TYPE", "postgresql"),
                    # 远程装插件是调试功能, 绑回环即可 —— 实例自带 EIP,
                    # 绑 0.0.0.0 等于把它摆到公网上 (安全组是最后一道, 但没有
                    # 理由多开一个面)。
                    ("PLUGIN_REMOTE_INSTALLING_HOST", "127.0.0.1"),
                    ("PLUGIN_REMOTE_INSTALLING_PORT", "5003"),
                    ("PLUGIN_MEDIA_CACHE_PATH", "assets"),
                    ("PLUGIN_PACKAGE_CACHE_PATH", "plugin_packages"),
                    ("PLUGIN_MAX_FILE_SIZE", "52428800"),
                    ("PLUGIN_STDIO_BUFFER_SIZE", "1024"),
                    ("PLUGIN_STDIO_MAX_BUFFER_SIZE", "5242880"),
                    ("PLUGIN_MODEL_SCHEMA_CACHE_TTL", "3600"),
                    ("PLUGIN_PPROF_ENABLED", "false"),
                    ("PLUGIN_SENTRY_ENABLED", "false"),
                    ("PYTHONIOENCODING", "utf-8"),
                    ("UV_CACHE_DIR", "/tmp/.uv-cache"),
                ),
                mounts=(("dify/plugin", "/app/storage"),)),
        Sidecar(name="sandbox", image_ref=f"langgenius/dify-sandbox:{config.DIFY_SANDBOX_VERSION}", env=(
            ("API_KEY", _DIFY_SANDBOX_KEY), ("GIN_MODE", "release"),
            ("WORKER_TIMEOUT", "15"), ("ENABLE_NETWORK", "true"), ("SANDBOX_PORT", "8194"),
        )),
        Sidecar(name="postgres", image_ref="postgres:15-alpine", env=(
            ("POSTGRES_USER", "postgres"), ("POSTGRES_PASSWORD", _DIFY_DB_PASSWORD),
            ("POSTGRES_DB", "dify"),
            # 数据放子目录: NAS 子路径上可能有 lost+found 之类, initdb 拒绝非空目录
            ("PGDATA", "/var/lib/postgresql/data/pgdata"),
        ), mounts=(("dify/pg", "/var/lib/postgresql/data"),)),
        Sidecar(name="redis", image_ref="redis:6-alpine",
                cmd=("redis-server", "--requirepass", _DIFY_REDIS_PASSWORD),
                mounts=(("dify/redis", "/data"),)),
        Sidecar(name="weaviate", image_ref="semitechnologies/weaviate:1.27.0", env=(
            ("PERSISTENCE_DATA_PATH", "/var/lib/weaviate"),
            ("QUERY_DEFAULTS_LIMIT", "25"),
            ("AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED", "true"),
            ("DEFAULT_VECTORIZER_MODULE", "none"),
            ("CLUSTER_HOSTNAME", "node1"),
            ("AUTHENTICATION_APIKEY_ENABLED", "true"),
            ("AUTHENTICATION_APIKEY_ALLOWED_KEYS", _DIFY_WEAVIATE_KEY),
            ("AUTHENTICATION_APIKEY_USERS", "dsh@dshcloud.online"),
            ("AUTHORIZATION_ADMINLIST_ENABLED", "true"),
            ("AUTHORIZATION_ADMINLIST_USERS", "dsh@dshcloud.online"),
        ), mounts=(("dify/weaviate", "/var/lib/weaviate"),)),
    )
# fmt: on


def _dify_embedding_model() -> str:
    """预置给 Dify 的向量化模型; 目录里没有就返回空串 (跳过, 而不是配个假的)。

    有它知识库才能用 —— 没有向量化模型, Dify 建知识库那一步直接卡住。
    """
    try:
        mid = model_catalog.default_embedding_model()
        return mid if model_catalog.resolve_embedding(mid) else ""
    except AttributeError:
        # 自建部署带着旧的 models.json 时没有这两个函数, 跳过预置即可。
        return ""


def _sh_list(ids) -> str:
    """摊成空格分隔的一行, 供 autologin 脚本里的 `for M in $DSH_MODELS` 用。

    **带空白的 id 直接剔掉**而不是照原样塞进去: sh 按空白分词, 那种 id 会被拆成
    两个不存在的模型, 表现是预置日志里两条莫名其妙的失败, 而真正那个模型没配上。
    目前目录里没有这种 id, 这里只是不让它有机会变成一个查半天的怪事。
    """
    return " ".join(i for i in ids if i and not any(c.isspace() for c in i))


def _dify_chat_models() -> str:
    """预置给 Dify 的全部在售 chat 模型。

    顺序照目录 —— models.json 是按倍率从低到高排的, 于是万一中途失败, 已经配上的
    是便宜的那几个。默认模型也在这个列表里, 由 provision() 跳过 (它单独先配, 且
    只有它设工作区默认)。
    """
    return _sh_list(model_catalog.catalog())


def _dify_embedding_models() -> str:
    """预置给 Dify 的全部向量化模型。知识库建好之后换模型要重建索引, 所以多给几个
    选择的意义不如 chat 那边大 —— 但少给同样没道理, 何况刷新是免费的。"""
    try:
        return _sh_list(model_catalog.embedding_catalog())
    except AttributeError:
        return ""


def autologin_password(secret: str) -> str:
    """免登录账号的密码, 从该用户的栈密钥推导。

    定长小写十六进制不一定过得了口令强度校验, 所以前后各补一段, 保证同时含
    大写、小写、数字。secret 为空时返回空串 —— 上层据此跳过免登录 (而不是用一个
    所有人共用的弱口令)。
    """
    return f"Dsh{secret[:24]}1a" if secret else ""


#: 工作台自己签发 Dify 的会话, 于是用户不再看到第二道登录墙。
#: 通则见 _COZE_AUTOLOGIN —— 只留我们这一层登录墙。
#:
#: 与 Coze 的差别有两处:
#:   · Dify 认的是 **cookie + X-CSRF-Token 头**, 而 access_token 是 HttpOnly,
#:     前端读不到、也不发 Authorization。所以必须给**浏览器**发 Set-Cookie,
#:     不能像 Coze 那样只在上游注入 —— 前端还要自己从 csrf_token 里取值拼头。
#:   · Dify 是单租户, setup 只能跑一次, 建不了第二个账号 -> 密码走推导而不是
#:     随机存盘 (见 dify_autologin_password)。
_DIFY_AUTOLOGIN = r"""#!/bin/sh
# 由 products.py 下发。工作台自己登一次 Dify, 把三个会话 cookie 发给浏览器。
#
# Dify 认的是 cookie + X-CSRF-Token 头 (access_token 是 HttpOnly, 前端读不到,
# 所以它不发 Authorization)。因此这里必须**给浏览器发 Set-Cookie**, 不能像
# Coze 那样只在上游注入 —— 前端还要自己从 csrf_token 里读值拼请求头。
EM="$DSH_AUTOLOGIN_EMAIL"
PW="$DSH_AUTOLOGIN_PASSWORD"
[ -n "$EM" ] && [ -n "$PW" ] || exit 0
API=http://127.0.0.1:5001/console/api
# 登录接口收 base64 的密码 (前端就是这么发的); setup 收明文。
B64=$(printf '%s' "$PW" | base64 | tr -d '\n')

# Dify 连错 5 次密码就把这个账号锁 24 小时 (LOGIN_LOCKOUT_DURATION, 键在 redis)。
# 单用户工作台里这把锁只会锁住我们自己 —— 能走到这个域的人早就过了我们那层
# 鉴权。所以每次启动先清掉它, 顺带也把用户自己手滑锁上的解开。
unlock() {
  printf 'AUTH %s\r\nDEL login_error_rate_limit:%s\r\nQUIT\r\n' \
    "$DSH_REDIS_PASSWORD" "$EM" | timeout 5 nc 127.0.0.1 6379 >/dev/null 2>&1
}


# 排查用的日志。此前整个脚本的输出都丢进 /dev/null, 出问题只能去翻 Dify 自己的
# 日志才知道我们这边干了什么 —— 两次都是这么排的, 太贵。落在 NAS 上, 只留末尾。
LOGF=/root/.dify-autologin.log
log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOGF" 2>/dev/null
  tail -n 400 "$LOGF" > "$LOGF.t" 2>/dev/null && mv "$LOGF.t" "$LOGF" 2>/dev/null
}

# 带着刚拿到的会话调控制台接口。用法: api METHOD PATH [BODY]
api() {
  _m=$1; _p=$2; _b=$3
  if [ -n "$_b" ]; then
    curl -s -m 90 -X "$_m" -H 'Content-Type: application/json' \
      -H "Cookie: access_token=$AT; csrf_token=$CT" -H "X-CSRF-Token: $CT" \
      -d "$_b" "$API$_p"
  else
    curl -s -m 60 -X "$_m" \
      -H "Cookie: access_token=$AT; csrf_token=$CT" -H "X-CSRF-Token: $CT" "$API$_p"
  fi
}

# ---- 预置模型 (被 autologin 在登录成功后调用) -----------------------------
# Dify 的模型供应商是**插件**, 开箱一个都没装 —— 用户新建个聊天助手, 模板里
# 写的是 gpt-*, 于是当场报 "Provider langgenius/openai/openai does not exist"。
# 所以装一个 OpenAI 兼容插件, 把我们的网关配成自定义模型, 并设为默认。
#
# **在售的模型全部预置** (2026-08-31 老板定)。原先只配默认那一个, 理由是新建凭据
# 时 Dify 会真打一次上游做校验、真扣一次积分 (实测 llm 84 token / emb 49 token)。
# 那个理由仍然成立, 只是代价比"少了十九个模型"小得多:
#   · 每次校验不足百 token, 但每笔请求最少记 1 积分 -> 26 个模型约 26 积分。
#   · **一次性**: 凭据落在 NAS 上的 Postgres, 实例重建后还在; 刷新已存凭据的 PUT
#     实测**不**校验, 所以之后每次冷启动刷一遍都是免费的。
# 换来的是用户开箱就能在下拉框里选到我们卖的每一个模型 —— 而"在界面上点两下就能
# 加"这个说法低估了成本: 用户得自己知道网关地址、令牌、上下文长度那一堆字段。
#
# **能力参数只能一刀切**: 目录 (config/models.json) 和网关的 /models 都只给
# id/价格/厂商, 没有上下文长度、视觉、工具调用这些能力位 (2026-08-31 查过上游,
# 每条只有 id/object/created/owned_by)。所以这里对所有模型用同一套保守参数, 与
# 用户自己在界面上添加时拿到的默认值一致。代价是: 大上下文模型被压到 64k、视觉
# 模型收不了图。要改得先有能力元数据 —— 别在这里手写一张能力表, 它会像手写价目表
# 一样漂掉, 而漂掉的表现是运行时报错, 不是构建期报错。
PROV=langgenius/openai_api_compatible/openai_api_compatible
CREDS_URL="/workspaces/current/model-providers/$PROV/models/credentials"

cred_id() {  # cred_id <model> <model_type> -> 已存凭据的 id (没有则空)
  api GET "$CREDS_URL?model=$1&model_type=$2&config_from=custom-model" \
    | grep -o '"current_credential_id":"[^"]*"' | head -1 | sed 's/^[^:]*:"//; s/"$//'
}

# **每次启动都把令牌写一遍**, 不只是"没有模型时才建"。
# 工作台每次重建都会铸新令牌并撤销旧的 (workspace._mint_workspace_token 的安全
# 语义), 而 Dify 把令牌**存在自己库里** —— 只在缺失时写的话, 实例一回收重建,
# 它手里那枚就永远是废的, 表现是模型节点报
# `API request failed with status code 401 {"detail":"not_authenticated"}`,
# 而 Dify 侧一切正常。2026-08-30 老板撞上。
# (Coze 没这问题: 它的令牌走 env, 每次启动都是新的。)
# 第四个参数是 `default` 时才把它设成工作区默认模型。**必须按模型区分**: 早先
# 版本每建一个新模型就设一次默认, 那时只配一个模型看不出问题 —— 一旦循环预置
# 二十个, 默认会被最后一个建成的顶掉, 而那是按目录顺序排的随便哪一个。
ensure_model() {  # ensure_model <model> <model_type> <credentials-json> [default]
  CID=$(cred_id "$1" "$2")
  if [ -n "$CID" ]; then
    OUT=$(api PUT "$CREDS_URL" \
      "{\"credential_id\":\"$CID\",\"model\":\"$1\",\"model_type\":\"$2\",\"name\":\"DSH Cloud\",\"credentials\":$3}")
  else
    OUT=$(api POST "$CREDS_URL" \
      "{\"model\":\"$1\",\"model_type\":\"$2\",\"name\":\"DSH Cloud\",\"credentials\":$3}")
    if [ "$4" = default ] && echo "$OUT" | grep -q '"result":"success"'; then
      api POST "/workspaces/current/default-model" \
        "{\"model_settings\":[{\"model_type\":\"$2\",\"provider\":\"$PROV\",\"model\":\"$1\"}]}" >/dev/null
    fi
  fi
  if echo "$OUT" | grep -q '"result":"success"'; then
    return 0
  fi
  log "写凭据失败 $1/$2: $(echo "$OUT" | head -c 160)"
  return 1
}

llm_creds() {
  printf '{"api_key":"%s","endpoint_url":"%s","mode":"chat","context_size":"65536","max_tokens_to_sample":"8192","function_calling_type":"tool_call","stream_function_calling":"supported","agent_thought_support":"supported","vision_support":"no_support"}' \
    "$DSH_CLOUD_TOKEN" "$DSH_GATEWAY_BASE"
}
emb_creds() {
  printf '{"api_key":"%s","endpoint_url":"%s","context_size":"8192","max_chunks":"32"}' \
    "$DSH_CLOUD_TOKEN" "$DSH_GATEWAY_BASE"
}

provision() {
  # 供应商不在才装插件 (装一次就够, 配置在 NAS 上的 Postgres, 实例重建后还在)。
  if ! api GET "/workspaces/current/model-providers" | grep -q openai_api_compatible; then
    PID=$(curl -s -m 20 "https://marketplace.dify.ai/api/v1/plugins/langgenius/openai_api_compatible" \
          | tr ',' '\n' | grep -o '"latest_package_identifier":"[^"]*"' | head -1 \
          | sed 's/^[^:]*:"//; s/"$//')
    [ -n "$PID" ] || { log "取不到插件标识"; return 1; }
    TASK=$(api POST "/workspaces/current/plugin/install/marketplace" \
           "{\"plugin_unique_identifiers\":[\"$PID\"]}" \
           | grep -o '"task_id":"[^"]*"' | sed 's/^[^:]*:"//; s/"$//')
    [ -n "$TASK" ] || { log "装插件没拿到 task_id"; return 1; }
    n=0
    while [ "$n" -lt 40 ]; do
      n=$((n + 1))
      S=$(api GET "/workspaces/current/plugin/tasks/$TASK" | grep -o '"status":"[a-z]*"' | tail -1)
      case "$S" in *success*) break ;; *failed*) log "装插件失败"; return 1 ;; esac
      sleep 5
    done
  fi

  # **要重试**: 容器组刚起来时插件运行时还没加载完, 写凭据会被插件守护进程顶回来
  # (`PluginDaemonInternalServerError: no available node, plugin runtime not found`)。
  # 一次就放弃的话, 凭据里留着上一枚**已被撤销**的令牌, 用户点开就是
  # `401 not_authenticated`, 而 Dify 侧看着一切正常。2026-08-30 老板撞上两次。
  #
  # **只有这两个默认模型走重试**, 其余的等它们成了再配: 重试要的是"等插件运行时
  # 加载完", 那是整个供应商一次性的状态 —— 默认模型写成功就说明运行时活了, 再让
  # 另外二十几个各自重试三十轮只会把首次进入拖成十几分钟。
  LLM_OK=no; EMB_OK=no
  [ -n "$DSH_EMBEDDING_MODEL" ] || EMB_OK=yes
  n=0
  while [ "$n" -lt 30 ]; do
    n=$((n + 1))
    if [ "$LLM_OK" = no ] && ensure_model "$DSH_DEFAULT_MODEL" llm "$(llm_creds)" default; then LLM_OK=yes; fi
    if [ "$EMB_OK" = no ] && ensure_model "$DSH_EMBEDDING_MODEL" text-embedding "$(emb_creds)" default; then EMB_OK=yes; fi
    if [ "$LLM_OK" = yes ] && [ "$EMB_OK" = yes ]; then break; fi
    sleep 10
  done
  if [ "$LLM_OK" = no ] || [ "$EMB_OK" = no ]; then
    log "默认模型凭据没配上 llm=$LLM_OK emb=$EMB_OK"
    return 1
  fi
  log "默认模型凭据已就绪 (第 $n 轮), 开始预置其余在售模型"

  # 其余模型**尽力而为**: 一个失败不拖累其他, 也不影响 provision 的成败。
  # 目录里下架某个模型后, 它在 Dify 里留着的那份凭据我们不主动删 —— 用户可能已经
  # 在应用里引用了它, 静默删掉会让那个应用当场报模型不存在; 让他自己在界面上处理。
  REST=0; FAIL=0
  for M in $DSH_MODELS; do
    [ "$M" = "$DSH_DEFAULT_MODEL" ] && continue
    if ensure_model "$M" llm "$(llm_creds)"; then REST=$((REST + 1)); else FAIL=$((FAIL + 1)); fi
  done
  for M in $DSH_EMBEDDING_MODELS; do
    [ "$M" = "$DSH_EMBEDDING_MODEL" ] && continue
    if ensure_model "$M" text-embedding "$(emb_creds)"; then REST=$((REST + 1)); else FAIL=$((FAIL + 1)); fi
  done
  log "其余模型预置完成: 成功 $REST 个, 失败 $FAIL 个"
  return 0
}

write_conf() {
  cat > /etc/nginx/conf.d/00-autologin.conf <<EOF
# **每次页面加载都补发**, 而不是"只在 cookie 不存在时补"。
# 后者有个致命洞: 浏览器手里那份一旦失效 (账号改过密码、会话被服务端作废),
# 它仍然"存在", 于是永远补不上新的 —— 用户就被永久钉在 Dify 自己的登录页,
# 而清 cookie 之前怎么刷新都没用。2026-08-30 上线当天就这么锁住了老板。
# 工作台只有一个账号, 不存在"别人的会话"要保住, 所以覆盖是安全的。
#
# 按响应类型收窄到 HTML 文档: 静态资源和接口响应不必背这三个头。
map \$sent_http_content_type \$dsh_at {
    ~*^text/html  "access_token=$AT; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax";
    default       "";
}
map \$sent_http_content_type \$dsh_rt {
    ~*^text/html  "refresh_token=$RT; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax";
    default       "";
}
map \$sent_http_content_type \$dsh_ct {
    ~*^text/html  "csrf_token=$CT; Path=/; Max-Age=3600; SameSite=Lax";
    default       "";
}
# **上游方向也要注入。** 只发 Set-Cookie 是不够的: 浏览器第一发 / 手里还没有
# cookie, 而 Dify 的 / 会 307 到 /auth/refresh, 那一跳认不出他就 303 到
# /signin —— 整条链在**同一次访问里**走完, Set-Cookie 根本来不及生效。
# 用户看到的是 Next.js 的错误页 (`渲染此组件时发生了意外错误`), 而所有容器
# Running、所有接口 200, 从后端完全看不出问题。
# 2026-08-31 scripts/visual_check.sh 第一次全量跑时抓到。
#
# Coze 与 Hermes 早就这么做了 (proxy_set_header Cookie), 是我当时没把这条推广
# 到 Dify —— 它那次我只修了"无条件补发"这一半。
#
# 无条件覆盖, 判据同上: 该判的是"能不能用", 不是"有没有"。
map \$http_cookie \$dsh_up {
    default "access_token=$AT; refresh_token=$RT; csrf_token=$CT";
}
EOF
  nginx -s reload
}

tries=0
while :; do
  unlock
  CODE=$(curl -s -o /tmp/.dify-login -w '%{http_code}' -m 10 -X POST \
         -H 'Content-Type: application/json' \
         -d "{\"email\":\"$EM\",\"password\":\"$B64\",\"language\":\"zh-Hans\",\"remember_me\":true}" \
         -D /tmp/.dify-hdr "$API/login")
  AT=$(grep -i '^set-cookie: access_token=' /tmp/.dify-hdr 2>/dev/null | head -1 | sed 's/.*access_token=//; s/;.*//' | tr -d '\r')
  RT=$(grep -i '^set-cookie: refresh_token=' /tmp/.dify-hdr 2>/dev/null | head -1 | sed 's/.*refresh_token=//; s/;.*//' | tr -d '\r')
  CT=$(grep -i '^set-cookie: csrf_token=' /tmp/.dify-hdr 2>/dev/null | head -1 | sed 's/.*csrf_token=//; s/;.*//' | tr -d '\r')
  if [ -n "$AT" ] && [ -n "$RT" ] && [ -n "$CT" ]; then
    write_conf
    provision || true
    tries=0
    # access_token 只活 1 小时, 而容器能连着跑很久 —— 定期重登刷新, 否则新开的
    # 标签页会拿到一个早就过期的 token。
    sleep 1200
    continue
  fi
  # 000/5xx = api 还没起来, 这是常态, 快重试。
  # 4xx = 它答了但不认 —— 密码不对之类, 再快也没用, **而且会把账号锁掉**
  # (2026-08-30 就是这么把老板的 Dify 锁了 24 小时的)。退到慢档, 并且只在头
  # 几次尝试 setup: 那个接口一辈子只能成功一次。
  case "$CODE" in
    000|5*)  sleep 3 ;;
    *)
      tries=$((tries + 1))
      if [ "$tries" -le 3 ]; then
        curl -s -o /dev/null -m 10 -X POST -H 'Content-Type: application/json' \
          -d "{\"email\":\"$EM\",\"name\":\"owner\",\"password\":\"$PW\"}" "$API/setup" || true
        sleep 5
      else
        sleep 120
      fi
      ;;
  esac
done
"""


def _dify_boot() -> str:
    """主容器是 nginx。**配置我们自己生成** —— 官方那份用 `set $up api:5001` +
    `resolver 127.0.0.11` (Docker 内嵌 DNS), 而 nginx 的 resolver 不读
    /etc/hosts, host_aliases 兜不住; upstream 写死回环就绕开了整个解析环节。
    """
    return (
        "set -e\n"
        # 免登录 (见 _DIFY_AUTOLOGIN)。先落一份**空**的默认值 —— 会话要等 api
        # 起来才拿得到, 而 nginx 现在就要能起; 引用未定义的变量它会直接启动失败。
        # 变量取空串时 nginx 不会发出这个响应头, 所以空值就是"什么都不做"。
        "cat > /etc/nginx/conf.d/00-autologin.conf <<'AUTOCONF'\n"
        'map $sent_http_content_type $dsh_at { default ""; }\n'
        'map $sent_http_content_type $dsh_rt { default ""; }\n'
        'map $sent_http_content_type $dsh_ct { default ""; }\n'
        # 上游方向的默认值: 原样透传浏览器自己的 cookie。等 autologin 拿到会话
        # 后会把它改成"无条件用工作台那份"。
        "map $http_cookie $dsh_up { default $http_cookie; }\n"
        "AUTOCONF\n"
        "cat > /usr/local/bin/dsh-dify-autologin <<'AUTOLOGIN'\n" + _DIFY_AUTOLOGIN + "AUTOLOGIN\n"
        "chmod +x /usr/local/bin/dsh-dify-autologin\n"
        "/usr/local/bin/dsh-dify-autologin >/dev/null 2>&1 &\n"
        "cat > /etc/nginx/conf.d/default.conf <<'NGINXCONF'\n"
        "server {\n"
        "  listen 80;\n"
        "  server_name _;\n"
        "  client_max_body_size 100m;\n"
        # 放在 server 层: 这份配置里没有别的 add_header, 所以各 location 都继承
        # 得到 (nginx 的 add_header 一旦在子层出现就会丢掉父层的, 这里没有子层的)。
        "  add_header Set-Cookie $dsh_at always;\n"
        "  add_header Set-Cookie $dsh_rt always;\n"
        "  add_header Set-Cookie $dsh_ct always;\n"
        # 见 _DIFY_AUTOLOGIN 里那段注释: 光发 Set-Cookie 不够, 浏览器第一发
        # 手里还没有 cookie, 而 / → /auth/refresh → /signin 这条链在同一次访问
        # 里就走完了, Set-Cookie 来不及生效, 用户看到 Next.js 的错误页。
        "  proxy_set_header Cookie $dsh_up;\n"
        "  proxy_set_header Host $host;\n"
        "  proxy_set_header X-Real-IP $remote_addr;\n"
        "  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "  proxy_set_header X-Forwarded-Proto $scheme;\n"
        "  proxy_http_version 1.1;\n"
        "  proxy_read_timeout 3600s;\n"
        "  proxy_send_timeout 3600s;\n"
        "  location /console/api { proxy_pass http://127.0.0.1:5001; }\n"
        "  location /api         { proxy_pass http://127.0.0.1:5001; }\n"
        "  location /v1          { proxy_pass http://127.0.0.1:5001; }\n"
        "  location /openapi     { proxy_pass http://127.0.0.1:5001; }\n"
        "  location /files       { proxy_pass http://127.0.0.1:5001; }\n"
        "  location /mcp         { proxy_pass http://127.0.0.1:5001; }\n"
        "  location /triggers    { proxy_pass http://127.0.0.1:5001; }\n"
        "  location /e/ { proxy_pass http://127.0.0.1:5002; "
        "proxy_set_header Dify-Hook-Url $scheme://$host$request_uri; }\n"
        "  location /socket.io/ { proxy_pass http://127.0.0.1:5011; "
        'proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; }\n'
        "  location /explore { proxy_pass http://127.0.0.1:3000; }\n"
        "  location / { proxy_pass http://127.0.0.1:3000; }\n"
        "}\n"
        "NGINXCONF\n"
        "exec nginx -g 'daemon off;'\n"
    )


_COZE_BUCKET = "opencoze"
_COZE_MILVUS_BUCKET = "milvus"

# 各中间件的启动命令。都与上游 compose 的 entrypoint/command 同构, 只改两类地方:
#   · bind mount -> 种子卷 (/seed)
#   · compose 服务名里那些**只在容器内用**的地址 -> 127.0.0.1
# 保持同构是有意的: 上游升级时能逐行对照, 而不是重新推导一遍。
_COZE_MYSQL_CMD = (
    "set -e\n"
    "cp /seed/mysql/schema.sql /docker-entrypoint-initdb.d/init.sql\n"
    "/usr/local/bin/docker-entrypoint.sh mysqld"
    " --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci &\n"
    "pid=$!\n"
    'until mysqladmin ping -h 127.0.0.1 -u root -p"$MYSQL_ROOT_PASSWORD" --silent 2>/dev/null;'
    " do sleep 2; done\n"
    # 建库 SQL 跑完的标志。atlas 要在这之后才能对齐 schema —— 提前跑会把一个
    # 空库"对齐"成完整 schema, 然后 initdb 的 SQL 再撞上去。
    'until mysql -h 127.0.0.1 -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'
    " -e \"SHOW TABLES LIKE 'workflow_version';\" 2>/dev/null | grep -q workflow_version;"
    " do sleep 2; done\n"
    "/seed/bin/atlas schema apply"
    ' -u "mysql://$MYSQL_USER:$MYSQL_PASSWORD@127.0.0.1:3306/$MYSQL_DATABASE"'
    " --to file:///seed/atlas/opencoze_latest_schema.hcl"
    " --exclude 'atlas_schema_revisions,table_*' --auto-approve\n"
    "wait $pid\n"
)

_COZE_REDIS_CMD = (
    "/opt/bitnami/scripts/redis/setup.sh\n"
    "chown -R redis:redis /bitnami/redis/data\n"
    "chmod g+s /bitnami/redis/data\n"
    "exec /opt/bitnami/scripts/redis/entrypoint.sh /opt/bitnami/scripts/redis/run.sh\n"
)

_COZE_ETCD_CMD = (
    "/opt/bitnami/scripts/etcd/setup.sh\n"
    "chown -R etcd:etcd /bitnami/etcd\n"
    "chmod g+s /bitnami/etcd\n"
    "exec /opt/bitnami/scripts/etcd/entrypoint.sh /opt/bitnami/scripts/etcd/run.sh\n"
)

# 装中文分词器 -> 后台等 ES 起来再建索引模板 -> 前台跑 ES。
# 分词器装不上就直接退出: 装了一半的插件目录会让 ES 每次启动都失败, 而错误在
# Java 栈里, 看不出是我们铺的那个 zip 有问题。
_COZE_ES_CMD = (
    "set -e\n"
    "cp /seed/elasticsearch/elasticsearch.yml"
    " /opt/bitnami/elasticsearch/config/my_elasticsearch.yml\n"
    "/opt/bitnami/scripts/elasticsearch/setup.sh\n"
    "chown -R elasticsearch:elasticsearch /bitnami/elasticsearch/data\n"
    "chmod g+s /bitnami/elasticsearch/data\n"
    "mkdir -p /bitnami/elasticsearch/plugins\n"
    "if [ ! -d /opt/bitnami/elasticsearch/plugins/analysis-smartcn ]; then\n"
    "  cp /seed/elasticsearch/analysis-smartcn.zip /tmp/smartcn.zip\n"
    "  elasticsearch-plugin install file:///tmp/smartcn.zip || {"
    " rm -rf /opt/bitnami/elasticsearch/plugins/analysis-smartcn; exit 1; }\n"
    "  rm -f /tmp/smartcn.zip\n"
    "fi\n"
    "(\n"
    "  until curl -sf http://127.0.0.1:9200/_cat/health >/dev/null 2>&1; do sleep 2; done\n"
    # 上游的 setup_es.sh 带 CRLF, bash 会把 \r 当成命令的一部分 —— 上游自己也
    # 在 compose 里 sed 掉, 照搬。
    "  sed 's/\\r$//' /seed/elasticsearch/setup_es.sh > /tmp/setup_es.sh\n"
    "  chmod +x /tmp/setup_es.sh\n"
    # **必须显式传 --es-address**: 脚本自己没有默认值, 它指望 compose 的 env_file
    # 给每个容器都塞一份 .env (里面有 ES_ADDR) —— 我们只给 coze-server 发了这个
    # 变量, 所以这里 ES_ADDR 是空串。空地址探测 60 次全失败后, 它打印的是
    # "smartcn plugin not loaded correctly" —— **报错完全指错方向**, 而真正的
    # 后果是索引一个都没建, 用户一进工作区就是 500 (no such index [project_draft])。
    # --docker-host false 顺带关掉它那条 "localhost -> http://elasticsearch:9200"
    # 的改写: 我们走回环, 不该再绕一次名字解析。
    "  /tmp/setup_es.sh --es-address http://127.0.0.1:9200 --docker-host false"
    " --index-dir /seed/elasticsearch/es_index_schema\n"
    ") &\n"
    "exec /opt/bitnami/scripts/elasticsearch/entrypoint.sh"
    " /opt/bitnami/scripts/elasticsearch/run.sh\n"
)

# 后台建桶并灌图标, 前台跑 minio。图标不灌的话平台内置的智能体/插件/工作流
# 全是碎图 —— 不报错, 只是难看到没法交付。
_COZE_MINIO_CMD = (
    "(\n"
    "  until /usr/bin/mc alias set local http://127.0.0.1:9000"
    ' "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do sleep 1; done\n'
    '  /usr/bin/mc mb --ignore-existing local/"$STORAGE_BUCKET"\n'
    '  /usr/bin/mc mb --ignore-existing local/"$MILVUS_BUCKET"\n'
    '  /usr/bin/mc cp --recursive /seed/default_icon/ local/"$STORAGE_BUCKET"/default_icon/\n'
    "  /usr/bin/mc cp --recursive /seed/official_plugin_icon/"
    ' local/"$STORAGE_BUCKET"/official_plugin_icon/\n'
    ") &\n"
    "exec minio server /data --console-address :9001\n"
)


# ---- Coze Studio (Agent/工作流搭建, 10 容器栈) -------------------------------
#
# 与 Dify 的差别在于**它靠 bind mount 送配置**: 上游 compose 有 17 处 —— 建库
# SQL、atlas schema、ES 的中文分词器与索引模板、MinIO 的图标、nginx 配置、后端的
# model/plugin/prompt 配置目录。ECI 没有 bind mount, 所以走初始化容器 + 种子卷
# (见 InitContainer), 资产烤在 deploy/workspace-coze 建的镜像里。
#
# 组内地址一律用**上游的服务名**而不是 127.0.0.1, 由 host_aliases 指回环。看着
# 绕, 但有一处非如此不可: nginx 用 `sub_filter 'minio:9000' '$http_host/local_storage'`
# 把后端返回的对象存储直链改写成同源路径。MINIO_ENDPOINT 写成 127.0.0.1:9000
# 的话改写就不匹配 —— 后果是**页面上的图片和附件全部打不开**, 而每个容器自己
# 看都正常, 日志里一个错都没有。既然有一处必须用服务名, 其余就都用, 少一条
# "这里为什么不一样"的规则。
_COZE_DB = "opencoze"
_COZE_DB_USER = "coze"
_COZE_DB_PASSWORD = "coze123"
_COZE_DB_ROOT_PASSWORD = "root"
_COZE_MINIO_USER = "minioadmin"
_COZE_MINIO_PASSWORD = "minioadmin123"
# 上面这些是**组内回环**上的凭据: 库、缓存、对象存储、向量库都只在容器组内可达,
# 跨用户隔离靠网络边界与 NAS 子路径, 不靠这几个常量 (与 Dify 同一判断)。
# 下面三个不一样 —— 它们加密的是**落库的用户数据** (插件的 OAuth 令牌),
# 所以按用户推导。AES-128, 必须正好 16 字节, 于是走 16 字节那个占位符。
_COZE_AES_ENV = (
    ("PLUGIN_AES_AUTH_SECRET", STACK_SECRET16_PLACEHOLDER),
    ("PLUGIN_AES_STATE_SECRET", STACK_SECRET16_PLACEHOLDER),
    ("PLUGIN_AES_OAUTH_TOKEN_SECRET", STACK_SECRET16_PLACEHOLDER),
)


def _coze_embedding_env(gateway: str) -> tuple[tuple[str, str], ...]:
    """知识库的向量化配置 —— 指回我们自己的 /llm/v1/embeddings。

    没有它, Coze 的知识库要用户自己去第三方申请一把 key, 而他已经在我们这里
    付过费了。上游开箱的那套是 `ark` + 空 key: 服务照常起, 只是知识库不可用。
    目录里一个向量化模型都没有时 (自建部署带着旧的 models.json) 仍退回那套,
    因为把 OPENAI_EMBEDDING_MODEL 配成空串会让知识库在**运行期**才炸。

    REQUEST_DIMS 必须跟着模型走: 它为真时 Coze 每次都带上 dimensions, 而
    BGE-M3 那类模型的上游**拒收**这个参数 (网关会回 400)。写死成真, 换个默认
    模型就是知识库整个不能用, 而这里一个字都不会变。
    """
    model_id = model_catalog.default_embedding_model()
    entry = model_catalog.resolve_embedding(model_id)
    if entry is None:
        return (("EMBEDDING_TYPE", "ark"),)
    return (
        ("EMBEDDING_TYPE", "openai"),
        ("OPENAI_EMBEDDING_BASE_URL", f"{gateway}/llm/v1"),
        ("OPENAI_EMBEDDING_API_KEY", GATEWAY_TOKEN_PLACEHOLDER),
        ("OPENAI_EMBEDDING_MODEL", model_id),
        ("OPENAI_EMBEDDING_DIMS", str(entry["dimensions"])),
        ("OPENAI_EMBEDDING_REQUEST_DIMS", "true" if entry.get("supports_dimensions") else "false"),
    )


# 下面这一段是**表**, 不是代码块: 相关的键成对排在一行上读。交给 formatter
# 会拆成一项一行, 分组关系随之消失。
# fmt: off
def _coze_server_env() -> tuple[tuple[str, str], ...]:
    """coze-server 的环境。

    上游把配置全放在 `.env` 里由 compose 的 env_file 读; ECI 这边直接发
    EnvironmentVar。一般只写**非空的与选类型的**, 第三方厂商的空 key 不用逐条搬。

    但**"不写"不等于"空"**: 上游镜像里烤了一份 `/app/.env` (它的 Dockerfile 有
    `COPY docker/.env.example /app/.env`), 后端启动时 `godotenv.Load` 会把我们
    **没设过**的键按那份文件补上 (它只补 os.Environ() 里没出现过的键)。所以凡是
    上游默认值非空、而我们要的语义是"关掉"的键, 必须**显式写成空串** —— 见下面
    的 MODEL_PROTOCOL_0, 那一条真咬过人。
    """
    gateway = config.PUBLIC_BASE.rstrip("/")
    dsn = (
        f"{_COZE_DB_USER}:{_COZE_DB_PASSWORD}@tcp(mysql:3306)/{_COZE_DB}"
        "?charset=utf8mb4&parseTime=True"
    )
    # 只剩内建模型 (平台起标题/扩写那类) 用它; 用户可选的那批走 _coze_model_yamls。
    model_id = model_catalog.default_model()
    return (
        ("LISTEN_ADDR", ":8888"),
        ("LOG_LEVEL", "info"),
        ("MAX_REQUEST_BODY_SIZE", "1073741824"),
        # 前端拼回调链接用它。写成 localhost 的话分享出来的链接指向用户自己的
        # 机器 —— 打不开, 而且看不出为什么。
        ("SERVER_HOST", f"https://{config.COZE_DOMAIN}" if config.COZE_DOMAIN else ""),
        ("USE_SSL", "0"),
        ("MYSQL_HOST", "mysql"), ("MYSQL_PORT", "3306"),
        ("MYSQL_USER", _COZE_DB_USER), ("MYSQL_PASSWORD", _COZE_DB_PASSWORD),
        ("MYSQL_DATABASE", _COZE_DB), ("MYSQL_ROOT_PASSWORD", _COZE_DB_ROOT_PASSWORD),
        ("MYSQL_DSN", dsn),
        ("MYSQL_MAX_IDLE_CONNS", "10"), ("MYSQL_MAX_OPEN_CONNS", "100"),
        ("MYSQL_CONN_MAX_LIFETIME", "3600"), ("MYSQL_CONN_MAX_IDLE_TIME", "600"),
        ("REDIS_ADDR", "redis:6379"), ("REDIS_PASSWORD", ""),
        ("FILE_UPLOAD_COMPONENT_TYPE", "storage"),
        ("STORAGE_TYPE", "minio"),
        # 站点是 https, 这里跟着写 https, 否则前端拿到 http 直链会被浏览器拦成
        # 混合内容 —— 表现是图片空白, 控制台里才有一行 blocked。
        ("STORAGE_UPLOAD_HTTP_SCHEME", "https"),
        ("STORAGE_BUCKET", _COZE_BUCKET),
        ("MINIO_AK", _COZE_MINIO_USER), ("MINIO_SK", _COZE_MINIO_PASSWORD),
        # 见本节开头: 这两个**必须**是服务名, nginx 的 sub_filter 认的就是它。
        ("MINIO_ENDPOINT", "minio:9000"), ("MINIO_API_HOST", "http://minio:9000"),
        ("MINIO_USE_SSL", "false"),
        ("ES_ADDR", "http://elasticsearch:9200"), ("ES_VERSION", "v8"),
        ("ES_USERNAME", ""), ("ES_PASSWORD", ""),
        ("ES_NUMBER_OF_SHARDS", "1"), ("ES_NUMBER_OF_REPLICAS", "0"),
        ("COZE_MQ_TYPE", "nsq"), ("MQ_NAME_SERVER", "nsqd:4150"),
        ("VECTOR_STORE_TYPE", "milvus"), ("MILVUS_ADDR", "milvus:19530"),
        # 一批 100 条实测上游吃得下 (2026-08-29), usage 是整批合计的。
        ("EMBEDDING_MAX_BATCH_SIZE", "100"),
        # 重排仍用 rrf (纯本地的倒数排序融合, 不调任何模型): 重排不是 OpenAI
        # 的标准端点, 网关也还没有这一支 —— 上游确实在售 bge-reranker, 要接是
        # 另一件事, 不是这次顺手能带的。
        ("RERANK_TYPE", "rrf"),
        ("OCR_TYPE", ""), ("PARSER_TYPE", "builtin"),
        # 开箱就有模型可选 —— 用户已经在我们这里付过费, 不该再去别处申请 key。
        # **在售模型全部由 YAML 出** (见 _coze_model_yamls), env 那条路径在这里
        # **显式关掉**。
        #
        # 关掉要写空串, 不能只是"不配": 镜像里那份 /app/.env (见上面的 docstring)
        # 写着 MODEL_PROTOCOL_0="ark" 和 MODEL_OPENCOZE_ID_0="100001", 我们不设
        # 它就由上游的默认值顶上, initModelByEnv 照样返回一条 —— 无名
        # (MODEL_NAME_0 也是空的)、class 是 SEED (ark 映射过去的)、id 还正好撞上
        # 我们默认模型的 100001, 追加在 20 条 YAML 之后。表现就是下拉框末尾多出
        # 一条**空白**条目。initModelByEnv 的门槛是
        # `MODEL_PROTOCOL_0 == "" || MODEL_OPENCOZE_ID_0 == ""`, 写空串才迈不过去。
        ("MODEL_PROTOCOL_0", ""), ("MODEL_OPENCOZE_ID_0", ""),
        # 内建的那个"平台自己用"的模型 (起标题、扩写这类) 也指过来。
        ("BUILTIN_CM_TYPE", "openai"),
        ("BUILTIN_CM_OPENAI_BASE_URL", f"{gateway}/llm/v1"),
        ("BUILTIN_CM_OPENAI_API_KEY", GATEWAY_TOKEN_PLACEHOLDER),
        ("BUILTIN_CM_OPENAI_MODEL", model_id),
        ("BUILTIN_CM_OPENAI_BY_AZURE", "false"),
    ) + _coze_embedding_env(gateway) + _COZE_AES_ENV
# fmt: on


# 下面这一段是**表**, 不是代码块: 相关的键成对排在一行上读。交给 formatter
# 会拆成一项一行, 分组关系随之消失。
# fmt: off
def _coze_stack() -> tuple[Sidecar, ...]:
    v = config.COZE_VERSION
    return (
        # --- 应用 ---------------------------------------------------------
        Sidecar(
            name="coze-server",
            image_ref=f"cozedev/coze-studio-server:{v}",
            env=_coze_server_env(),
            # 后端的 model/plugin/prompt 配置目录。上游 compose 是 bind mount
            # ../backend/conf; 这里用子路径直接把种子卷的 conf 挂过去 —— 不经
            # shell, 也就不依赖这个镜像里有没有 sh。
            seeds=(("conf", "/app/resources/conf"),),
        ),
        # --- 中间件 -------------------------------------------------------
        Sidecar(
            name="mysql", image_ref="mysql:8.4.5",
            env=(
                ("MYSQL_ROOT_PASSWORD", _COZE_DB_ROOT_PASSWORD),
                ("MYSQL_DATABASE", _COZE_DB),
                ("MYSQL_USER", _COZE_DB_USER),
                ("MYSQL_PASSWORD", _COZE_DB_PASSWORD),
            ),
            # 先跑建库 SQL, 起来后再用 atlas 把 schema 对齐到当前版本 —— 与上游
            # 的 entrypoint 同构, 只有两处改动:
            #   · atlas 从种子卷取, 不再启动时 `curl atlasgo.sh | sh` 现装
            #     (外网一抖就是"库起来了但没 schema", 而报错出现在 coze-server)
            #   · 建库 SQL 从种子卷拷进 initdb 目录 (没有 bind mount)
            cmd=("bash", "-c", _COZE_MYSQL_CMD),
            mounts=(("coze/mysql", "/var/lib/mysql"),),
            seeds=(("", "/seed"),),
            run_as_user=0,
        ),
        Sidecar(
            name="redis", image_ref="bitnamilegacy/redis:8.0",
            env=(("ALLOW_EMPTY_PASSWORD", "yes"), ("REDIS_AOF_ENABLED", "no"),
                 ("REDIS_IO_THREADS", "4"), ("REDIS_PORT_NUMBER", "6379")),
            cmd=("bash", "-c", _COZE_REDIS_CMD),
            mounts=(("coze/redis", "/bitnami/redis/data"),),
            run_as_user=0,
        ),
        Sidecar(
            name="elasticsearch", image_ref="bitnamilegacy/elasticsearch:8.18.0",
            # 不限堆的话 ES 按宿主内存的一半自取, 在 12GiB 的组里就是 6GiB ——
            # Milvus 和 MySQL 会被挤死, 而症状是"过一会儿容器莫名重启"。
            env=(("ES_JAVA_OPTS", "-Xms1g -Xmx1g"),),
            cmd=("bash", "-c", _COZE_ES_CMD),
            mounts=(("coze/es", "/bitnami/elasticsearch/data"),),
            seeds=(("", "/seed"),),
            run_as_user=0,
        ),
        Sidecar(
            name="minio", image_ref="minio/minio:RELEASE.2025-06-13T11-33-47Z-cpuv1",
            env=(("MINIO_ROOT_USER", _COZE_MINIO_USER),
                 ("MINIO_ROOT_PASSWORD", _COZE_MINIO_PASSWORD),
                 ("STORAGE_BUCKET", _COZE_BUCKET),
                 ("MILVUS_BUCKET", _COZE_MILVUS_BUCKET)),
            cmd=("sh", "-c", _COZE_MINIO_CMD),
            mounts=(("coze/minio", "/data"),),
            seeds=(("minio", "/seed"),),
            run_as_user=0,
        ),
        Sidecar(
            name="etcd", image_ref="bitnamilegacy/etcd:3.5",
            env=(("ALLOW_NONE_AUTHENTICATION", "yes"),
                 ("ETCD_AUTO_COMPACTION_MODE", "revision"),
                 ("ETCD_AUTO_COMPACTION_RETENTION", "1000"),
                 ("ETCD_QUOTA_BACKEND_BYTES", "4294967296")),
            # 上游还挂了个 etcd.conf.yml, 但那是个**空文件** —— 不铺, 少一个
            # 挂载点就少一处能出错的地方。
            cmd=("bash", "-c", _COZE_ETCD_CMD),
            mounts=(("coze/etcd", "/bitnami/etcd"),),
            run_as_user=0,
        ),
        Sidecar(
            name="milvus", image_ref="milvusdb/milvus:v2.5.10",
            env=(("ETCD_ENDPOINTS", "etcd:2379"), ("MINIO_ADDRESS", "minio:9000"),
                 ("MINIO_BUCKET_NAME", _COZE_MILVUS_BUCKET),
                 ("MINIO_ACCESS_KEY_ID", _COZE_MINIO_USER),
                 ("MINIO_SECRET_ACCESS_KEY", _COZE_MINIO_PASSWORD),
                 ("MINIO_USE_SSL", "false"), ("LOG_LEVEL", "warn")),
            cmd=("bash", "-c", "chown -R root:root /var/lib/milvus; exec milvus run standalone"),
            mounts=(("coze/milvus", "/var/lib/milvus"),),
            run_as_user=0,
        ),
        Sidecar(name="nsqlookupd", image_ref="nsqio/nsq:v1.2.1", cmd=("/nsqlookupd",)),
        Sidecar(name="nsqd", image_ref="nsqio/nsq:v1.2.1",
                cmd=("/nsqd", "--lookupd-tcp-address=127.0.0.1:4160",
                     "--broadcast-address=127.0.0.1")),
        # nsqadmin 不起: 它只是 nsq 的管理页, 对用户不可见, 而每个容器都要钱。
    )


#: 上游 nginx 配置里那条把对象存储直链改写成同源路径的规则, 以及我们要换成的
#: 版本 (带 https)。放在这里而不是内联进启动脚本, 是为了 deploy/workspace-coze/
#: build.sh 能在**构建期**断言上游那行还长这样 —— 上游一改, 构建就红, 而不是
#: 等用户看到一片碎图。`\$http_host` 的反斜杠是给 shell 的: 启动脚本经 sh -c
#: 执行, 不转义会被当成空变量展开掉。
# fmt: on
_COZE_SUBFILTER_FROM = "sub_filter 'minio:9000' '\\$http_host/local_storage';"
_COZE_SUBFILTER_TO = "sub_filter 'http://minio:9000' 'https://\\$http_host/local_storage';"
#: 免登录那行 proxy_set_header 插在哪。这一行在整份上游配置里唯一 ——
#: /local_storage/ 那个 location 用的是 `Host minio:9000`, 匹配不上。
#: build.sh 在构建期断言它还在: 匹配不上是**静默失效**, 用户只会又看到登录墙。
_COZE_APIHOST_ANCHOR = "proxy_set_header Host \\$http_host;"


#: 工作台自己签发 Coze 的会话, 于是用户不再看到第二道登录墙。
#:
#: 平台的通则是**只有我们这一层登录墙**: 用户进到这个域已经过了 forward_auth,
#: 容器是他一个人的, 再让他注册一个第三方账号既多余又走不通 —— 密码是我们随机
#: 生成的, 他根本不知道。
#:
#: Coze 那边没有任何免登开关: SessionAuthMW 只认 session_key cookie ->
#: ValidateSession, 既没有受信头也没有匿名模式
#: (backend/api/middleware/session.go)。所以只能真登一次。
#:
#: 注入在**上游方向**而不是给浏览器发 Set-Cookie: 少依赖一层浏览器状态, 用户
#: 禁 cookie 也照样能用; 而他自己带了 session_key 时原样透传, 想切账号也切得了。
#:
#: 密码存在 /root (NAS, 跟着用户走) —— 实例重建后还是同一个账号, 里面的智能体
#: 和知识库都还在。账号用 owner@ 而不是 admin@: 后者可能已被人工建过而密码不在
#: 我们手里, 那样注册和登录会双双失败, 而且**不报错**, 只是又看到登录墙。
_COZE_AUTOLOGIN = r"""#!/bin/sh
# 由 products.py 下发。工作台自己登一次 Coze, 把会话注入到上游请求里。
PWF=/root/.coze-autologin
EM=owner@dshcloud.online
API=http://127.0.0.1:8888/api/passport/web/email
[ -s "$PWF" ] || {
  printf 'Dsh%s1a\n' "$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)" > "$PWF"
  chmod 600 "$PWF"
}
PW=$(cat "$PWF")
BODY="{\"email\":\"$EM\",\"password\":\"$PW\"}"
SK=""
i=0
while [ "$i" -lt 150 ]; do
  i=$((i + 1))
  SK=$(curl -s -D- -o /dev/null -m 10 -X POST -H 'Content-Type: application/json' \
        -d "$BODY" "$API/login/" \
       | grep -i '^set-cookie: session_key=' | head -1 \
       | sed 's/.*session_key=//; s/;.*//' | tr -d '\r')
  [ -n "$SK" ] && break
  # 登不上多半是账号还不存在 (或 coze-server 还没起来) —— 注册一次再试。
  curl -s -o /dev/null -m 10 -X POST -H 'Content-Type: application/json' \
       -d "$BODY" "$API/register/v2/" || true
  sleep 3
done
[ -n "$SK" ] || exit 0
cat > /etc/nginx/conf.d/00-autologin.conf <<EOF
# 浏览器没带 session_key 时注入工作台自己的那个; 带了就原样透传 (想切账号也切得了)。
map \$cookie_session_key \$dsh_cookie {
    ""      "session_key=$SK";
    default \$http_cookie;
}
EOF
nginx -s reload
"""


#: Coze 里模型条目的数字 id 起点。**默认模型必须保住 100001**: 早先只配一个模型时
#: 走的是 `MODEL_OPENCOZE_ID_0=100001` 那条 env 路径, 用户已建的智能体就是按这个 id
#: 引用模型的 —— 换个号等于让他所有智能体指向一个不存在的模型。
_COZE_MODEL_ID_BASE = 100001


def _coze_model_yamls() -> str:
    """把在售模型写成 Coze 的模型配置文件, 由初始化容器铺进种子卷。

    Coze 读模型有**两条**旧路径, 合起来用 (backend 的 deprecate_model_get.go):
    扫 `resources/conf/model/*.yaml`, 再把 `MODEL_*_0` 那组 env 追加一条。两条都
    走的话默认模型会**出现两次**, 所以 env 那条在 _coze_server_env 里**写空串
    关掉** (只是不配关不掉 —— 镜像里烤了一份 .env 会把默认值补回来), 全部由 YAML 出。

    上游镜像里那个目录只有 `template/` 子目录和一个 json —— readDirYaml 跳过目录、
    只认 .yaml, 所以那里原本一个模型都没有, 我们铺进去的就是全部。

    只写结构体认的四个字段 (id/name/description/meta)。模板里那一大段
    default_parameters 不在 OldModel 结构体里, yaml 解析直接忽略 —— 抄进来纯属
    给自己找漂移。

    **新界面存过模型就不再走这条路**: Coze 有个 `do_not_use_old_model_key` 开关,
    用户一旦在模型管理页里保存过, 旧配置整个失效, 这里铺的东西也就不再露面。那是
    上游的语义, 不是故障。
    """
    gateway = config.PUBLIC_BASE.rstrip("/")
    docs = []
    for i, m in enumerate(model_catalog.catalog().values()):
        mid = m["id"]
        name = m.get("display_name", mid)
        # 值一律加引号: 模型 id 带斜杠 (Qwen/...)、令牌是 base64url、显示名带括号,
        # 裸写进 YAML 迟早撞上某个被当成语法的字符, 而那会让 coze-server 启动即崩。
        doc = (
            f"id: {_COZE_MODEL_ID_BASE + i}\n"
            f'name: "{name} (DSH Cloud)"\n'
            "description:\n"
            '  zh: "由 DSH Cloud 提供，按积分计费"\n'
            '  en: "Provided by DSH Cloud"\n'
            "meta:\n"
            "  protocol: openai\n"
            "  conn_config:\n"
            f'    base_url: "{gateway}/llm/v1"\n'
            f'    api_key: "{GATEWAY_TOKEN_PLACEHOLDER}"\n'
            f'    model: "{mid}"\n'
        )
        # 文件名不能照抄模型 id —— 带斜杠的那几个会变成"写进不存在的子目录"。
        docs.append(f"cat > /seed/conf/model/dsh_{i:03d}.yaml <<'DSHMODEL'\n{doc}DSHMODEL\n")
    return "".join(docs)


def _coze_boot() -> str:
    """主容器是上游的 coze-web (nginx + 打好的前端静态资源)。

    配置**用上游那份**, 不像 Dify 那样自己生成: 它里面有两处不好手抄的东西 ——
    `sub_filter` 把后端返回的 minio 直链改写成同源路径, 以及 /local_storage/
    那几条剥离签名参数的 rewrite。抄错了不报错, 只是附件打不开。

    能直接用是因为它的 proxy_pass 写的是**静态**主机名 (coze-server / minio):
    nginx 只在启动时解析一次, 走 /etc/hosts, 于是 host_aliases 兜得住。
    (Dify 那份用的是 `resolver` + 变量式 proxy_pass, resolver 不读 /etc/hosts,
    所以只能自己生成 —— 见 _dify_boot。) build.sh 里有守卫盯着上游哪天改法。
    """
    return (
        "set -e\n"
        # /seed 挂的是种子卷里的 nginx **子目录**, 那一层已经剥掉了 ——
        # 写 /seed/nginx/nginx.conf 会 no such file, 而 nginx 压根不启动。
        "cp /seed/nginx.conf /etc/nginx/nginx.conf\n"
        "mkdir -p /etc/nginx/conf.d\n"
        "cp /seed/conf.d/default.conf /etc/nginx/conf.d/default.conf\n"
        # 唯一一处改上游配置: 对象存储直链要改写成 **https**。
        #
        # 后端 presign 出来的地址是 `http://minio:9000/...` —— scheme 取自
        # MINIO_USE_SSL, 而那个必须是 false (服务端走回环明文连 minio)。
        # STORAGE_UPLOAD_HTTP_SCHEME 管不到这里, 它只写进上传令牌的 HostScheme
        # (backend/infra/storage/impl/minio/minio_imagex.go 的 GetUploadAuth)。
        # 上游自己的部署是纯 http 的, 所以他们碰不到; 我们的站点在 https 上,
        # 页面里出现 http:// 的图片就是**混合内容**, 浏览器直接拦 —— 表现是
        # 头像和附件一片空白, 而服务端一切正常、控制台里才有一行 blocked。
        f'sed -i "s#{_COZE_SUBFILTER_FROM}#{_COZE_SUBFILTER_TO}#" '
        "/etc/nginx/conf.d/default.conf\n"
        # 免登录 (见 _COZE_AUTOLOGIN)。先落一份**透传**的默认值 —— 会话要等
        # coze-server 起来才拿得到, 而 nginx 现在就要能起; 引用一个还没定义的
        # 变量会让 nginx 直接启动失败, 那就连静态页都没有了。
        "cat > /etc/nginx/conf.d/00-autologin.conf <<'AUTOCONF'\n"
        "map $cookie_session_key $dsh_cookie { default $http_cookie; }\n"
        "AUTOCONF\n"
        f"sed -i '/{_COZE_APIHOST_ANCHOR}/a\\        proxy_set_header Cookie $dsh_cookie;'"
        " /etc/nginx/conf.d/default.conf\n"
        "cat > /usr/local/bin/dsh-coze-autologin <<'AUTOLOGIN'\n" + _COZE_AUTOLOGIN + "AUTOLOGIN\n"
        "chmod +x /usr/local/bin/dsh-coze-autologin\n"
        "/usr/local/bin/dsh-coze-autologin >/dev/null 2>&1 &\n"
        "exec nginx -g 'daemon off;'\n"
    )


# 下面这一段是**表**, 不是代码块: 相关的键成对排在一行上读。交给 formatter
# 会拆成一项一行, 分组关系随之消失。
# fmt: off
#: 反代注入的身份头。OpenClaw 的 trusted-proxy 鉴权认它, 值由 /api/work/route
#: 随 forward_auth 一起吐出来 (见 workspace.work_route)。
PROXY_USER_HEADER = "X-Dsh-User"


def _openclaw_boot() -> str:
    """写配置 -> 起网关。

    **用 `config patch` 而不是整份覆盖**: 这个文件里还装着用户自己接的频道
    (Telegram/Discord/Slack 那些), 每次启动重写一遍等于把他配的东西全抹掉。
    patch 是递归合并的, 只动我们这几个键。

    鉴权走 trusted-proxy 而不是关掉: OpenClaw 明确拒绝"监听 LAN + 无鉴权"
    (实测 `Refusing to bind gateway to lan without auth`), 而这个模式就是为
    "边缘已经鉴过权"设计的 —— 用户走到这个域已经过了我们的 forward_auth。
    身份头只认 WORK_PROXY_CIDR 那个来源, 放宽等于让任何能连到容器的人自称是
    任意用户。
    """
    import json as _json

    gateway = config.PUBLIC_BASE.rstrip("/")
    models = [
        {
            "id": m["id"],
            "name": m.get("display_name", m["id"]),
            "contextWindow": 65536,
            "maxTokens": 8192,
        }
        for m in model_catalog.catalog().values()
    ]
    patch = _json.dumps(
        {
            "gateway": {
                "mode": "local",
                "port": OPENCLAW_PORT,
                "bind": "lan",
                "auth": {
                    "mode": "trusted-proxy",
                    "trustedProxy": {
                        "userHeader": PROXY_USER_HEADER,
                        # 2026.8.1 起, 设备配对由**这里**放行, 不再是
                        # controlUi.dangerouslyDisableDeviceAuth (那个键被上游
                        # 废弃并明确"ignored")。
                        #
                        # 换过来其实更严: 旧键是全局关掉配对 (所以叫
                        # dangerously), 新的是"已经过了 trusted-proxy 鉴权的
                        # 浏览器才自动批准" —— 身份仍由边缘给定, 我们没有绕开
                        # 任何控制。
                        #
                        # 不配它的话用户看到的是一张要填 WebSocket URL/令牌/密码
                        # 的连接表单 + "去主机上跑 openclaw devices approve",
                        # 而他既没有主机也不该有。这道墙**HTTP 全绿**(200 +
                        # 标题 OpenClaw Control), 只有渲染出来才看得见 ——
                        # 2026-08-31 是 scripts/visual_check.sh 第一次跑就抓到的。
                        "deviceAutoApprove": {"enabled": True},
                    },
                },
                "trustedProxies": [config.WORK_PROXY_CIDR],
                # **必须列出完整来源**。少了它, 控制台 UI 能打开, 但一连
                # WebSocket 就被网关按来源拒掉, 页面上显示"浏览器来源不被允许"
                # 并退回一个要你填 WebSocket URL / 令牌 / 密码的连接表单 ——
                # 看着像第二道登录墙, 其实是 CORS 那一类的拒绝。
                # 不支持通配符, 所以只能按域名拼。2026-08-30 上线当天踩到。
                "controlUi": {
                    "enabled": True,
                    "allowedOrigins": [f"https://{config.OPENCLAW_DOMAIN}"],
                    # 设备配对与"严格浏览器鉴权"都关掉。它们防的是"网关直接暴露
                    # 在公网、任意浏览器都能连"的场景 —— 而我们的实例入站只放行
                    # 应用机那一个 /32, 前面还压着自家的 forward_auth, 身份也已经
                    # 由 trusted-proxy 给定。留着它们只会变成第二道墙: 配对那道会
                    # 让用户去主机上跑 `openclaw devices approve <id>`, 而他既没有
                    # 主机也不该有。
                    # dangerouslyDisableDeviceAuth 与 allowInsecureAuth 都在
                    # 2026.8.1 被废弃了。前者上游明说"retired and ignored",
                    # 后者更狠 —— controlUi 是 strictObject, 留着它容器直接起不来。
                    # 配对现在走 auth.trustedProxy.deviceAutoApprove (见上面)。
                    # allowInsecureAuth 在 2026.8.1 被废弃了 (进了上游的
                    # TIER_EVAL_RETIRED_ROOT_PATHS), 而 controlUi 的 schema 是
                    # strictObject —— **多一个键就整个启动失败**, 报
                    # `Unrecognized key: "allowInsecureAuth"` 然后容器直接
                    # Terminated。升级到 2026.8.1 时踩到, 症状是实例建得出来但
                    # 探活永远不过。
                    #
                    # 去掉它不会把墙放回来: 设备配对那道由上面
                    # dangerouslyDisableDeviceAuth 关掉, 来源那道由 allowedOrigins
                    # 放行, 身份仍由 trusted-proxy 给定。
                },
            },
            "models": {
                "mode": "merge",
                "providers": {
                    "dshcloud": {
                        "baseUrl": f"{gateway}/llm/v1",
                        # 展开成该用户的网关令牌 —— 见下面那个**不带引号**的 heredoc
                        "apiKey": "$DSH_CLOUD_TOKEN",
                        "models": models,
                    }
                },
            },
            "agents": {"defaults": {"model": f"dshcloud/{model_catalog.default_model()}"}},
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        "set -e\n"
        'mkdir -p "$OPENCLAW_STATE_DIR"\n'
        # heredoc **不加引号**: 里面的 $DSH_CLOUD_TOKEN 要展开成真令牌。
        # 配置里除它以外没有别的 $, 所以不会误伤。
        "node /app/openclaw.mjs config patch --stdin <<PATCH\n" + patch + "\nPATCH\n"
        "exec node /app/openclaw.mjs gateway\n"
    )


#: 自研工作台的端口。单容器 —— 不需要 nginx 在前面, 因为没有别人的登录墙要绕。
AGENTUI_PORT = 8080

#: 每个坑位默认驱动哪个 CLI, 以及界面上能切换哪几个。
_AGENTUI_SLOTS = {
    "claude-code": ("claude", "claude,codex"),
    "codex": ("codex", "codex,claude"),
}


def _agentui_boot(product_id: str) -> str:
    """把 NAS 挂载点交给 agent 用户 -> 种掉各 CLI 的首跑向导 -> 起服务。

    **chown 不是可有可无的**: Claude Code 拒绝以 root 跑放开权限的模式
    ("--dangerously-skip-permissions cannot be used with root/sudo privileges"),
    所以 agent 子进程降权到 uid 1000 跑 (见 main.py 的 _drop)。而 NAS 挂进来的
    /root 与 /workspace 属主是 root —— 不交过去的话 agent 连自己的会话文件都写
    不进去, 而它只在日志里抱怨一句然后照常跑, 用户是闲置回收之后才发现东西没了。

    首跑向导同样要压掉: 拆了登录墙却把人放进一个"必填"表单, 对用户没区别。
    只在文件不存在时写 —— 之后那是用户自己的偏好。
    """
    import json as _json

    cli, _ = _AGENTUI_SLOTS[product_id]
    gateway = config.PUBLIC_BASE.rstrip("/")
    home = "/home/agent"
    claude_seed = _json.dumps(
        {
            "hasCompletedOnboarding": True,
            "theme": "dark",
            "autoUpdates": False,
            "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
        },
        ensure_ascii=False,
        indent=2,
    )
    codex_toml = (
        f'model = "{_codecli_model("codex")}"\n'
        'model_provider = "dshcloud"\n'
        "\n"
        "[model_providers.dshcloud]\n"
        'name = "DSH Cloud"\n'
        f'base_url = "{gateway}/llm/v1"\n'
        'env_key = "OPENAI_API_KEY"\n'
        # Codex 0.151 起**不认 chat 面**, 只能走 responses。
        'wire_api = "responses"\n'
        "\n"
        '[projects."/workspace"]\n'
        'trust_level = "trusted"\n'
    )
    return (
        "set -e\n"
        f"install -d -o 1000 -g 1000 {home} {home}/.claude {home}/.codex /workspace\n"
        # -R 只在这两个挂载点上做。NAS 上文件可能很多, 但不交属主的话 agent
        # 一个字都写不进去, 这个代价必须付。
        f"chown -R 1000:1000 {home} /workspace 2>/dev/null || true\n"
        f'if [ ! -f {home}/.claude.json ]; then\n'
        f"cat > {home}/.claude.json <<'DSHEOF'\n" + claude_seed + "\nDSHEOF\n"
        "fi\n"
        # config.toml 每次重写: 里面有该用户的网关令牌路径与型号, 而令牌每次建
        # 实例都会换 (上一张会被吊销), 沿用旧的必然 401。
        f"cat > {home}/.codex/config.toml <<'DSHEOF'\n" + codex_toml + "DSHEOF\n"
        f"chown -R 1000:1000 {home}\n"
        # 没有 git 仓库的话「版本」标签页是空的 —— 建一个空仓库, 用户改了东西
        # 立刻就能看到 diff。已经是仓库就不动。
        "cd /workspace && (git rev-parse --git-dir >/dev/null 2>&1 || "
        "setpriv --reuid=1000 --regid=1000 --clear-groups -- git init -q) || true\n"
        # **回 /srv 再起**: 上一行把工作目录换到了 /workspace, 而 uvicorn 要从
        # /srv 才 import 得到 app —— 不回来就是 ModuleNotFoundError, 容器起不来。
        "cd /srv\n"
        f"exec uvicorn app.main:app --host 0.0.0.0 --port {AGENTUI_PORT}\n"
    )


#: CloudCLI 的端口。它绑回环, 前面压一个 nginx 主容器 (见 _cloudcli_boot)。
CLOUDCLI_PORT = 3001


def _cloudcli_stack() -> tuple[Sidecar, ...]:
    """CloudCLI 本体作为伴随容器。

    与 code-server 版 (workspace-codecli) 是同一批 CLI 的**两种形态**: 那边是
    编辑器 + 文件树, 这边是聊天式界面。老板要两版摆一起比。

    NAS 子路径故意用 home / workspace —— 和主容器那两个 (workbackend 里写死的
    /root 与 /workspace) 是**同一份**。换外壳的时候用户的文件和会话跟着走, 不会
    因为我们改了产品形态就凭空消失。
    """
    gateway = config.PUBLIC_BASE.rstrip("/")
    model = _codecli_model("claude-code")
    return (
        Sidecar(
            name="cloudcli",
            image_ref=config.CLOUDCLI_IMAGE_REF,
            cmd=("sh", "-c", f"cd /workspace && exec cloudcli start --port {CLOUDCLI_PORT}"),
            env=(
                ("HOME", "/root"),
                # 项目只能建在这个根下面 (它自己的校验), 默认是 HOME。指到
                # /workspace —— 那是 NAS 上的那一份, 用户建的东西才留得住。
                ("WORKSPACES_ROOT", "/workspace"),
                # 它底下驱动的就是 claude / codex 两个 CLI, 网关接线与 code-server
                # 版完全一致 (见 env_for)。
                ("ANTHROPIC_BASE_URL", f"{gateway}/llm/anthropic"),
                ("ANTHROPIC_AUTH_TOKEN", GATEWAY_TOKEN_PLACEHOLDER),
                ("ANTHROPIC_MODEL", model),
                ("ANTHROPIC_SMALL_FAST_MODEL", model),
                ("OPENAI_API_KEY", GATEWAY_TOKEN_PLACEHOLDER),
                ("OPENAI_BASE_URL", f"{gateway}/llm/v1"),
                # 它自带账号体系, 没有关掉鉴权的开关 —— 凭据由工作台代持, 主容器
                # 替用户注册+登录一次再把令牌注入页面 (见 _CLOUDCLI_AUTOLOGIN)。
                ("DSH_AUTOLOGIN_USER", "owner"),
                ("DSH_AUTOLOGIN_PASSWORD", STACK_PASSWORD_PLACEHOLDER),
            ),
            mounts=(("home", "/root"), ("workspace", "/workspace")),
            run_as_user=0,
        ),
    )


#: 工作台替用户注册+登录 CloudCLI, 再把令牌注入页面。
#:
#: 与 Dify/Hermes 那两处的区别: CloudCLI 的会话是 **localStorage 里的 JWT**
#: (键名 auth-token), 不是 cookie —— 所以不能靠 Set-Cookie, 只能往 HTML 里插
#: 一段脚本, 在应用自己的 bundle 跑起来之前把令牌写进去。
_CLOUDCLI_AUTOLOGIN = r"""#!/bin/sh
# 由 products.py 下发。CloudCLI 自带单用户账号体系 (注册 + 账号密码 + 7 天期的
# JWT), 没有任何关掉鉴权的开关 —— 而老板的铁律是接进来的应用不留登录墙。
# 凭据是我们生成的, 用户不可能知道, 所以只能由工作台代登。
U="$DSH_AUTOLOGIN_USER"
P="$DSH_AUTOLOGIN_PASSWORD"
[ -n "$U" ] && [ -n "$P" ] || exit 0
API=http://127.0.0.1:3001
LOGF=/root/.cloudcli-autologin.log
log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOGF" 2>/dev/null
  tail -n 200 "$LOGF" > "$LOGF.t" 2>/dev/null && mv "$LOGF.t" "$LOGF" 2>/dev/null
}

# 令牌里可能有 . 和 -, 没有引号和反斜杠 (JWT 是 base64url), 直接塞进 nginx 的
# 字符串是安全的。仍然校验一下形状 —— 万一上游换了令牌格式, 宁可不注入也不要
# 写出一份语法错的配置 (那会让 reload 静默失败, 而症状是"登录墙又回来了")。
write_conf() {
  case "$1" in
    *[!A-Za-z0-9._-]*) log "令牌形状不对, 拒绝注入"; return 1 ;;
  esac
  cat > /etc/nginx/inject/token.conf <<EOF
# 在应用自己的 bundle 跑起来之前, 把令牌写进 localStorage。
# CloudCLI 的会话是 localStorage 里的 JWT (键名 auth-token), 不是 cookie ——
# 所以这里是插脚本, 不是发 Set-Cookie。
#
# **每次页面加载都覆盖**, 不判断浏览器里有没有: 手里那份一旦失效 (实例重建换了
# 密码、或者 7 天到期) 它仍然"存在", 只在缺失时补的话用户就被永久钉在登录页。
# 这个判据我在 Dify 上错过两次, 症状一模一样。
sub_filter '</head>' '<script>try{localStorage.setItem("auth-token","$1")}catch(e){}</script></head>';
EOF
  if nginx -t >/tmp/.nginxt 2>&1; then
    nginx -s reload && return 0
    log "reload 失败: $(tail -2 /tmp/.nginxt | tr '\n' ' ')"
    return 1
  fi
  log "配置语法错, 没敢 reload: $(tail -2 /tmp/.nginxt | tr '\n' ' ')"
  return 1
}

n=0
while [ "$n" -lt 120 ]; do
  n=$((n + 1))
  # needsSetup 为真 = 这台还没建过账号 (NAS 上是空的)。注册与登录返回同一个形状,
  # 都带 token, 所以两条路都能直接拿到。
  ST=$(curl -s -m 10 "$API/api/auth/status")
  case "$ST" in
    *'"needsSetup":true'*) EP=register ;;
    *'"needsSetup":false'*) EP=login ;;
    *) sleep 5; continue ;;
  esac
  R=$(curl -s -m 15 -X POST -H 'Content-Type: application/json' \
      -d "{\"username\":\"$U\",\"password\":\"$P\"}" "$API/api/auth/$EP")
  T=$(echo "$R" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
  if [ -n "$T" ]; then
    write_conf "$T" || { sleep 10; continue; }
    # 首次配置向导 (Git 配置 / 连接 Agents) 也一并答掉 —— 登录墙拆了却把人放进
    # 一个"必填"表单, 对用户没区别: 他还是进不去。这个接口是幂等的。
    curl -s -m 10 -o /dev/null -X POST -H "Authorization: Bearer $T" \
      -H 'Content-Type: application/json' -d '{}' "$API/api/user/complete-onboarding"
    # 向导那一步要的 git 身份。/root 挂的是 NAS, 所以只在缺失时写 —— 用户自己
    # 改过就不该被下次启动覆盖。
    if [ ! -f /root/.gitconfig ]; then
      printf '[user]\n\tname = DSH Cloud Workspace\n\temail = workspace@dshcloud.online\n' > /root/.gitconfig
    fi
    # 一个项目都没有的话就把 /workspace 建成默认项目。不建的话用户进来看到的是
    # "No projects found — Run Claude CLI in a project directory to get started",
    # 得先自己建一个才能开口说话 —— 拆了登录墙却留一道空状态墙, 对他没区别。
    # 只在列表为空时建: 之后那是用户自己的项目列表。
    if [ "$(curl -s -m 10 -H "Authorization: Bearer $T" "$API/api/projects")" = "[]" ]; then
      curl -s -m 15 -o /dev/null -X POST -H "Authorization: Bearer $T" \
        -H 'Content-Type: application/json' -d '{"path":"/workspace"}' \
        "$API/api/projects/create-project"
      log "已建默认项目 /workspace"
    fi
    log "会话已下发 ($EP, 第 $n 轮)"
    # JWT 是 7 天期而容器活不到那么久, 但重登一次很便宜 —— 而且它顺带覆盖掉
    # "实例重建后密码变了" 这种情况。
    sleep 1200
    n=0
    continue
  fi
  log "$EP 没拿到令牌: $(echo "$R" | head -c 160)"
  sleep 5
done
log "放弃: 120 轮都没能登录, 用户会看到 CloudCLI 自己的登录页"
"""


def _cloudcli_boot() -> str:
    """主容器 (nginx) : 反代 CloudCLI + 把会话注入页面。

    两个坑写在这儿, 都是这套模式上踩过的:
      * `proxy_set_header Accept-Encoding ""` —— sub_filter **改不了 gzip 过的
        响应体**, 不关掉上游压缩的话脚本根本插不进去, 而两边都不报错。
      * 注入点用 `</head>` 而不是 `<head>` —— 插在 head 末尾, 应用的 bundle
        在 body 里, 顺序上稳稳早于它。
    """
    conf = (
        "map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n"
        # 这个页面里嵌着一枚活令牌 —— 任何一层把它缓存下来重放, 用户拿到的都是
        # 某一刻的旧快照。浏览器自己的 HTTP 缓存就够制造这个故障: 缓存发生在
        # 令牌注入之后、容器某次重建之前, 之后免网络重放, 症状正是"session
        # expired" (令牌本身没坏, 只是那次响应是旧的)。只对 HTML 禁缓存 ——
        # JS/CSS 文件名带内容哈希, 天然可以长期缓存, 不必连累。\n"
        "map $sent_http_content_type $cc_no_store { ~*^text/html \"no-store\"; default \"\"; }\n"
        "server {\n"
        "  listen 80;\n"
        "  client_max_body_size 512m;\n"
        "  location / {\n"
        f"    proxy_pass http://127.0.0.1:{CLOUDCLI_PORT};\n"
        "    proxy_http_version 1.1;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n"
        "    proxy_set_header Connection $connection_upgrade;\n"
        "    proxy_set_header Host $http_host;\n"
        "    proxy_set_header X-Forwarded-Proto https;\n"
        # sub_filter 改不了 gzip 过的响应体 —— 不关掉上游压缩就白插。
        '    proxy_set_header Accept-Encoding "";\n'
        "    proxy_buffering off;\n"
        "    proxy_read_timeout 86400s;\n"
        "    proxy_send_timeout 86400s;\n"
        "    sub_filter_once on;\n"
        "    sub_filter_types text/html;\n"
        # 令牌那条 sub_filter 由 autologin 写进来。glob 匹配不到时 nginx 不报错,
        # 所以登录还没成功的那几秒也能正常起。
        "    add_header Cache-Control $cc_no_store always;\n"
        "    include /etc/nginx/inject/*.conf;\n"
        "  }\n"
        "}\n"
    )
    return (
        "set -e\n"
        "mkdir -p /etc/nginx/inject\n"
        "apk add --no-cache curl >/dev/null 2>&1 || true\n"
        "cat > /etc/nginx/conf.d/default.conf <<'DSHEOF'\n" + conf + "DSHEOF\n"
        "cat > /usr/local/bin/dsh-autologin <<'DSHEOF'\n" + _CLOUDCLI_AUTOLOGIN + "DSHEOF\n"
        "chmod +x /usr/local/bin/dsh-autologin\n"
        "/usr/local/bin/dsh-autologin &\n"
        "exec nginx -g 'daemon off;'\n"
    )


#: 编码智能体工作台的端口 (code-server 的默认值)。
CODECLI_PORT = 8080

#: 每个坑位对应哪个 CLI。值是 (可执行文件, 终端里显示的名字)。
_CODECLI_AGENTS = {
    "claude-code": ("claude", "Claude Code"),
    "codex": ("codex", "Codex"),
}


def _codecli_boot(product_id: str) -> str:
    """写 code-server 与 CLI 的配置 -> 起 code-server。

    三件事是**每次启动都重写**的 (它们是产品配置, 不是用户数据):
      * code-server 的用户设置 —— 默认终端直接就是这个 agent, 用户不用先认识
        一个 shell 再想起来敲命令。
      * agent 自己的配置 (Codex 的 config.toml) —— 里面有该用户的网关令牌,
        令牌每次建实例都会换 (workspace._mint_workspace_token 会吊销上一张),
        沿用旧的必然 401。
      * 那份 tasks.json **只在不存在时写**: 它落在 /workspace 上, 是用户自己
        的目录, 每次覆盖等于把他改的东西抹掉。

    HOME 用 /root 而不是镜像里的 /home/coder: NAS 就挂在 /root 与 /workspace
    这两处 (见 workbackend 的 VolumeMount)。放别处的话 code-server 的设置、
    会话历史、CLI 的登录态全落在容器里, 闲置回收一删就没了 —— 而这个错法不
    报任何错, 用户只会发现"昨天的会话不见了"。
    """
    exe, label = _CODECLI_AGENTS[product_id]
    settings = {
        "workbench.startupEditor": "none",
        "workbench.colorTheme": "Default Dark Modern",
        # 默认给 **bash**, 不是 agent。agent 由镜像里的 dsh-agent 扩展开在编辑器
        # 区 (见 Dockerfile), 这里再把它设成默认的话, VS Code 每实例化一次终端
        # 面板就按默认配置文件**再起一个 agent** —— 屏幕上两个、账上两份。
        # 用户想要 shell 就该拿到 shell; 想再开一个 agent, 下拉框里也还在。
        "terminal.integrated.defaultProfile.linux": "bash",
        "terminal.integrated.profiles.linux": {
            label: {"path": f"/usr/local/bin/{exe}", "icon": "sparkle"},
            "bash": {"path": "/bin/bash"},
        },
        # 工作区信任那道弹窗要用户点"是, 我信任作者" —— 这是他自己的目录,
        # 问了也只有一个答案, 而在没点之前终端和任务都是禁用的。
        "security.workspace.trust.enabled": False,
        # 没有这条, folderOpen 的自动任务**不会跑** —— 默认值是"问一下", 而那句
        # 询问只在通知区闪一下, 用户看到的就是一个空编辑器, 终端得自己开。
        "task.allowAutomaticTasks": "on",
        # 关掉 VS Code 自带的 Chat/Agent 面板。它在 4.135 里是**内核 UI**, 删掉
        # copilot 扩展也去不掉 —— 面板还在, 右下角照样挂着 "Sign In", 一点就要
        # GitHub OAuth。等于我们自己的产品里又藏了一道登录墙, 正是老板明令不要
        # 的东西。而且它是多余的: 这个产品的 agent 就是终端里那个 CLI。
        "chat.disableAIFeatures": True,
        # 连带把第二侧栏默认收起 —— 那格子本来就是给 Chat 用的。
        "workbench.secondarySideBar.defaultVisibility": "hidden",
        "telemetry.telemetryLevel": "off",
    }
    import json as _json

    out = [
        "set -e\n",
        'export HOME=/root\n',
        'mkdir -p "$HOME/.local/share/code-server/User" /workspace/.vscode\n',
        "cat > \"$HOME/.local/share/code-server/User/settings.json\" <<'DSHEOF'\n",
        _json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        "DSHEOF\n",
        # 压掉 CLI 的首跑向导。用户进来该看到的是一个能打字的 agent, 不是
        # "选个主题"、"信任这个目录吗" —— 这些问题在托管环境里只有一个答案,
        # 而问出来就等于让人在自己付了钱的产品里先做一遍配置。
        # 只在不存在时写: 这个文件之后装的是用户自己的会话与偏好。
        'if [ ! -f "$HOME/.claude.json" ]; then\n',
        "cat > \"$HOME/.claude.json\" <<'DSHEOF'\n",
        _json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "autoUpdates": False,
                "hasSeenTasksHint": True,
                # 目录信任是**按项目**存的, 与 hasCompletedOnboarding 是两码事:
                # 只压掉后者的话向导没了, 但一进来还是问"这个目录你信任吗"。
                # 这是用户自己的工作区, 问了只有一个答案。
                "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "DSHEOF\n",
        "fi\n",
    ]
    if product_id == "codex":
        gateway = config.PUBLIC_BASE.rstrip("/")
        # Codex 已经**不认 chat 面**了 (0.151 起 wire_api="chat" 直接报不支持),
        # 所以这里必须是 responses —— 见网关的 /llm/v1/responses。
        toml = (
            f'model = "{_codecli_model("codex")}"\n'
            'model_provider = "dshcloud"\n'
            "\n"
            "[model_providers.dshcloud]\n"
            'name = "DSH Cloud"\n'
            f'base_url = "{gateway}/llm/v1"\n'
            'env_key = "OPENAI_API_KEY"\n'
            'wire_api = "responses"\n'
            "\n"
            # 不写这段, Codex 一起来就问"你信任这个目录吗" —— 而这是用户自己的
            # 工作区, 问了只有一个答案。托管环境里每一道这样的确认都是白挡。
            '[projects."/workspace"]\n'
            'trust_level = "trusted"\n'
        )
        out += [
            'mkdir -p "$HOME/.codex"\n',
            "cat > \"$HOME/.codex/config.toml\" <<'DSHEOF'\n",
            toml,
            "DSHEOF\n",
        ]
    out.append(
        f"exec code-server --auth none --bind-addr 0.0.0.0:{CODECLI_PORT} /workspace\n"
    )
    return "".join(out)


def _codecli_model(product_id: str) -> str:
    """这个坑位默认用哪个在售型号。

    必须钉死: 网关只放行在售目录里的型号 (见 gateway 的 model_catalog.resolve),
    而这些 CLI 各自的内置默认值都是它们厂商自己的型号名, 不钉就是每次 404。
    """
    return {"claude-code": "claude-sonnet-5", "codex": "gpt-5.6-luna"}[product_id]


#: Hermes 的控制台端口。它只绑回环 —— 见 _hermes_stack。
HERMES_PORT = 9119


#: 工作台自己登一次 Hermes 控制台, 用户不再面对它的登录/连接界面。
#: 它即使绑回环, /api/* 仍然要会话 —— 页面能打开而接口一律 401, SPA 就停在
#: 未登录态。这一点我在 spike 里看到过却当成无关, 上线后才暴露。
_HERMES_AUTOLOGIN = r"""#!/bin/sh
# 由 products.py 下发。工作台自己登一次 Hermes 控制台, 把会话发给浏览器。
#
# 它即使绑回环, /api/* 仍然要会话 (页面能打开, 接口一律 401, 于是 SPA 停在
# 未登录态)。登录是表单式的: POST /auth/password-login, 成功后下发三个 cookie。
# 所以只能像 Dify 那样替用户登一次 —— 凭据是我们生成的, 用户不可能知道。
U="$HERMES_USER"
P="$HERMES_PASS"
[ -n "$U" ] && [ -n "$P" ] || exit 0
API=http://127.0.0.1:9119
LOGF=/root/.hermes-autologin.log
log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOGF" 2>/dev/null
  tail -n 200 "$LOGF" > "$LOGF.t" 2>/dev/null && mv "$LOGF.t" "$LOGF" 2>/dev/null
}

write_conf() {
  cat > /etc/nginx/conf.d/00-autologin.conf <<EOF
# **每次页面加载都补发**, 不看浏览器有没有 —— 手里那份一旦失效 (实例重建换了
# 密码) 它仍然"存在", 只在缺失时补的话用户就被永久钉在未登录态。
# 工作台只有一个账号, 不存在"别人的会话"要保住。
# 按响应类型收窄到 HTML 文档: 静态资源和接口响应不必背这些头。
map \$sent_http_content_type \$hm_c1 { ~*^text/html "$C1"; default ""; }
map \$sent_http_content_type \$hm_c2 { ~*^text/html "$C2"; default ""; }
map \$sent_http_content_type \$hm_c3 { ~*^text/html "$C3"; default ""; }
# 上游方向也注入: 浏览器第一发 / 是没有 cookie 的, 只发 Set-Cookie 的话它会先
# 吃一个 302 落到 /login —— 页面是"Sign in", 按规矩这就算露出登录界面了。
#
# **无条件注入, 不看浏览器带没带**。先前写成"带了自己的就透传", 于是浏览器里
# 一份**失效**的 hermes_session_at (上一版发过带引号的坏值、或者实例重建换了
# 密码) 仍然算"带了" —— 原样送上去被服务端拒掉, 又跳回登录页, 而且不清 cookie
# 怎么刷新都没用。工作台只有一个账号, 不存在别人的会话要保住。
# 与 Dify 那次是同一个判据错误: 该判的是能不能用, 不是有没有。
map \$http_cookie \$hm_up { default "$V1; $V2; $V3"; }
EOF
  # 先验再 reload, 并把结果记下来 —— 上一版无论成败都报"已下发", 于是配置没换
  # 也看不出来。
  if nginx -t >/tmp/.nginxt 2>&1; then
    nginx -s reload && return 0
    log "reload 失败: $(tail -2 /tmp/.nginxt | tr '\n' ' ')"
    return 1
  fi
  log "配置语法错, 没敢 reload: $(tail -2 /tmp/.nginxt | tr '\n' ' ')"
  return 1
}

n=0
while [ "$n" -lt 120 ]; do
  n=$((n + 1))
  H=$(curl -s -D- -o /dev/null -m 10 -X POST -H 'Content-Type: application/json' \
      -H "Host: 127.0.0.1:9119" \
      -d "{\"provider\":\"basic\",\"username\":\"$U\",\"password\":\"$P\",\"next\":\"/\"}" \
      "$API/auth/password-login")
  # **把双引号去掉**, 不是转义。base64 结尾带 = 时 Hermes 会给 cookie 值加引号
  # (at="eyJ...="), 而:
  #   · 原样塞进 nginx 的 "..." 里是语法错误 -> reload 失败 -> 配置没换, 而脚本
  #     这边什么都察觉不到;
  #   · 转义成 \" 又会让引号**留在值里**发给浏览器, 服务端不认, 接口照样 401。
  # 去掉引号最省事: 带 = 的 base64 本来就是合法的 cookie 值。
  # 2026-08-30 上线当天两种写法都踩了一遍。
  pick() { echo "$H" | grep -i "^set-cookie: $1=" | head -1 | sed 's/^[Ss]et-[Cc]ookie: //' | tr -d '\r"'; }
  C1=$(pick hermes_session_at)
  C2=$(pick hermes_session_rt)
  C3=$(pick hermes_session_provider)
  # 上游注入用的是**不带属性**的那一半 (name=value), Set-Cookie 用的才带属性。
  V1=${C1%%;*}; V2=${C2%%;*}; V3=${C3%%;*}
  if [ -n "$C1" ] && [ -n "$C2" ] && [ -n "$C3" ]; then
    write_conf || { sleep 10; continue; }
    log "会话已下发 (第 $n 轮)"
    # 会话有寿命, 而容器能连着跑很久 —— 定期重登刷新。
    sleep 1200
    n=0
    continue
  fi
  sleep 5
done
log "登不上, 放弃"
"""


def _hermes_stack() -> tuple[Sidecar, ...]:
    """Hermes 本体作为伴随容器, 绑 0.0.0.0 并**开着它自己的鉴权**。

    绕过一次弯路, 记下来: 先前想绑回环省掉鉴权 (它文档确实建议"绑 127.0.0.1
    + 隧道"), 而组内共享网络命名空间, 主容器那个 nginx 就是隧道。页面确实能开,
    但**它的 /api/* 在回环模式下不认会话** —— 直连登录拿到 cookie 再用, 照样
    401, 于是 SPA 停在未登录态。实测过: 同一套流程在 0.0.0.0 + basic auth 下
    登录 200、/api/auth/me 与 /api/profiles 全 200。

    所以走这条: 鉴权**开着** (它的规矩完全满足, 我们没绕开任何控制), 凭据由
    工作台代持 —— 主容器登一次再把会话补发给浏览器 (见 _HERMES_AUTOLOGIN),
    用户看不到登录框。这也和 Dify/Coze 是同一套做法。

    **不覆盖 entrypoint, 只传 args**: 它的 entrypoint 是 s6 监督树, 顶掉之后
    脚本自己会在 stderr 抱怨一句"supervised services are unavailable"然后照常
    跑 —— 起来了但什么都不工作。
    """
    return (
        Sidecar(
            name="hermes",
            image_ref=config.HERMES_IMAGE_REF,
            args=("dashboard", "--host", "0.0.0.0", "--port", str(HERMES_PORT),
                  "--no-open", "--skip-build"),
            # **不要设 HERMES_DASHBOARD_PUBLIC_URL**。它和鉴权是绑死的:
            # 一旦配了外部公开 URL, 它就要求必须有鉴权提供方, 否则直接拒绝启动
            # ("There is no unauthenticated public-dashboard option. For
            # local-only use, bind 127.0.0.1 and leave dashboard.public_url
            # unset")。而那正是我们不要的第二道登录墙。
            # 它文档给的本地用法就是"绑回环 + 隧道", 于是 Host 由 nginx 用**绑定
            # 主机名**送过去 (见 _hermes_boot) —— SSH 隧道本来也是这个效果。
            #
            # 控制台账号: 绑回环也挡不住 /api/* 要会话, 所以配一副凭据, 由主容器
            # 替用户登一次 (见 _HERMES_AUTOLOGIN)。密码按用户推导。
            env=(
                ("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "owner"),
                ("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", STACK_PASSWORD_PLACEHOLDER),
            ),
            mounts=(("hermes/data", "/opt/data"),),
            run_as_user=0,
        ),
    )


def _hermes_init(secret: str = "", token: str = "") -> tuple[InitContainer, ...]:
    """在 Hermes 起来之前把模型指到我们的网关。

    用它自己的 `config set` 而不是整份写 config.yaml —— 那个文件里还有用户自己
    调的东西 (人格、技能、渠道), 重写一遍等于抹掉。
    """
    gateway = config.PUBLIC_BASE.rstrip("/")
    sets = " && ".join(
        f"hermes config set {k} {v}"
        for k, v in (
            ("model.provider", "custom"),
            ("model.base_url", f"{gateway}/llm/v1"),
            ("model.model", model_catalog.default_model()),
            ("model.api_key", GATEWAY_TOKEN_PLACEHOLDER),
        )
    )
    return (
        InitContainer(
            name="seed",
            image_ref=config.HERMES_IMAGE_REF,
            # 这里可以顶掉 entrypoint: 一次性写配置不需要那套监督树。
            cmd=("sh", "-c", f"mkdir -p /opt/data && ({sets}) || true"),
            mounts=(("hermes/data", "/opt/data"),),
        ),
    )


def _hermes_boot() -> str:
    """主容器是 nginx, 把流量送进同组的 Hermes 回环端口 (见 _hermes_stack)。"""
    return (
        "set -e\n"
        # 免登录 (见 _HERMES_AUTOLOGIN)。先落空默认值 —— 会话要等 hermes 起来
        # 才拿得到, 而 nginx 现在就要能起; 引用未定义变量它会直接启动失败。
        "cat > /etc/nginx/conf.d/00-autologin.conf <<'AUTOCONF'\n"
        "map $sent_http_content_type $hm_c1 { default \"\"; }\n"
        "map $sent_http_content_type $hm_c2 { default \"\"; }\n"
        "map $sent_http_content_type $hm_c3 { default \"\"; }\n"
        "map $http_cookie $hm_up { default $http_cookie; }\n"
        "AUTOCONF\n"
        "cat > /usr/local/bin/dsh-hermes-autologin <<'AUTOLOGIN'\n"
        + _HERMES_AUTOLOGIN
        + "AUTOLOGIN\n"
        "chmod +x /usr/local/bin/dsh-hermes-autologin\n"
        "/usr/local/bin/dsh-hermes-autologin >/dev/null 2>&1 &\n"
        "cat > /etc/nginx/conf.d/default.conf <<'NGINXCONF'\n"
        "server {\n"
        "  listen 80;\n"
        "  server_name _;\n"
        "  client_max_body_size 512m;\n"
        "  add_header Set-Cookie $hm_c1 always;\n"
        "  add_header Set-Cookie $hm_c2 always;\n"
        "  add_header Set-Cookie $hm_c3 always;\n"
        "  location / {\n"
        f"    proxy_pass http://127.0.0.1:{HERMES_PORT};\n"
        # Host 必须是它**绑定的**主机名。它有 Host 白名单, 只认绑定主机名或
        # 配了 public_url 的那个域; 送 $host 过去就是每一发都
        # `400 Invalid Host header`, 而两个容器都 Running、日志还写着 READY,
        # 看着一切正常。而 public_url 那条路要求必须有鉴权 = 第二道登录墙。
        f"    proxy_set_header Host 127.0.0.1:{HERMES_PORT};\n"
        # 真实域名另外给, 免得应用拼出来的绝对链接指向回环。
        "    proxy_set_header X-Forwarded-Host $host;\n"
        # 见 _HERMES_AUTOLOGIN: 没带自己 cookie 的请求走工作台那份会话, 于是
        # 第一发 / 就是 200, 不会先闪一下登录页。
        "    proxy_set_header Cookie $hm_up;\n"
        "    proxy_set_header X-Real-IP $remote_addr;\n"
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "    proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    proxy_http_version 1.1;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n"
        '    proxy_set_header Connection "upgrade";\n'
        "    proxy_read_timeout 3600s;\n"
        "    proxy_send_timeout 3600s;\n"
        "    proxy_buffering off;\n"
        "  }\n"
        "}\n"
        "NGINXCONF\n"
        "exec nginx -g 'daemon off;'\n"
    )


def registry() -> dict[str, Product]:
    return {
        DEFAULT: Product(
            id=DEFAULT,
            name="DSH",
            image=config.WORK_IMAGE,
            image_ref=config.WORK_IMAGE_REF,
            port=3081,
            mem_mb=config.WORK_MEM_LIMIT_MB,
            cpus=config.WORK_CPUS,
            domain=config.WORK_DOMAIN,
        ),
        "comfyui": Product(
            id="comfyui",
            name="ComfyUI",
            image=config.COMFY_IMAGE,
            image_ref=config.COMFY_IMAGE_REF,
            port=8188,
            mem_mb=config.COMFY_MEM_LIMIT_MB,
            cpus=config.COMFY_CPUS,
            domain=config.COMFY_DOMAIN,
            # ComfyUI 不认识 /api/work/active, 也不经网关跑模型。
            reports_presence=False,
            tab_grace_min=config.COMFY_TAB_GRACE_MIN,
        ),
        # Dify: LLM 应用搭建。10 容器栈 —— 走 Sidecar 那条轨道, 伴随容器全部
        # 上游原生镜像。实测空载约 1.9GB (4 个 Python 进程是大头), 4G 组够用。
        "dify": Product(
            id="dify",
            name="Dify",
            image="nginx:1.27-alpine",
            image_ref="nginx:1.27-alpine",
            port=80,
            mem_mb=config.DIFY_MEM_LIMIT_MB,
            cpus=config.DIFY_CPUS,
            domain=config.DIFY_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.DIFY_TAB_GRACE_MIN,
            # 首页 (/) 答的是 Next.js, 它比 api 早起来一分多钟, 而且 api 没起来时
            # 它把错误边界渲染出来照样回 200 —— 拿它探活等于没探。这条路径
            # proxy_pass 去 api:5001 (见 _dify_boot), api 没起来时 nginx 给 502。
            ready_path="/console/api/system-features",
            sidecars=_dify_stack(),
            # 栈内互相用回环, 这里只兜住万一漏改的服务名引用。
            host_aliases=("api", "api_websocket", "web", "db_postgres", "redis",
                          "weaviate", "plugin_daemon", "sandbox", "nginx"),
        ),
        # Coze Studio: 10 容器栈, 目前最重的一个 (ES + Milvus + MySQL)。
        # 主容器是上游的 coze-web (nginx + 前端静态资源); 业务全在伴随容器上。
        "coze": Product(
            id="coze",
            name="Coze Studio",
            image=f"cozedev/coze-studio-web:{config.COZE_VERSION}",
            image_ref=f"cozedev/coze-studio-web:{config.COZE_VERSION}",
            port=80,
            mem_mb=config.COZE_MEM_LIMIT_MB,
            cpus=config.COZE_CPUS,
            domain=config.COZE_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.COZE_TAB_GRACE_MIN,
            # 首页 (/) 是 `root /usr/share/nginx/html` 的**静态文件** —— coze-server
            # 还没起来它照样回 200, 拿它探活等于没探 (与 Dify 同一个洞, 见
            # Product.ready_path)。/api/ 走上游那条 `^/(api|v[1-3]|admin)(/|$)`
            # 的 proxy_pass, 转发去 coze-server:8888。
            #
            # 2026-08-30 起真实例实测 (合成 key, 测完即毁): 第 23 秒 nginx 已经
            # 就绪, `/`=200 而 `/api/`=502; 第 119 秒 coze-server 才应答,
            # `/api/`=404。**同一条路径 502 -> 404 的跃迁**就是"后端可达了"的
            # 铁证 —— 404 是 Hertz 对未注册路由的默认应答 (后端没有自定义
            # NoRoute), 判据 <500 于是做对; 而旧探针会在第 23 秒就放人进来,
            # 96 秒的坏窗口, 比 Dify 那 44 秒还长。
            #
            # 用未注册路由而不是某个真接口: 它**不会有副作用**, 也不随接口增删
            # 而失效 (那几个 GET 里 /logout/ 会登出、/password/reset/ 会触发重置)。
            ready_path="/api/",
            sidecars=_coze_stack(),
            # 组内一律用上游的服务名 (见 _coze_server_env 的说明), 全部指回环。
            host_aliases=("mysql", "redis", "elasticsearch", "minio", "milvus",
                          "etcd", "nsqd", "nsqlookupd", "coze-server", "coze-web"),
            init_containers=(
                InitContainer(
                    name="seed",
                    image_ref=config.COZE_ASSETS_IMAGE_REF,
                    # 铺完上游资产, 再把在售模型写成模型配置。顺序不能反 ——
                    # cp 的目标目录是 conf/model, 得先有它。
                    cmd=("sh", "-c", "set -e\ncp -a /assets/. /seed/\n" + _coze_model_yamls()),
                ),
            ),
            seeds=(("nginx", "/seed"),),
        ),
        # OpenClaw: 自托管的个人智能体 (Peter Steinberger), 一个网关同时接
        # Telegram/Discord/Slack 等几十个渠道, 自带控制台 UI。单容器。
        "openclaw": Product(
            id="openclaw",
            name="OpenClaw 2.0",
            image=config.OPENCLAW_IMAGE_REF,
            image_ref=config.OPENCLAW_IMAGE_REF,
            port=OPENCLAW_PORT,
            mem_mb=config.OPENCLAW_MEM_LIMIT_MB,
            cpus=config.OPENCLAW_CPUS,
            domain=config.OPENCLAW_DOMAIN if config.WORK_PROXY_CIDR else "",
            reports_presence=False,
            tab_grace_min=config.OPENCLAW_TAB_GRACE_MIN,
            # 镜像里 USER 是 node, 写不进 NAS 挂进来的目录 —— 而它只会在日志里
            # 抱怨一句数据库打不开, 照常起来, 于是用户的东西一回收就没了。
            run_as_user=0,
        ),
        # Hermes Agent (Nous Research): 会自己攒技能、带持久记忆的常驻 agent。
        # 两容器: nginx 主容器 + 绑回环的 hermes (见 _hermes_stack)。
        "hermes": Product(
            id="hermes",
            name="Hermes Agent",
            image="nginx:1.27-alpine",
            image_ref="nginx:1.27-alpine",
            port=80,
            mem_mb=config.HERMES_MEM_LIMIT_MB,
            cpus=config.HERMES_CPUS,
            domain=config.HERMES_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.HERMES_TAB_GRACE_MIN,
            sidecars=_hermes_stack(),
            init_containers=_hermes_init(),
        ),
        # Open Design: 智能体编排壳 (上游官方镜像**不带任何 agent CLI**, 托管
        # 环境里干不了活 —— 衍生镜像 od-local 烤进了我们的 dsh, 见
        # deploy/workspace-opendesign)。单容器, 比 dsh 还轻 (实测空载 166MB)。
        # Claude Code / Codex: 两格都跑**我们自己写的**工作台
        # (deploy/workspace-agentui), 区别只在默认驱动哪个 CLI。
        #
        # 顶掉 CloudCLI 的理由 (老板 2026-08-31 拍板): 别人的界面里挂着别人的
        # 引流入口 (Star / Join Community / Report Issue), 而用户付的是我们的钱;
        # 更要紧的是**积分这个核心机制在别人的 UI 里没有位置** —— 余额、本轮
        # 消耗、剩余分钟只有自己写才放得进去。附带好处: 为了绕开 CloudCLI 自带
        # 的账号体系我写过四段补丁, 每段都可能被上游一次更新打掉。
        "claude-code": Product(
            id="claude-code",
            name="Claude Code",
            image=config.AGENTUI_IMAGE_REF,
            image_ref=config.AGENTUI_IMAGE_REF,
            port=AGENTUI_PORT,
            mem_mb=config.CODECLI_MEM_LIMIT_MB,
            cpus=config.CODECLI_CPUS,
            domain=config.CLAUDE_CODE_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.CODECLI_TAB_GRACE_MIN,
            # 首页是静态文件, 后端没起来它照样 200 —— 探针必须打一条真进后端的
            # 路径 (2026-08-30 Dify 与 Coze 都栽过这个)。
            ready_path="/api/health",
            # NAS 挂进来的 /root 与 /workspace 属主是 root, 服务要以 root 起才
            # 写得动; agent 子进程再由服务自己降权 (见 _agentui_boot)。
            run_as_user=0,
        ),
        "codex": Product(
            id="codex",
            name="Codex",
            image=config.AGENTUI_IMAGE_REF,
            image_ref=config.AGENTUI_IMAGE_REF,
            port=AGENTUI_PORT,
            mem_mb=config.CODECLI_MEM_LIMIT_MB,
            cpus=config.CODECLI_CPUS,
            domain=config.CODEX_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.CODECLI_TAB_GRACE_MIN,
            ready_path="/api/health",
            run_as_user=0,
        ),
        "open-design": Product(
            id="open-design",
            name="Open Design",
            image=config.OPEN_DESIGN_IMAGE_REF,
            image_ref=config.OPEN_DESIGN_IMAGE_REF,
            port=7456,
            mem_mb=config.OPEN_DESIGN_MEM_LIMIT_MB,
            cpus=config.OPEN_DESIGN_CPUS,
            domain=config.OPEN_DESIGN_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.OPEN_DESIGN_TAB_GRACE_MIN,
        ),
        # Operator: 我们自己写的动手型智能体 (deploy/workspace-agents-team)。
        # 与 codecli 那条线的分工: 那边给编辑器 + CLI, 用户自己敲; 这边交代一件事,
        # 它自己在容器里做完。单容器, 前端和 API 都在 8710。
        "agents-team": Product(
            id="agents-team",
            name="Agents Team",
            image=config.AGENTS_TEAM_IMAGE_REF,
            image_ref=config.AGENTS_TEAM_IMAGE_REF,
            port=8710,
            mem_mb=config.AGENTS_TEAM_MEM_LIMIT_MB,
            cpus=config.AGENTS_TEAM_CPUS,
            domain=config.AGENTS_TEAM_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.AGENTS_TEAM_TAB_GRACE_MIN,
            # 首页是静态文件, 后端没起来照样 200 —— 探它等于没探 (见 ready_path
            # 字段的说明)。/api/health 由 FastAPI 出, 后端不活就连不上。
            ready_path="/api/health",
        ),
        # OpenHands: 自主编码智能体。单容器 (uvicorn 同时出前端和 API), 沙箱走
        # local runtime —— 默认那个要挂 docker socket 起第二个容器, ECI 上给不了。
        "openhands": Product(
            id="openhands",
            name="OpenHands",
            image=config.OPENHANDS_IMAGE_REF,
            image_ref=config.OPENHANDS_IMAGE_REF,
            port=3000,
            mem_mb=config.OPENHANDS_MEM_LIMIT_MB,
            cpus=config.OPENHANDS_CPUS,
            domain=config.OPENHANDS_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.OPENHANDS_TAB_GRACE_MIN,
        ),
        # AutoGen Studio: 单进程 (FastAPI 同时出前端和 API)。
        "autogen": Product(
            id="autogen",
            name="AutoGen Studio",
            image=config.AUTOGEN_IMAGE_REF,
            image_ref=config.AUTOGEN_IMAGE_REF,
            port=8081,
            mem_mb=config.AUTOGEN_MEM_LIMIT_MB,
            cpus=config.AUTOGEN_CPUS,
            domain=config.AUTOGEN_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.AUTOGEN_TAB_GRACE_MIN,
            # 首页是打包好的静态站, 后端没起来照样 200 —— 探它等于没探。
            ready_path="/api/health",
        ),
        # LangChain: 前端 + LangGraph 一个容器, 前面 node 反代分流。
        "langchain": Product(
            id="langchain",
            name="LangChain",
            image=config.LANGCHAIN_IMAGE_REF,
            image_ref=config.LANGCHAIN_IMAGE_REF,
            port=3000,
            mem_mb=config.LANGCHAIN_MEM_LIMIT_MB,
            cpus=config.LANGCHAIN_CPUS,
            domain=config.LANGCHAIN_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.LANGCHAIN_TAB_GRACE_MIN,
            # 探**后端**那一侧: 前端起得比 LangGraph 快得多, 探首页等于只探到
            # Next.js 起没起来 —— 而没有后端的聊天界面是个死壳。
            ready_path="/langgraph/info",
        ),
        # OpenManus 与 CrewAI: 同一个镜像, 靠启动脚本区分 —— 两个产品共用一份
        # ECI 镜像缓存。界面是浏览器终端 (ttyd), 因为这两个框架都没有界面。
        "openmanus": Product(
            id="openmanus",
            name="OpenManus",
            image=config.FRAMEWORKS_IMAGE_REF,
            image_ref=config.FRAMEWORKS_IMAGE_REF,
            port=7681,
            mem_mb=config.FRAMEWORKS_MEM_LIMIT_MB,
            cpus=config.FRAMEWORKS_CPUS,
            domain=config.OPENMANUS_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.FRAMEWORKS_TAB_GRACE_MIN,
        ),
        "crewai": Product(
            id="crewai",
            name="CrewAI",
            image=config.FRAMEWORKS_IMAGE_REF,
            image_ref=config.FRAMEWORKS_IMAGE_REF,
            port=7681,
            mem_mb=config.FRAMEWORKS_MEM_LIMIT_MB,
            cpus=config.FRAMEWORKS_CPUS,
            domain=config.CREWAI_DOMAIN,
            reports_presence=False,
            tab_grace_min=config.FRAMEWORKS_TAB_GRACE_MIN,
        ),
    }
# fmt: on


def get(product_id: str) -> Product | None:
    return registry().get(product_id)


def enabled() -> list[Product]:
    """配了域名且配了镜像的才算启用 —— 少任何一样都进不去。

    初始化容器的镜像也算"镜像": 它空着的话容器组会被阿里云拒掉, 而用户看到的
    是一直转圈。宁可这个产品干脆不出现在目录里。
    """
    return [
        p
        for p in registry().values()
        if p.domain and p.image and all(ic.image_ref for ic in p.init_containers)
    ]


def by_domain(host: str) -> Product | None:
    host = (host or "").split(":")[0]
    for product in registry().values():
        if product.domain and product.domain == host:
            return product
    return None


# --- 启动脚本 ------------------------------------------------------------------
# 刻意不含任何按用户变化的值 (凭据走 env), 于是它的摘要标识的是**配置**而不是
# 用户 —— 这正是"运行中的容器算不算过期"能被判定的原因。


def _dshcloud_provider(indent: str) -> str:
    """dsh 侧「DSH Cloud」这个 provider 的定义, 按给定缩进吐 YAML。

    两处要用同一份 (dsh 工作台的 settings.yaml、Open Design 那个 profile 的
    patch 层), 而它们的缩进层级不同 —— 所以参数化缩进而不是各写一份: 目录里
    加个模型只改一处, 否则必漂, 而漂的表现是"某个工作台里选不到新模型"。

    走 pi-ai 适配器 (openai-completions) 而**不是** llm-deepseek: 我们的上游说
    的是标准 OpenAI 流式, 而 llm-deepseek 那套 DeepSeek 风味的工具调用解析会从
    里面拼出空的工具名 —— 智能体于是干不完活也不报错。
    """
    gateway = config.PUBLIC_BASE.rstrip("/")
    rows = "".join(
        f"{indent}  - id: {m['id']}\n{indent}    name: {m.get('display_name', m['id'])}\n"
        for m in model_catalog.catalog().values()
    )
    return (
        f"{indent}displayName: DSH Cloud\n"
        f"{indent}apiKeyEnv: DSH_CLOUD_TOKEN\n"
        f"{indent}api: openai-completions\n"
        f"{indent}baseURL: {gateway}/llm/v1\n"
        f"{indent}models:\n" + rows
    )


def _dsh_boot() -> str:
    gateway = config.PUBLIC_BASE.rstrip("/")
    # web_search stays on the deepseek search row via env; 聊天走 pi-ai,
    # 理由见 _dshcloud_provider。
    settings_yaml = (
        "llm-deepseek:\n"
        f"  baseURL: {gateway}/llm/v1\n"
        "  models: []\n"
        "llm-pi-ai:\n"
        "  providers:\n"
        "    dshcloud:\n" + _dshcloud_provider("      ") + "agent-default-model:\n"
        "  provider: dshcloud\n"
        f"  model: {model_catalog.default_model()}\n"
    )
    agents_md = (
        "# DSH Cloud 云工作台\n\n"
        "你运行在一个云端容器里，用户通过浏览器访问你。用户的电脑和这个容器"
        "**不是同一台机器**。\n\n"
        "## 让用户能打开你做的网页 / 服务\n\n"
        "- **绝不要**让用户访问 `http://localhost:<端口>` 或 `127.0.0.1` —— 那是本容器的"
        "回环地址，用户的浏览器打不开。\n"
        f"- 本容器的端口可以通过这个公网地址预览：`{gateway}/preview/<端口>/`\n"
        f"  例如你在 8080 起了服务，就告诉用户打开 `{gateway}/preview/8080/`\n"
        "- **服务必须监听 `0.0.0.0`**，只听 127.0.0.1 的服务无法被预览代理到。\n"
        "  - `python3 -m http.server 8080 --bind 0.0.0.0`\n"
        "  - vite: `--host 0.0.0.0`；next: `-H 0.0.0.0`\n"
        "- 页面里引用资源请用**相对路径**（`./game.js`），预览代理对相对路径最稳。\n"
        "- 纯静态单文件（如一个 index.html）也需要起个 http 服务再给预览地址，"
        "不要只把文件路径告诉用户。\n"
    )
    merge_agents_md = (
        "node -e '"
        'const fs=require("fs"),p="/root/.dsh/AGENTS.md";'
        'const B="<!-- dshcloud:begin -->",E="<!-- dshcloud:end -->";'
        'const block=B+"\\n"+fs.readFileSync("/root/.dsh/.dshcloud-agents.md","utf8").trim()+"\\n"+E;'
        'let cur="";try{cur=fs.readFileSync(p,"utf8")}catch(e){}'
        'const re=new RegExp(B+"[\\\\s\\\\S]*?"+E);'
        'fs.writeFileSync(p,re.test(cur)?cur.replace(re,block):(cur.trim()?block+"\\n\\n"+cur.trim()+"\\n":block+"\\n"));'
        "'"
    )
    return (
        "mkdir -p /root/.dsh && cat > /root/.dsh/settings.yaml <<'DHCEOF'\n" + settings_yaml + "DHCEOF\n"
        "cat > /root/.dsh/.dshcloud-agents.md <<'DHCMDEOF'\n"
        + agents_md
        + "DHCMDEOF\n"
        + merge_agents_md
        + "\n"
        f"python3 -m http.server {PREVIEW_STATIC_PORT} --bind 0.0.0.0 "
        "--directory /workspace >/dev/null 2>&1 & "
        "socat TCP-LISTEN:3081,fork,reuseaddr TCP:127.0.0.1:3080 & "
        "exec dsh web --host 127.0.0.1 --port 3080"
    )


def _comfyui_boot() -> str:
    """ComfyUI 以**纯编排器**运行 —— 不带 GPU, 算力全在远端。

    实测 (deploy/workspace-comfyui/): 内存峰值 583MB, 冷启动 8-9 秒, 所以它和
    dsh 工作台是同一个数量级的东西, 不需要另开机型。

    产物落在卷上: ComfyUI 默认往 /opt/ComfyUI/output 写, 而持久化的是 /workspace,
    所以把 output/input 指过去 —— 否则容器一回收, 用户生成的东西全没。
    """
    return (
        "set -e\n"
        "mkdir -p /workspace/output /workspace/input\n"
        "rm -rf /opt/ComfyUI/output /opt/ComfyUI/input\n"
        "ln -s /workspace/output /opt/ComfyUI/output\n"
        "ln -s /workspace/input /opt/ComfyUI/input\n"
        # 用户目录也落在持久卷上 —— 默认它在镜像里, 一回收用户自己存的工作流、
        # 设置全没, 而且不报错, 只是下次进来空空如也。
        "mkdir -p /workspace/.comfy-user/default/workflows\n"
        # 预置那两张能直接跑的图。
        #
        # 规则: **用户没动过才更新**。两个极端都不行 ——
        #   只补缺的 (原做法): 发出去的工作流永远到不了已有用户。2026-08-27 实测:
        #     生视频节点改成返回 VIDEO、预置图接上了 SaveVideo, 而老板工作台里
        #     仍是那张孤零零的旧图。
        #   无条件覆盖: 用户在同名工作流上的改动每次冷启动都被抹掉。
        # 做法是留一份「我们发的是什么」在 .shipped/, 磁盘上那份与它逐字节相同
        # 就说明没人动过, 可以安全换新。
        "mkdir -p /workspace/.comfy-user/default/workflows/.shipped\n"
        "for f in /opt/ComfyUI/custom_nodes/dsh_cloud/example_workflows/*.json; do\n"
        '  [ -e "$f" ] || continue\n'
        '  base="$(basename "$f")"\n'
        '  live="/workspace/.comfy-user/default/workflows/$base"\n'
        '  mark="/workspace/.comfy-user/default/workflows/.shipped/$base"\n'
        # 没有标记 = 这份是标记机制上线前铺下去的, 当作未改动更新一次并补上标记。
        # 一次性风险: 若用户在那之前就改过同名工作流, 这次会被覆盖。可接受 ——
        # 机制与工作流是同一版发出去的, 中间只隔几小时。
        '  if [ ! -e "$live" ] || [ ! -e "$mark" ] || cmp -s "$live" "$mark"; then\n'
        '    cp "$f" "$live"; cp "$f" "$mark"\n'
        "  fi\n"
        "done\n"
        # 官方 API 节点的垫片 (见镜像里的 /opt/dsh-api-shim.py)。它把 ComfyUI 内置
        # 那 30+ 个厂商节点的请求转译到我们的网关并补上令牌 —— 于是官方节点和官方
        # 模板都能跑我们的模型, 用户不必只用我们自己那两个节点。
        # 只监听回环: 它是拿着容器令牌的, 绝不能对外。
        # 日志走 **stdout**, 不落文件: ECI 上没有 docker exec, 落进容器里的文件
        # 谁也看不到 —— 垫片一出问题就完全没有线索。stdout 会进容器日志
        # (DescribeContainerLog 能取), 与 ComfyUI 的交错但有 [shim] 前缀。
        "python -u /opt/dsh-api-shim.py 2>&1 &\n"
        "cd /opt/ComfyUI\n"
        "exec python main.py --cpu --listen 0.0.0.0 --port 8188 "
        "--user-directory /workspace/.comfy-user "
        f"--comfy-api-base http://127.0.0.1:{SHIM_PORT}\n"
    )


# OpenDesign 的应用偏好 (<dataDir>/app-config.json)。**不预置就有一道上游登录墙**:
# 默认选中的 agent 是它自家的 amr(vela), 镜像里没有那个二进制, 于是 onboarding
# 向导停在 "登录 OpenDesign" 那一步, 点下去报 `vela binary not found`。
# 而我们的域早被 forward_auth 挡在自家登录之后 —— 用户已经登过一次了,
# 再要他登第三方账号既走不通也不该有。
#
# 选 deepseek-harness (bin=dsh): 实测它是这个镜像里**唯一** available=true 的
# agent —— 上游原生就认 dsh, 不是我们硬塞的。
#
# 只补缺失的键: 用户之后自己换 agent / 换模型要留得住 (这个文件在 NAS 上),
# 每次启动强写会把他的选择按回去, 而且不报错。
_OD_APP_CONFIG_JS = (
    "const fs=require('fs'),p='/app/.od/app-config.json';"
    "let c={};try{c=JSON.parse(fs.readFileSync(p,'utf8'))||{}}catch(e){}"
    "let d=false;"
    "if(c.onboardingCompleted!==true){c.onboardingCompleted=true;d=true}"
    # 也把 amr 掰回来: 向导里点过一次的用户, 文件里已经写着 amr, 只判"缺不缺"
    # 救不回他 —— 而 amr 在这个镜像里永远不可用 (没有 vela, 也不该有)。
    "if(!c.agentId||c.agentId==='amr'){c.agentId='deepseek-harness';d=true}"
    # 上游默认 metrics/content 全开, content 会把用户的设计内容发去第三方。
    # 我们是托管方, 替用户默认关掉; 他想开自己去设置里开。
    "if(!c.telemetry){c.telemetry={metrics:false,content:false};"
    "c.privacyDecisionAt=new Date().toISOString();d=true}"
    "if(d)fs.writeFileSync(p,JSON.stringify(c,null,2));"
)


def _opendesign_boot() -> str:
    """四步: 数据目录落 NAS、给 dsh 装 OpenDesign profile、**预置应用偏好**、拉起 daemon。

    profile 装在用户的 /root/.dsh (NAS 持久化) —— 只装一次; 但 dsh 的插件
    loader 从 dsh **自己的** node_modules 解析包名, 所以每次启动要把 profile
    里的 @open-design 作用域软链过去 (镜像文件系统每次都是新的)。
    没有这条软链的症状: probe 报 Cannot find package '@open-design/dsh-runtime',
    UI 里 agent 显示 profile incompatible —— spike 时逐条踩过。
    """
    return (
        "set -e\n"
        "mkdir -p /workspace/.od\n"
        "rm -rf /app/.od\n"
        "ln -s /workspace/.od /app/.od\n"
        "[ -f /root/.dsh/profiles/open-design/package.json ] || "
        "dsh plugin --profile open-design add /opt/od-profile.tgz\n"
        "ln -sfn /root/.dsh/profiles/open-design/node_modules/@open-design "
        "/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@open-design\n"
        "cat > /root/.dsh/profiles/open-design/cordis.patch.yml <<'DHCPATCH'\n"
        + _opendesign_patch_yaml()
        + "DHCPATCH\n"
        f"node -e {_OD_APP_CONFIG_JS!r}\n"
        "cd /app\n"
        "exec node apps/daemon/dist/cli.js --no-open\n"
    )


def _opendesign_patch_yaml() -> str:
    """给 open-design 这个 profile 的**用户层** (cordis.patch.yml) 写模型配置。

    `~/.dsh/settings.yaml` 对**命名 profile 不生效** —— 那是 web profile 的用户层。
    没有这一步, agent-default-model 解析出来是 `provider: deepseek-official`,
    即 llm-deepseek 适配器; 它对着我们这个说标准 OpenAI 流式的网关, 会把工具调用
    拼成空的工具名, 于是智能体跑一半就结束、**不给终态**。
    用户看到的是 `DSH_PROFILE_MISSING_RESULT: profile exited without a terminal
    result`, 而每一层自己看都正常: 令牌是对的、网关是通的、probe 也握手成功。
    2026-08-30 老板在 Open Design 里点"写种"就撞上了这个。

    这个文件是**我们的配置**, 不是用户的内容 —— 与 dsh 工作台的 settings.yaml
    同一性质, 所以每次启动照写 (目录里加了模型, 下次进来就能选到)。
    (用户的内容是 AGENTS.md 那种, 那边走的是带标记的合并。)
    """
    return (
        "- id: llm-pi-ai\n"
        "  config:\n"
        "    providers:\n"
        "      dshcloud:\n" + _dshcloud_provider("        ") + "- id: agent-default-model\n"
        "  config:\n"
        "    provider: dshcloud\n"
        f"    model: {model_catalog.default_model()}\n"
    )


def _agents_team_boot() -> str:
    """Operator 是我们自己的镜像, 启动脚本因此很短 —— 配置全走 env。

    仍要 mkdir: 工作目录是 NAS 挂进来的, 挂载点在容器里出现的时机不由我们定;
    首轮 shell 落在一个不存在的 cwd 上会莫名其妙失败, 而报错里不会提"目录不存在"。
    """
    return (
        "set -e\n"
        "mkdir -p /workspace\n"
        "cd /opt/agents-team\n"
        "exec uvicorn app.main:app --host 0.0.0.0 --port 8710\n"
    )


def _openhands_boot() -> str:
    """OpenHands: 起服务, 然后**用它自己的接口把设置灌进去**。

    首屏本来是一道硬墙 ("To continue, connect an OpenAI, Anthropic, or other LLM
    account", 没有跳过)。实测:
      · `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` 三个环境变量**不管用** ——
        它读的是存下来的设置 (容器内的 SQLite), 不是环境;
      · `POST /api/v1/settings` 灌一次, 墙当场消失, 进的是真正的应用。
    所以只能等服务起来再灌 —— 灌在服务起来之前会连不上, 而那是静默的
    (curl 失败, 脚本照常往下走, 用户开门见墙)。

    沙箱用 **local runtime**: 默认那个要挂宿主的 docker socket 起第二个容器,
    ECI 上给不了。而我们本来就是一人一容器 —— 这个容器就是他的沙箱, 正合适。

    遥测默认**关**: 隐私偏好一律选最保守的那个。
    """
    return (
        "set -e\n"
        "mkdir -p /workspace\n"
        # **必须 cd /app**: 前端那堆静态文件是按工作目录找的, 在别处起 uvicorn
        # 的话首页直接 404 —— 而且是 `{"detail":"Not Found"}` 这种 API 式的 404,
        # 看着像路由没配, 其实是 cwd 不对 (镜像的 WorkingDir 就是 /app, 我们绕过
        # 它的 entrypoint 自己起, 就得自己把这条补上)。2026-09-01 线上撞到。
        "cd /app\n"
        # 服务先起, 灌设置要等它应答 —— 后台起, 前台等
        "uvicorn openhands.server.listen:app --host 0.0.0.0 --port 3000 &\n"
        "srv=$!\n"
        # 最多等 90 秒。它启动时要跑数据库迁移, 冷启动实测 ~25 秒。
        "for i in $(seq 1 90); do\n"
        "  curl -fsS -o /dev/null http://127.0.0.1:3000/ 2>/dev/null && break\n"
        "  sleep 1\n"
        "done\n"
        # 灌设置。**必须成功** —— 失败就是用户开门见墙, 所以留痕并重试几次。
        "for i in 1 2 3; do\n"
        '  code=$(curl -s -o /tmp/oh_set.log -w "%{http_code}" -X POST '
        "http://127.0.0.1:3000/api/v1/settings -H 'Content-Type: application/json' "
        '-d "{\\"llm_model\\":\\"$DSH_LLM_MODEL\\",'
        '\\"llm_base_url\\":\\"$DSH_LLM_BASE\\",'
        '\\"llm_api_key\\":\\"$DSH_CLOUD_TOKEN\\",'
        '\\"agent\\":\\"CodeActAgent\\",'
        '\\"enable_default_condenser\\":true,'
        '\\"user_consents_to_analytics\\":false}") || true\n'
        '  [ "$code" = "200" ] && { echo "[dsh] openhands 设置已灌入"; break; }\n'
        '  echo "[dsh] 灌设置失败 (HTTP $code), 重试"; cat /tmp/oh_set.log; sleep 3\n'
        "done\n"
        "wait $srv\n"
    )


def _autogen_boot() -> str:
    """AutoGen Studio: 直接起。

    它**本来就没有登录墙** (进去是 Guest User 的 Playground), 模型也在构建期就
    换成读环境变量了 —— 所以这里不用像 OpenHands 那样起完再灌一遍设置。

    状态落 /data (NAS 挂在这儿): 队伍、会话、图库都在 SQLite 里, 落容器里的话
    闲置回收一删就没了。
    """
    return (
        "set -e\n"
        "mkdir -p /data\n"
        "autogenstudio ui --host 0.0.0.0 --port 8081 --appdir /data &\n"
        "srv=$!\n"
        # 默认图库和那支示例队伍是**首次访问接口时才惰性生成**的 —— 不预热的话,
        # 第一个打开页面的人正好撞在生成之前, 看到的是"Create a team to get
        # started"的空侧栏, 刷新一次才有 (线上第一次视觉验收就是这样)。
        "for i in $(seq 1 60); do\n"
        "  curl -fsS -o /dev/null http://127.0.0.1:8081/api/health 2>/dev/null && break\n"
        "  sleep 1\n"
        "done\n"
        'curl -fsS -o /dev/null "http://127.0.0.1:8081/api/gallery/?user_id=guestuser@gmail.com" || true\n'
        'curl -fsS -o /dev/null "http://127.0.0.1:8081/api/teams/?user_id=guestuser@gmail.com" || true\n'
        'echo "[dsh] autogen 默认队伍已预热"\n'
        "wait $srv\n"
    )


def _langchain_boot() -> str:
    """LangChain: 一个容器里三个进程 —— LangGraph、前端、前置反代。

    顺序不讲究 (前端连的是浏览器发过来的请求, LangGraph 慢起几秒无非是头一条
    消息重试), 但**反代必须在前台**: 它是对外那个端口, 它退出容器就该退出。

    LangGraph 用 `dev` 模式跑: 它自带内存版检查点, 不用再挂一个 Postgres ——
    一人一容器, 会话本来就不跨容器共享。
    """
    return (
        "set -e\n"
        "mkdir -p /data\n"
        "cd /opt/agent\n"
        "langgraph dev --host 127.0.0.1 --port 2024 --no-browser >/tmp/langgraph.log 2>&1 &\n"
        "PORT=3001 HOSTNAME=127.0.0.1 node /opt/web/server.js >/tmp/web.log 2>&1 &\n"
        "exec node /opt/front.mjs\n"
    )


#: 这一格进去先看到什么。**不是装饰**: 这两个框架都没有界面, 用户开门就是一个
#: 黑终端 —— 不告诉他能敲什么, 这个产品跟"给了台空机器"没区别。
_FRAMEWORK_HELLO = {
    "openmanus": (
        "OpenManus —— 开源版 Manus, 一个通用智能体。模型和网关已经配好 "
        "(config/config.toml), 直接开聊:\n"
        "    cd /opt/openmanus && python main.py\n"
        "换个跑法: python run_flow.py (多智能体编排) / python run_mcp.py (MCP 服务)\n"
    ),
    "crewai": (
        "CrewAI —— 把智能体组成一支船员队, 各有角色和任务。模型和网关已配好 "
        "(环境变量里)。三步开工:\n"
        "    crewai create crew my_crew      # 生成脚手架\n"
        "    cd my_crew                      # 改 config/agents.yaml 和 tasks.yaml\n"
        "    crewai run                      # 跑起来\n"
        "工具包按需装: pip install crewai-tools (没预装 —— 它拖一大串, 该由你按用途选)\n"
    ),
}


def _frameworks_boot(product_id: str) -> str:
    """OpenManus / CrewAI: 一个浏览器终端, 框架和网关都配好了。

    **OpenManus 的配置必须在这里写**: 它 import 时就构造 Config(), 读的是
    config/config.toml —— 镜像里烤的是占位值, 不换成这个用户的令牌, 他敲第一条
    命令就是 401。

    ttyd 的启动命令是**必填**的 (少了它自己会说 "missing start command" 然后
    退出); 这里给的是 bash, 先打一段说明再交给用户。
    """
    hello = _FRAMEWORK_HELLO[product_id].replace("'", "'\\''")
    venv = "openmanus" if product_id == "openmanus" else "crewai"
    return (
        "set -e\n"
        "mkdir -p /workspace\n"
        # 用户的令牌灌进 OpenManus 的配置。printf 而不是 cat<<EOF —— 这段脚本
        # 本身是被 sh -c 传进来的, 少一层 here-doc 少一层转义。
        'printf \'[llm]\\nmodel = "%s"\\nbase_url = "%s"\\napi_key = "%s"\\n'
        # **[daytona] 那一段不能漏**: 上游的 DaytonaSettings.daytona_api_key 没有
        # 默认值, 缺了它 Config() 在 **import 期**就抛 pydantic 校验错误。镜像里
        # 烤的那份带着它, 这里覆盖时漏掉就等于把它撤销了 —— 渲染出来一看就发现,
        # 光读代码看不出来。
        "max_tokens = 8192\\ntemperature = 0.0\\n\\n[daytona]\\n"
        'daytona_api_key = "unused"\\n\' '
        '"$DSH_MODEL" "$OPENAI_BASE_URL" "$OPENAI_API_KEY" '
        "> /opt/openmanus/config/config.toml\n"
        f"printf '%s' '{hello}' > /etc/motd\n"
        # ttyd: -W 允许写入 (只读终端没法用), 起始目录是 NAS 上的 /workspace
        # **各自的虚拟环境放进 PATH**: 两个框架要的 openai 版本不兼容, 镜像里
        # 是两个 venv (见 Dockerfile)。不设 PATH 的话用户敲 crewai 找不到命令,
        # 敲 python 用的是系统那个 —— 两样都不对。
        f"export PATH=/opt/venv-{venv}/bin:$PATH\n"
        "exec ttyd -W -p 7681 -t titleFixed='DSH Cloud' "
        "bash -lc 'cat /etc/motd; cd /workspace; exec bash'\n"
    )


_BOOTS = {
    DEFAULT: _dsh_boot,
    "comfyui": _comfyui_boot,
    "open-design": _opendesign_boot,
    "dify": _dify_boot,
    "coze": _coze_boot,
    "openclaw": _openclaw_boot,
    "hermes": _hermes_boot,
    "claude-code": lambda: _agentui_boot("claude-code"),
    "codex": lambda: _agentui_boot("codex"),
    "agents-team": _agents_team_boot,
    "openhands": _openhands_boot,
    "autogen": _autogen_boot,
    "langchain": _langchain_boot,
    "openmanus": lambda: _frameworks_boot("openmanus"),
    "crewai": lambda: _frameworks_boot("crewai"),
}


def boot_script(product_id: str) -> str:
    builder = _BOOTS.get(product_id)
    if builder is None:
        raise ValueError(f"unknown product {product_id!r}")
    return builder()


def _pick_media_model(offered: list | None, key: str, prefer: tuple[str, ...]) -> dict[str, str]:
    """按子串优先级从在售目录里挑一个型号。

    目录顺序没有语义, 取第一项是碰运气 —— 实测会挑到 seedance 而不是万相 3.0。
    prefer 里的子串**按顺序**匹配 (子串而非全等, 免得写死 -260128 这种日期后缀),
    全不中才回落第一项; 目录为空则什么都不设 (调用方据此判定工具不可用)。
    """
    if not offered:
        return {}
    ids = [str(m.get("id") or "") for m in offered if m.get("id")]
    for want in prefer:
        for mid in ids:
            if want in mid:
                return {key: mid}
    return {key: ids[0]} if ids else {}


def env_for(product_id: str, token: str, secret: str = "") -> dict[str, str]:
    gateway = config.PUBLIC_BASE.rstrip("/")
    if product_id == "hermes":
        # 模型配置由初始化容器写进 NAS (见 _hermes_init); 这里给主容器免登录用的
        # 凭据 —— 它要替用户登一次控制台 (见 _HERMES_AUTOLOGIN)。
        return {
            "HERMES_USER": "owner",
            "HERMES_PASS": autologin_password(secret),
        }
    if product_id in ("openmanus", "crewai"):
        return {
            "HOME": "/root",
            # OpenManus 的 config.toml 与 CrewAI 的 litellm 都认这几个。型号要钉在
            # 在售目录里 —— 网关只放行目录内的。
            "DSH_MODEL": _codecli_model("codex"),
            "OPENAI_BASE_URL": f"{gateway}/llm/v1",
            "OPENAI_API_KEY": token,
            "OPENAI_API_BASE": f"{gateway}/llm/v1",  # litellm 认的是这个名字
        }
    if product_id == "langchain":
        return {
            # graph.py 认这三个。型号要钉在在售目录里 —— 网关只放行目录内的。
            "DSH_MODEL": _codecli_model("codex"),
            "OPENAI_BASE_URL": f"{gateway}/llm/v1",
            "OPENAI_API_KEY": token,
        }
    if product_id == "autogen":
        return {
            # 镜像里那个补丁认这三个 (见 deploy/workspace-autogen/patch_models.py)。
            "DSH_MODEL": _codecli_model("codex"),
            "OPENAI_BASE_URL": f"{gateway}/llm/v1",
            "OPENAI_API_KEY": token,
        }
    if product_id == "openhands":
        return {
            # 一人一容器, 容器本身就是沙箱 —— 默认 runtime 要挂宿主 docker socket
            # 再起一个容器, ECI 上做不到。
            "RUNTIME": "local",
            "SANDBOX_VOLUMES": "/workspace:/workspace:rw",
            # 灌设置那一步要用 (见 _openhands_boot)。型号必须钉在**在售目录**里 ——
            # 它内置的默认值是厂商自己的名字, 网关只放行目录内的, 不钉就是 404。
            "DSH_CLOUD_TOKEN": token,
            "DSH_LLM_MODEL": f"openai/{_codecli_model('codex')}",
            "DSH_LLM_BASE": f"{gateway}/llm/v1",
        }
    if product_id in _AGENTUI_SLOTS:
        cli, enabled = _AGENTUI_SLOTS[product_id]
        model = _codecli_model("claude-code")
        return {
            # 服务以 root 起 (要写 NAS), agent 子进程降权到这个 uid 跑 ——
            # Claude Code 拒绝以 root 跑放开权限的模式。
            "HOME": "/root",
            "DSH_AGENT_UID": "1000",
            "DSH_AGENT_HOME": "/home/agent",
            "DSH_WORKSPACE": "/workspace",
            # 会话清单落 NAS —— 闲置回收会把容器整个删掉。
            "DSH_STATE_DIR": "/home/agent/.dsh-agentui",
            "DSH_DEFAULT_CLI": cli,
            "DSH_ENABLED_CLIS": enabled,
            # 界面上那个余额/消耗就靠这两个 (工作台令牌能查 /api/work/status)。
            "DSH_CLOUD_TOKEN": token,
            "DSH_GATEWAY_BASE": gateway,
            "DSH_PRODUCT_ID": product_id,
            # 各 CLI 的网关接线。型号必须钉在**在售目录**里 —— 网关只放行目录内
            # 的, 而这些 CLI 的内置默认值都是厂商自己的名字, 不钉就是每次 404。
            "ANTHROPIC_BASE_URL": f"{gateway}/llm/anthropic",
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_MODEL": model,
            # 不设它的话后台任务去要 haiku, 而我们不卖那个名字。
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "OPENAI_API_KEY": token,
        }
    if product_id in _CODECLI_AGENTS:
        model = _codecli_model(product_id)
        env = {
            # NAS 就挂在 /root 与 /workspace —— HOME 指别处等于让 code-server 的
            # 设置、会话历史和 CLI 登录态全落在容器里, 闲置回收一删就没了。
            "HOME": "/root",
            # 这几个 CLI 各自的内置默认型号都是厂商自己的名字, 网关只放行在售
            # 目录里的 —— 不钉死就是每次 404。
            "DSH_CLOUD_TOKEN": token,
            # 镜像里那个 dsh-agent 扩展照这两个变量开终端 (见
            # deploy/workspace-codecli/Dockerfile)。少了它们, 用户进去看到的
            # 是一个空编辑器 —— 而这个产品卖的就是"点开就能用"。
            "DSH_AGENT_CMD": f"/usr/local/bin/{_CODECLI_AGENTS[product_id][0]}",
            "DSH_AGENT_NAME": _CODECLI_AGENTS[product_id][1],
        }
        if product_id == "claude-code":
            env |= {
                "ANTHROPIC_BASE_URL": f"{gateway}/llm/anthropic",
                "ANTHROPIC_AUTH_TOKEN": token,
                "ANTHROPIC_MODEL": model,
                # 小模型也指同一个: 不设的话它去要 haiku, 而我们不卖那个名字。
                "ANTHROPIC_SMALL_FAST_MODEL": model,
            }
        else:
            # Codex 的 base_url / wire_api 在 config.toml 里 (见 _codecli_boot),
            # 这里只给密钥 —— env_key 指的就是它。
            env["OPENAI_API_KEY"] = token
        return env
    if product_id == "coze":
        # 主容器只是 nginx —— 业务 env 全在伴随容器上 (见 _coze_stack)。
        # 免登录的账号密码由容器自己随机生成并存在 NAS 上 (Coze 可以随便建账号)。
        return {}
    if product_id == "dify":
        # 同上, 但 Dify 是**单租户**: setup 一辈子只能跑一次, 建不了第二个账号。
        # 所以免登录的密码必须**可推导** —— 存文件的话, NAS 一丢或换个实例就再也
        # 登不进那个既有账号了 (而且它没有找回密码的路)。
        return {
            # 用 admin@ 而不是 Coze 那边的 owner@: Dify 单租户, setup 建的就是这个
            # 账号, 没有第二个可建 —— 所以这里必须与既有账号对齐, 不能另起一个。
            "DSH_AUTOLOGIN_EMAIL": "admin@dshcloud.online",
            "DSH_AUTOLOGIN_PASSWORD": autologin_password(secret),
            # 用来清掉 Dify 那把 24 小时的登录锁 (见 _DIFY_AUTOLOGIN 的 unlock)。
            "DSH_REDIS_PASSWORD": _DIFY_REDIS_PASSWORD,
            # 预置模型用 (见 _DIFY_AUTOLOGIN 的 provision)。Dify 的模型供应商是
            # **插件**, 开箱一个都没装 —— 用户新建个聊天助手, 模板里写的是 gpt-*,
            # 于是当场报 "Provider langgenius/openai/openai does not exist"。
            "DSH_CLOUD_TOKEN": token,
            "DSH_GATEWAY_BASE": f"{gateway}/llm/v1",
            # 默认那两个单独给: 它们先配、且只有它们设工作区默认模型。
            "DSH_DEFAULT_MODEL": model_catalog.default_model(),
            "DSH_EMBEDDING_MODEL": _dify_embedding_model(),
            # 在售的全部模型 (含上面两个, provision 会跳过)。
            "DSH_MODELS": _dify_chat_models(),
            "DSH_EMBEDDING_MODELS": _dify_embedding_models(),
        }
    if product_id == "agents-team":
        # 我们自己的镜像, 所以不用占位符那一套 —— 令牌在这里就是真值 (env_for 拿到的
        # token 已经是该用户铸好的)。模型列表与在售目录一致, 前端下拉直接用。
        env = {
            "DSH_GATEWAY_BASE": f"{gateway}/llm/v1",
            "DSH_CLOUD_TOKEN": token,
            "DSH_DEFAULT_MODEL": model_catalog.default_model(),
            # 不复用 _dify_chat_models(): 那个名字属于 Dify 那条线, 借过来用会让
            # 以后改 Dify 的人不知道自己顺手也改了这里。
            "DSH_MODELS": _sh_list(model_catalog.catalog()),
        }
        # 剧组要出图/出片 (2026-08-31): 型号从 media.offered() 的**在售目录**里挑,
        # 不硬编码 —— 写死就是哪天下架每次 404, 而错误只出现在容器里没人看的日志里。
        #
        # 但也不能"取第一项": 目录顺序没有语义, 实测取到的是 seedance 而不是万相
        # 3.0。剧组这条线对模型有明确偏好, 用**子串优先级**表达 —— 目录里有就用,
        # 没有就顺位, 全都没有才回落第一项。既不依赖某个型号一定存在, 也不会把
        # 最合适的那个漏掉。
        try:
            from . import media as _media

            _off = _media.offered()
            # 视频: 万相 3.0 优先 —— 单段更长意味着**接缝更少**, 而换脸/道具漂移/
            # 环境音断裂大多就发生在接缝处, 是短剧一致性的头号来源。
            # Prime 不设默认: 它贵一半 (720p 15 vs 10 积分/秒), 该由用户自己选。
            env |= _pick_media_model(
                _off.get("video"),
                "DSH_VIDEO_MODEL",
                ("wan3.0-video", "seedance-2-5", "seedance-2-0"),
            )
            # 图像: 通义千问图像 3.0 优先 —— 提示词长度是前代的 4.5 倍, 而分镜要把
            # 画面结构/文字内容/排版细节像需求文档一样写全。
            env |= _pick_media_model(
                _off.get("image"),
                "DSH_IMAGE_MODEL",
                ("qwen-image-3.0", "gpt-image-2"),
            )
        except Exception:  # noqa: BLE001 — 媒体目录读不出来不该拖垮整个工作台创建
            logger.exception("agents-team: 媒体目录读取失败, 出图/出片工具将不可用")
        return env
    if product_id == "open-design":
        return {
            # daemon 自身
            "NODE_ENV": "production",
            "OD_BIND_HOST": "0.0.0.0",
            "OD_PORT": "7456",
            # 它的 API 鉴权是给公网裸奔场景准备的; 我们的 forward_auth 已经把
            # 整个域挡在登录后面, 双层鉴权只会让前端配不上令牌而全挂。
            "OD_DISABLE_API_AUTH": "1",
            "OD_ALLOWED_ORIGINS": f"https://{config.OPEN_DESIGN_DOMAIN}" if config.OPEN_DESIGN_DOMAIN else "",
            # 里面跑的 agent 是 dsh —— 与 dsh 工作台同一套网关凭据
            "DSH_CLOUD_TOKEN": token,
            "DEEPSEEK_API_KEY": token,
            "DEEPSEEK_BASE_URL": f"{gateway}/llm/v1",
            "DEEPSEEK_SEARCH_BASE_URL": f"{gateway}/llm/anthropic/v1",
            "DSH_TELEMETRY_DISABLED": "1",
            "DSH_PERMISSION_MODE": "danger-full-access",
        }
    if product_id == "openclaw":
        return {
            # 状态与配置落在 NAS 上 (/workspace 是挂载点) —— 镜像默认写
            # /home/node, 那是容器内的盘, 实例一回收就没了。
            "OPENCLAW_STATE_DIR": "/workspace/.openclaw",
            "OPENCLAW_CONFIG_PATH": "/workspace/.openclaw/openclaw.json",
            "DSH_CLOUD_TOKEN": token,
        }
    if product_id == "comfyui":
        return {
            # 自有节点只认这两个 —— 它不认识任何一家厂商, 由网关去适配。
            "DSH_CLOUD_TOKEN": token,
            "DSH_CLOUD_VIDEO_BASE": f"{gateway}/llm/v1",
            "COMFY_OUTPUT_DIR": "/workspace/output",
        }
    return {
        "DSH_CLOUD_TOKEN": token,
        "DEEPSEEK_API_KEY": token,
        "DEEPSEEK_BASE_URL": f"{gateway}/llm/v1",
        "DEEPSEEK_SEARCH_BASE_URL": f"{gateway}/llm/anthropic/v1",
        "DSH_TELEMETRY_DISABLED": "1",
        # 每用户容器就是沙箱边界: 资源上限、隔离网络、无 docker socket、非特权。
        "DSH_PERMISSION_MODE": "danger-full-access",
    }
