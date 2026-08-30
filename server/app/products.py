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

from dataclasses import dataclass

from . import config, model_catalog

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


def resolve_sidecars(sidecars: tuple[Sidecar, ...], secret: str, token: str = "") -> tuple[Sidecar, ...]:
    """把伴随容器 env 里的占位符换成真值 (每用户密钥、网关凭据)。"""
    from dataclasses import replace

    def sub(v: str) -> str:
        return (
            v.replace(STACK_SECRET16_PLACEHOLDER, secret[:16])
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
            v.replace(STACK_SECRET16_PLACEHOLDER, secret[:16])
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


def dify_autologin_password(secret: str) -> str:
    """Dify 免登录账号的密码, 从该用户的栈密钥推导。

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
# 只配**默认的那一个** chat 模型和一个向量化模型: **新建**凭据时 Dify 会真打一次
# 上游做校验, 也就是真扣一次积分 (实测 84/49 token)。二十个模型全配等于首次进入
# 白烧二十次, 而用户想要别的在界面上点两下就能加。
# (刷新已存凭据的 PUT 实测**不**校验, 所以每次冷启动刷一遍是免费的。)
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
ensure_model() {  # ensure_model <model> <model_type> <credentials-json>
  CID=$(cred_id "$1" "$2")
  if [ -n "$CID" ]; then
    OUT=$(api PUT "$CREDS_URL" \
      "{\"credential_id\":\"$CID\",\"model\":\"$1\",\"model_type\":\"$2\",\"name\":\"DSH Cloud\",\"credentials\":$3}")
  else
    OUT=$(api POST "$CREDS_URL" \
      "{\"model\":\"$1\",\"model_type\":\"$2\",\"name\":\"DSH Cloud\",\"credentials\":$3}")
    echo "$OUT" | grep -q '"result":"success"' && api POST "/workspaces/current/default-model" \
      "{\"model_settings\":[{\"model_type\":\"$2\",\"provider\":\"$PROV\",\"model\":\"$1\"}]}" >/dev/null
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
  LLM_OK=no; EMB_OK=no
  [ -n "$DSH_EMBEDDING_MODEL" ] || EMB_OK=yes
  n=0
  while [ "$n" -lt 30 ]; do
    n=$((n + 1))
    if [ "$LLM_OK" = no ] && ensure_model "$DSH_DEFAULT_MODEL" llm "$(llm_creds)"; then LLM_OK=yes; fi
    if [ "$EMB_OK" = no ] && ensure_model "$DSH_EMBEDDING_MODEL" text-embedding "$(emb_creds)"; then EMB_OK=yes; fi
    if [ "$LLM_OK" = yes ] && [ "$EMB_OK" = yes ]; then log "模型凭据已就绪 (第 $n 轮)"; return 0; fi
    sleep 10
  done
  log "模型凭据没配上 llm=$LLM_OK emb=$EMB_OK"
  return 1
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
        "map $sent_http_content_type $dsh_at { default \"\"; }\n"
        "map $sent_http_content_type $dsh_rt { default \"\"; }\n"
        "map $sent_http_content_type $dsh_ct { default \"\"; }\n"
        "AUTOCONF\n"
        "cat > /usr/local/bin/dsh-dify-autologin <<'AUTOLOGIN'\n"
        + _DIFY_AUTOLOGIN
        + "AUTOLOGIN\n"
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
    EnvironmentVar。只写**非空的与选类型的** —— Go 侧 os.Getenv 读不到和读到
    空串是一回事, 所以第三方厂商的空 key 不用逐条搬。
    """
    gateway = config.PUBLIC_BASE.rstrip("/")
    dsn = (
        f"{_COZE_DB_USER}:{_COZE_DB_PASSWORD}@tcp(mysql:3306)/{_COZE_DB}"
        "?charset=utf8mb4&parseTime=True"
    )
    model_id = model_catalog.default_model()
    model_name = (model_catalog.catalog().get(model_id) or {}).get("display_name", model_id)
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
        ("MODEL_PROTOCOL_0", "openai"),
        ("MODEL_OPENCOZE_ID_0", "100001"),
        ("MODEL_NAME_0", f"{model_name} (DSH Cloud)"),
        ("MODEL_ID_0", model_id),
        ("MODEL_API_KEY_0", GATEWAY_TOKEN_PLACEHOLDER),
        ("MODEL_BASE_URL_0", f"{gateway}/llm/v1"),
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
        "cat > /usr/local/bin/dsh-coze-autologin <<'AUTOLOGIN'\n"
        + _COZE_AUTOLOGIN
        + "AUTOLOGIN\n"
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
                    "trustedProxy": {"userHeader": PROXY_USER_HEADER},
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


#: Hermes 的控制台端口。它只绑回环 —— 见 _hermes_stack。
HERMES_PORT = 9119


def _hermes_stack() -> tuple[Sidecar, ...]:
    """Hermes 本体作为伴随容器, **绑回环**。

    它自己的规矩: 非回环绑定强制要鉴权 (`--insecure` 从 2026-06 起是 no-op),
    而它没有 OpenClaw 那种 trusted-proxy 模式, 只有表单密码或 OAuth —— 两条都
    是第二道登录墙。文档给的建议是"绑 127.0.0.1 + 隧道"。
    容器组共享网络命名空间, 所以**主容器那个 nginx 就是那条隧道**: Hermes 绑
    回环 (于是免鉴权, 完全按它自己推荐的姿势), 只有同组的 nginx 够得着, 而
    nginx 前面是我们的 forward_auth。既没绕开它的安全控制, 也没有第二道墙。

    **不覆盖 entrypoint, 只传 args**: 它的 entrypoint 是 s6 监督树, 顶掉之后
    脚本自己会在 stderr 抱怨一句"supervised services are unavailable"然后照常
    跑 —— 起来了但什么都不工作。
    """
    return (
        Sidecar(
            name="hermes",
            image_ref=config.HERMES_IMAGE_REF,
            args=("dashboard", "--host", "127.0.0.1", "--port", str(HERMES_PORT),
                  "--no-open", "--skip-build"),
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
        "cat > /etc/nginx/conf.d/default.conf <<'NGINXCONF'\n"
        "server {\n"
        "  listen 80;\n"
        "  server_name _;\n"
        "  client_max_body_size 512m;\n"
        "  location / {\n"
        f"    proxy_pass http://127.0.0.1:{HERMES_PORT};\n"
        "    proxy_set_header Host $host;\n"
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
            sidecars=_coze_stack(),
            # 组内一律用上游的服务名 (见 _coze_server_env 的说明), 全部指回环。
            host_aliases=("mysql", "redis", "elasticsearch", "minio", "milvus",
                          "etcd", "nsqd", "nsqlookupd", "coze-server", "coze-web"),
            init_containers=(
                InitContainer(
                    name="seed",
                    image_ref=config.COZE_ASSETS_IMAGE_REF,
                    cmd=("sh", "-c", "cp -a /assets/. /seed/"),
                ),
            ),
            seeds=(("nginx", "/seed"),),
        ),
        # OpenClaw: 自托管的个人智能体 (Peter Steinberger), 一个网关同时接
        # Telegram/Discord/Slack 等几十个渠道, 自带控制台 UI。单容器。
        "openclaw": Product(
            id="openclaw",
            name="OpenClaw",
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
        "      dshcloud:\n" + _dshcloud_provider("        ") +
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: dshcloud\n"
        f"    model: {model_catalog.default_model()}\n"
    )


_BOOTS = {
    DEFAULT: _dsh_boot,
    "comfyui": _comfyui_boot,
    "open-design": _opendesign_boot,
    "dify": _dify_boot,
    "coze": _coze_boot,
    "openclaw": _openclaw_boot,
    "hermes": _hermes_boot,
}


def boot_script(product_id: str) -> str:
    builder = _BOOTS.get(product_id)
    if builder is None:
        raise ValueError(f"unknown product {product_id!r}")
    return builder()


def env_for(product_id: str, token: str, secret: str = "") -> dict[str, str]:
    gateway = config.PUBLIC_BASE.rstrip("/")
    if product_id == "hermes":
        # 主容器只是 nginx; 模型配置由初始化容器写进 NAS (见 _hermes_init)。
        return {}
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
            "DSH_AUTOLOGIN_PASSWORD": dify_autologin_password(secret),
            # 用来清掉 Dify 那把 24 小时的登录锁 (见 _DIFY_AUTOLOGIN 的 unlock)。
            "DSH_REDIS_PASSWORD": _DIFY_REDIS_PASSWORD,
            # 预置模型用 (见 _DIFY_AUTOLOGIN 的 provision)。Dify 的模型供应商是
            # **插件**, 开箱一个都没装 —— 用户新建个聊天助手, 模板里写的是 gpt-*,
            # 于是当场报 "Provider langgenius/openai/openai does not exist"。
            "DSH_CLOUD_TOKEN": token,
            "DSH_GATEWAY_BASE": f"{gateway}/llm/v1",
            "DSH_DEFAULT_MODEL": model_catalog.default_model(),
            "DSH_EMBEDDING_MODEL": _dify_embedding_model(),
        }
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
