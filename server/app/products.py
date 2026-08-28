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


def wskey(user_id: str, product_id: str = DEFAULT) -> str:
    return user_id if product_id == DEFAULT else f"{user_id}{_SEP}{product_id}"


def split_key(key: str) -> tuple[str, str]:
    if _SEP not in key:
        return key, DEFAULT
    user_id, _, product_id = key.partition(_SEP)
    return user_id, product_id


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
        "python /opt/dsh-api-shim.py >/tmp/dsh-shim.log 2>&1 &\n"
        "cd /opt/ComfyUI\n"
        "exec python main.py --cpu --listen 0.0.0.0 --port 8188 "
        "--user-directory /workspace/.comfy-user "
        f"--comfy-api-base http://127.0.0.1:{SHIM_PORT}\n"
    )


_BOOTS = {DEFAULT: _dsh_boot, "comfyui": _comfyui_boot}


def boot_script(product_id: str) -> str:
    builder = _BOOTS.get(product_id)
    if builder is None:
        raise ValueError(f"unknown product {product_id!r}")
    return builder()


def env_for(product_id: str, token: str) -> dict[str, str]:
    gateway = config.PUBLIC_BASE.rstrip("/")
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
