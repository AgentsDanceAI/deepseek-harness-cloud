from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import re
import shutil
import subprocess
import sys

from .wizard import apply_answers, command_prefix, next_steps, prompt_answers, should_run_wizard
from pathlib import Path


HELP = """Usage: dsh-cloud COMMAND [DIRECTORY] [OPTIONS]

Commands:
  start    safely initialize when needed, validate, and start the stack
  init     write a managed deployment without starting Docker
  doctor   validate Docker Compose and the managed configuration
  up       start an initialized deployment
  down     stop it without deleting data
  status   show Compose service status
  logs     show service logs

Global options:
  --help              show this help
  --version           show the product release version
  --mode trial|selfhost (default: trial)
  --dir PATH          deployment directory (default: ./dsh-cloud)
  --dry-run           print the exact plan without writing or running Docker
  --json              stable machine-readable output
"""
COMMANDS = {"init", "start", "doctor", "up", "down", "status", "logs"}
VALUE_OPTIONS = {"--mode": "mode", "--dir": "dir", "--domain": "domain", "--admin-email": "adminEmail", "--project-name": "projectName"}
BOOLEAN_OPTIONS = {"--yes": "yes", "--json": "json", "--dry-run": "dryRun", "--wait": "wait", "--follow": "follow"}
TRIAL_PUBLIC_BASE = "http://localhost:8787"
TRIAL_CADDY_SITE = "http://localhost"
PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class CliError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


def parse_args(argv: list[str]) -> dict:
    if not argv or "--help" in argv or argv[0] == "-h":
        return {"special": "help"}
    if "--version" in argv:
        return {"special": "version"}
    command = argv[0]
    if command not in COMMANDS:
        raise CliError(f"unknown command: {command}")
    options: dict[str, str | bool] = {}
    positionals: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--upstream-key" or token.startswith("--upstream-key="):
            raise CliError("secret values are not accepted as command arguments")
        if token in BOOLEAN_OPTIONS:
            options[BOOLEAN_OPTIONS[token]] = True
        elif token in VALUE_OPTIONS:
            index += 1
            if index >= len(argv) or argv[index].startswith("--"):
                raise CliError(f"{token} requires a value")
            options[VALUE_OPTIONS[token]] = argv[index]
        elif token.startswith("-"):
            raise CliError(f"unknown option: {token}")
        else:
            positionals.append(token)
        index += 1
    return {"command": command, "options": options, "positionals": positionals}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def load_manifest() -> dict:
    packaged = Path(__file__).with_name("release-manifest.json")
    if packaged.is_file():
        return json.loads(packaged.read_text(encoding="utf-8"))
    source = json.loads((repository_root() / "release/release.json").read_text(encoding="utf-8"))
    return {
        "schemaVersion": 1,
        "version": source["version"],
        "license": source["license"],
        "stackSchema": source["stackSchema"],
        "databaseSchema": source["databaseSchema"],
        "minCliVersion": source["minCliVersion"],
        "minUpgradeFrom": source["minUpgradeFrom"],
        "harnessRuntime": source["harnessRuntime"],
        "images": source["productImages"],
        "baseImages": source["baseImages"],
    }


def target_of(parsed: dict) -> Path:
    options, positionals = parsed["options"], parsed["positionals"]
    return Path(str(options.get("dir") or (positionals[0] if positionals else "dsh-cloud"))).resolve()


def docker_argv(target: Path, project: str, action: list[str] | None = None) -> list[str]:
    return ["docker", "compose", "--project-directory", str(target), "--project-name", project,
            "--env-file", str(target / ".env"), "-f", str(target / "docker-compose.yml"),
            *(action or ["up", "-d", "--wait"])]


def valid_domain(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 253 or "." not in value:
        return False
    return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in value.split("."))


def validate_options(options: dict, mode: str, project: str) -> None:
    if not PROJECT_NAME_PATTERN.fullmatch(project):
        raise CliError("invalid project name")
    if mode != "selfhost":
        return
    if not valid_domain(options.get("domain")):
        raise CliError("--domain must be a hostname without a scheme or port")
    email = options.get("adminEmail")
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise CliError("--admin-email must be a valid email address")


def plan(parsed: dict, manifest: dict) -> dict:
    target = target_of(parsed)
    options = parsed["options"]
    mode = str(options.get("mode", "trial"))
    if mode not in {"trial", "selfhost"}:
        raise CliError(f"invalid mode: {mode}")
    project = str(options.get("projectName", "dsh-selfhost"))
    validate_options(options, mode, project)
    trial = mode == "trial"
    return {
        "ok": True,
        "dryRun": bool(options.get("dryRun")),
        "command": parsed["command"],
        "mode": mode,
        "directory": str(target),
        "projectName": project,
        "bindAddress": "127.0.0.1" if trial else "0.0.0.0",
        "url": TRIAL_PUBLIC_BASE if trial else f"https://{options.get('domain', '')}",
        "publicBaseUrl": TRIAL_PUBLIC_BASE if trial else f"https://{options.get('domain', '')}",
        "version": manifest["version"],
        "dockerArgv": docker_argv(target, project, action_for(parsed["command"], options)),
    }


def atomic_write(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def template_roots() -> tuple[Path, Path]:
    packaged = Path(__file__).with_name("templates")
    if (packaged / "docker-compose.yml").is_file():
        return packaged, packaged / "config"
    root = repository_root()
    return root / "deploy/selfhost", root / "server/config"


def generated_env(parsed: dict, manifest: dict, answers: dict | None = None) -> str:
    options = parsed["options"]
    mode = str(options.get("mode", "trial"))
    trial = mode == "trial"
    if not trial and (not options.get("domain") or not options.get("adminEmail")):
        raise CliError("--domain and --admin-email are required in selfhost mode")
    domain = "localhost" if trial else str(options["domain"])
    public_base = TRIAL_PUBLIC_BASE if trial else f"https://{domain}"
    values = [
        ("COMPOSE_PROJECT_NAME", str(options.get("projectName", "dsh-selfhost"))),
        ("DOMAIN", domain), ("SITE_SCHEME", "http" if trial else "https"),
        # 键序与 src/wizard.mjs 的那份保持一致 —— 两个安装器的 .env 逐字节相同,
        # 由 tests/distribution 的 parity 用例钉着。
        ("PUBLIC_BASE", public_base),
        ("DSH_SITE", TRIAL_CADDY_SITE if trial else f"https://{domain}"),
        ("BIND_ADDRESS", "127.0.0.1" if trial else "0.0.0.0"),
        ("HTTP_PORT", "8787" if trial else "80"), ("HTTPS_PORT", "8443" if trial else "443"),
        ("DHC_DEV", "1" if trial else "0"), ("DHC_CONFIG_DIR", "./config"),
        ("PRICING_FILE", "pricing.cny.json"), ("AUTH_SECRET_FILE", "/run/secrets/auth_secret"),
        ("UPSTREAM_BASE_URL", "https://api.deepseek.com/v1"), ("UPSTREAM_API_KEY", ""),
        # 联网搜索: 留空则 web_search 不可用。zhipu 走 open.bigmodel.cn。
        ("SEARCH_PROVIDER", "zhipu"), ("ZHIPU_SEARCH_API_KEY", ""),
        ("ADMIN_EMAILS", str(options.get("adminEmail", ""))),
        ("MAIL_SMTP_HOST", ""), ("MAIL_SMTP_USER", ""), ("MAIL_SMTP_PASS", ""), ("MAIL_FROM", ""),
        ("GOOGLE_LOGIN_CLIENT_ID", ""), ("GOOGLE_LOGIN_CLIENT_SECRET", ""),
        ("GITHUB_LOGIN_CLIENT_ID", ""), ("GITHUB_LOGIN_CLIENT_SECRET", ""),
        ("DHC_SERVER_IMAGE", manifest["images"]["server"]),
        ("CADDY_IMAGE", manifest["baseImages"]["caddy"]),
        ("POSTGRES_IMAGE", manifest["baseImages"]["postgres"]),
        # 云工作台。自部署给了域名就默认开 —— 那是招牌功能, 装完没有它等于交付了
        # 半个东西。还差一条 DNS 记录 (work.<域名> 指向本机), 装完面板会说。试用
        # 模式必定关: localhost 与 work.localhost 是不同 host, 会话 cookie 过不去。
        ("WORK_ENABLED", "0" if trial else "1"),
        ("WORK_DOMAIN", "" if trial else f"work.{domain}"),
        ("COOKIE_DOMAIN", "" if trial else f".{domain}"),
        ("COMPOSE_PROFILES", "" if trial else "work"),
        ("SOCKET_PROXY_IMAGE", manifest["baseImages"]["socketProxy"]),
        ("WORK_IMAGE", manifest["images"]["workspace"]),
    ]
    values = apply_answers(values, answers or {})
    return "".join(f"{key}={value}\n" for key, value in values)


def initialize(parsed: dict, manifest: dict, deployment_plan: dict | None = None, answers: dict | None = None) -> Path:
    deployment_plan = deployment_plan or plan(parsed, manifest)
    target = target_of(parsed)
    if target.exists():
        entries = list(target.iterdir())
        if (target / ".dsh-cloud").exists():
            raise CliError(f"deployment is already initialized: {target}")
        if entries:
            raise CliError(f"refusing to overwrite non-empty directory: {target}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    templates, config = template_roots()
    for name in ("docker-compose.yml", "Caddyfile", "compose.build.yml", "compose.postgres.yml"):
        if (templates / name).is_file():
            shutil.copy2(templates / name, target / name)
    compose_path = target / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8").replace(
        "      - dhc-data:/app/data\n",
        "      - dhc-data:/app/data\n      - ./secrets/auth_secret:/run/secrets/auth_secret:ro\n",
    )
    atomic_write(compose_path, compose)
    shutil.copytree(config, target / "config")
    secret = os.environ.get("DSH_CLOUD_TEST_RANDOM_HEX", secrets.token_hex(32))
    atomic_write(target / "secrets/auth_secret", f"{secret}\n", 0o600)
    atomic_write(target / ".env", generated_env(parsed, manifest, answers), 0o600)
    atomic_write(target / ".gitignore", ".env\nsecrets/\n.dsh-cloud/lock\n")
    state = {
        "schemaVersion": 1, "version": manifest["version"], "stackSchema": manifest["stackSchema"],
        "mode": parsed["options"].get("mode", "trial"),
        "projectName": parsed["options"].get("projectName", "dsh-selfhost"),
        "publicBaseUrl": deployment_plan["publicBaseUrl"],
        "composeFiles": ["docker-compose.yml"],
    }
    atomic_write(target / ".dsh-cloud/state.json", json.dumps(state, indent=2) + "\n", 0o600)
    return target


def action_for(command: str, options: dict) -> list[str]:
    if command in {"up", "start"}:
        return ["up", "-d", "--wait"]
    if command == "down":
        return ["down"]
    if command == "status":
        return ["ps", "--format", "json"]
    if command == "logs":
        return ["logs", *(["--follow"] if options.get("follow") else [])]
    return ["config", "--quiet"]


def restore_deployment(value: dict, state_path: Path, parsed: dict) -> None:
    try:
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CliError(f"invalid deployment state: {state_path}") from error
    project_name = persisted.get("projectName")
    if not isinstance(project_name, str) or not project_name:
        raise CliError(f"invalid deployment project name: {state_path}")
    environment: dict[str, str] = {}
    try:
        for line in (state_path.parent.parent / ".env").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, setting = line.split("=", 1)
                environment[key] = setting
    except OSError:
        pass
    value["projectName"] = project_name
    value["mode"] = persisted.get("mode", value["mode"])
    value["publicBaseUrl"] = environment.get("PUBLIC_BASE") or persisted.get("publicBaseUrl") or value["publicBaseUrl"]
    value["url"] = value["publicBaseUrl"]
    value["bindAddress"] = environment.get("BIND_ADDRESS") or ("127.0.0.1" if value["mode"] == "trial" else "0.0.0.0")
    value["dockerArgv"] = docker_argv(
        Path(value["directory"]), value["projectName"], action_for(parsed["command"], parsed["options"])
    )


def parse_compose_output(stdout: str) -> object | None:
    output = stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def public_identity_configured(target: Path) -> bool:
    environment: dict[str, str] = {}
    for line in (target / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            environment[key] = value.strip()
    return bool(
        (environment.get("MAIL_SMTP_HOST") and (environment.get("MAIL_FROM") or environment.get("MAIL_SMTP_USER")))
        or (environment.get("GOOGLE_LOGIN_CLIENT_ID") and environment.get("GOOGLE_LOGIN_CLIENT_SECRET"))
        or (environment.get("GITHUB_LOGIN_CLIENT_ID") and environment.get("GITHUB_LOGIN_CLIENT_SECRET"))
    )


def execute(parsed: dict) -> dict:
    manifest = load_manifest()
    if parsed.get("special") == "help":
        return {"text": HELP}
    if parsed.get("special") == "version":
        return {"text": manifest["version"] + "\n"}
    value = plan(parsed, manifest)
    state = Path(value["directory"]) / ".dsh-cloud/state.json"
    fresh_init = not state.is_file()
    # 只有全新部署才问; 已初始化的目录再问一遍等于诱导用户覆盖自己的配置。
    answers: dict = {}
    if should_run_wizard(parsed, is_tty=sys.stdin.isatty() and sys.stdout.isatty(), fresh_init=fresh_init):
        answers = prompt_answers(version=manifest["version"], mode=str(parsed["options"].get("mode", "trial")))
    if parsed["command"] == "init":
        if parsed["options"].get("dryRun"):
            return {"json": value}
        initialize(parsed, manifest, value, answers)
        return {"json": {**value, "initialized": True}}
    if parsed["command"] == "start" and fresh_init and not parsed["options"].get("dryRun"):
        initialize(parsed, manifest, value, answers)
    if state.is_file():
        restore_deployment(value, state, parsed)
    if parsed["options"].get("dryRun"):
        return {"json": value}
    if not state.is_file():
        raise CliError(f"not an initialized deployment: {value['directory']}")
    if (
        value["mode"] == "selfhost"
        and parsed["command"] in {"start", "up", "doctor"}
        and not public_identity_configured(Path(value["directory"]))
    ):
        raise CliError(
            f"public self-host requires SMTP or OAuth for the first verified account; "
            f"edit {Path(value['directory']) / '.env'} and run dsh-cloud up"
        )
    argv = docker_argv(Path(value["directory"]), value["projectName"], action_for(parsed["command"], parsed["options"]))
    prefix = json.loads(os.environ["DSH_CLOUD_TEST_COMMAND_JSON"]) if os.environ.get("DSH_CLOUD_TEST_COMMAND_JSON") else []
    capture = bool(parsed["options"].get("json"))
    result = subprocess.run(
        [*prefix, *argv] if prefix else argv,
        cwd=value["directory"], check=False, capture_output=capture, text=capture,
    )
    if result.returncode:
        raise CliError(f"Docker Compose exited with status {result.returncode}", result.returncode)
    if capture:
        response = {**value, "dockerArgv": argv}
        output = parse_compose_output(result.stdout)
        if output is not None:
            response["composeOutput"] = output
        if result.stderr.strip():
            response["composeError"] = result.stderr.strip()
        return {"json": response}
    if parsed["command"] not in {"start", "up"}:
        return {"text": ""}
    # 人在终端前就给完整指引; 管道/CI 里保持裸 URL, 免得打断既有脚本的解析。
    if not sys.stdout.isatty():
        return {"text": value["url"] + "\n"}
    env_text = (Path(value["directory"]) / ".env").read_text(encoding="utf-8")
    has_key = bool(re.search(r"^UPSTREAM_API_KEY=.+$", env_text, re.MULTILINE))
    prefix = command_prefix(resolved=shutil.which("dsh-cloud") or "", entry=sys.argv[0])
    return {"text": next_steps(url=value["url"], directory=value["directory"],
                               has_upstream_key=has_key, project_name=value["projectName"],
                               prefix=prefix,
                               work_domain=(f"work.{parsed['options'].get('domain')}"
                                            if value["mode"] == "selfhost" else ""),
                               dev_mail=value["mode"] != "selfhost")}


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        result = execute(parse_args(values))
        if result.get("text"):
            print(result["text"], end="")
        if result.get("json") is not None:
            print(json.dumps(result["json"], separators=(",", ":")))
        return 0
    except CliError as error:
        if "--json" in values:
            print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        else:
            print(f"error: {error}", file=sys.stderr)
        return error.exit_code
