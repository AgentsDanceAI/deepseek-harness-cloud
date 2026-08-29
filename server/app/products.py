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

    name: str                                # 组内容器名
    image_ref: str                           # 上游原生镜像的完整地址
    cmd: tuple[str, ...] = ()                # 空 = 用镜像默认 entrypoint
    env: tuple[tuple[str, str], ...] = ()
    # NAS 卷上的挂载: (用户子目录下的相对路径, 容器内路径)。中间件的数据要
    # 跟着用户走 —— 实例回收重建后, 库还在。
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


def resolve_sidecars(sidecars: tuple[Sidecar, ...], secret: str) -> tuple[Sidecar, ...]:
    """把伴随容器 env 里的密钥占位符换成真值。"""
    from dataclasses import replace

    return tuple(
        replace(sc, env=tuple((k, v.replace(STACK_SECRET_PLACEHOLDER, secret)) for k, v in sc.env))
        for sc in sidecars
    )


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
    # 不跑 squid: 它要挂配置文件, 而我们的每用户容器组本来就是隔离边界。
    # 留空 = 不经代理直出; 留着指向不存在的 3128 会让 HTTP 请求节点全挂。
    ("SSRF_PROXY_HTTP_URL", ""), ("SSRF_PROXY_HTTPS_URL", ""),
    ("MARKETPLACE_ENABLED", "true"),
    ("MARKETPLACE_API_URL", "https://marketplace.dify.ai"),
)


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


def _dify_boot() -> str:
    """主容器是 nginx。**配置我们自己生成** —— 官方那份用 `set $up api:5001` +
    `resolver 127.0.0.11` (Docker 内嵌 DNS), 而 nginx 的 resolver 不读
    /etc/hosts, host_aliases 兜不住; upstream 写死回环就绕开了整个解析环节。
    """
    return (
        "set -e\n"
        "cat > /etc/nginx/conf.d/default.conf <<'NGINXCONF'\n"
        "server {\n"
        "  listen 80;\n"
        "  server_name _;\n"
        "  client_max_body_size 100m;\n"
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
            image=f"nginx:1.27-alpine",
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


def get(product_id: str) -> Product | None:
    return registry().get(product_id)


def enabled() -> list[Product]:
    """配了域名且配了镜像的才算启用 —— 少任何一样都进不去。"""
    return [p for p in registry().values() if p.domain and p.image]


def by_domain(host: str) -> Product | None:
    host = (host or "").split(":")[0]
    for product in registry().values():
        if product.domain and product.domain == host:
            return product
    return None


# --- 启动脚本 ------------------------------------------------------------------
# 刻意不含任何按用户变化的值 (凭据走 env), 于是它的摘要标识的是**配置**而不是
# 用户 —— 这正是"运行中的容器算不算过期"能被判定的原因。


def _dsh_boot() -> str:
    gateway = config.PUBLIC_BASE.rstrip("/")
    # Chat goes through dsh's pi-ai adapter (openai-completions protocol), NOT
    # the llm-deepseek adapter: our upstream speaks standard OpenAI streaming,
    # and llm-deepseek's DeepSeek-flavored tool-call parsing assembles empty
    # tool names from it. web_search stays on the deepseek search row via env.
    model_rows = "".join(
        f"        - id: {m['id']}\n          name: {m.get('display_name', m['id'])}\n"
        for m in model_catalog.catalog().values()
    )
    settings_yaml = (
        "llm-deepseek:\n"
        f"  baseURL: {gateway}/llm/v1\n"
        "  models: []\n"
        "llm-pi-ai:\n"
        "  providers:\n"
        "    dshcloud:\n"
        "      displayName: DSH Cloud\n"
        "      apiKeyEnv: DSH_CLOUD_TOKEN\n"
        "      api: openai-completions\n"
        f"      baseURL: {gateway}/llm/v1\n"
        "      models:\n" + model_rows + "agent-default-model:\n"
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


def _opendesign_boot() -> str:
    """三步: 数据目录落 NAS、给 dsh 装 OpenDesign profile、拉起 daemon。

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
        "cd /app\n"
        "exec node apps/daemon/dist/cli.js --no-open\n"
    )


_BOOTS = {
    DEFAULT: _dsh_boot,
    "comfyui": _comfyui_boot,
    "open-design": _opendesign_boot,
    "dify": _dify_boot,
}


def boot_script(product_id: str) -> str:
    builder = _BOOTS.get(product_id)
    if builder is None:
        raise ValueError(f"unknown product {product_id!r}")
    return builder()


def env_for(product_id: str, token: str) -> dict[str, str]:
    gateway = config.PUBLIC_BASE.rstrip("/")
    if product_id == "dify":
        # 主容器只是 nginx —— 业务 env 全在伴随容器上 (见 _dify_stack)。
        return {}
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
