/* OpenMausBot 的型号清单改写 —— 拿 products.py 真发下去的那段脚本, 对着上游 bundle
 * 的**真实形状**跑一遍。
 *
 * 为什么要有它: 那段脚本靠一个正则去换 `var STATIC_CLAUDE_MODELS = {...};`。正则
 * 一旦对不上 (换上游镜像时形状变了), 选择器就会继续摆着 claude-fable-5-1 这种我们
 * 根本不卖的名字 —— 用户选中了它, 机器人就一句话都不说, 界面上没有任何异常。
 * 2026-09-13 老板那格的 Pesto 就是这么废的。
 *
 * 下面那两段 fixture 是从真镜像 (openmausbot:acf88c4-r1 的 /app/dist-server/index.js)
 * 里原样抄出来的, 抄的是**上游的样子**, 不是我们逻辑的复制品。
 *
 * 跑法: node --test server/tests/js/openmausbot_models.test.mjs
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { test } from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const PRODUCTS = join(here, "..", "..", "app", "products.py");

/** 从 products.py 里取出真的那段脚本 (不复制, 直接用)。 */
function patchScript() {
  const src = readFileSync(PRODUCTS, "utf8");
  const m = src.match(/_OMB_PATCH_MODELS = r"""([\s\S]*?)"""\n/);
  assert.ok(m, "products.py 里找不到 _OMB_PATCH_MODELS —— 名字改了就把这里一起改");
  return m[1];
}

// 真镜像里的形状: 两段之间隔着别的代码, 每段都以顶格的 `};` 收尾。
const BUNDLE_FIXTURE = `var DRIVER_KIND2 = "claudeAgent";
var STATIC_CLAUDE_MODELS = {
  default: "claude-sonnet-5",
  options: [
    { id: "claude-fable-5-1", label: "Claude Fable 5.1" },
    { id: "claude-fable-5", label: "Claude Fable 5" },
    { id: "claude-opus-5", label: "Claude Opus 5" },
    { id: "claude-sonnet-5", label: "Claude Sonnet 5" },
    { id: "claude-haiku-4-5", label: "Claude Haiku 4.5" }
  ]
};
var CLAUDE_MODEL_ID = /^[a-z0-9][a-z0-9._:/-]*$/i;
function noise() { return { default: "x", options: [] }; }
var STATIC_CODEX_MODELS = {
  default: "gpt-5.6-sol",
  options: [
    { id: "gpt-5.6-sol", label: "GPT-5.6 Sol" },
    { id: "gpt-5.6-luna", label: "GPT-5.6 Luna" },
    { id: "gpt-5.4-mini", label: "GPT-5.4 Mini" }
  ]
};
var OFFICIAL_CODEX_PROVIDER = "openai";
`;

const SPEC = {
  STATIC_CLAUDE_MODELS: {
    default: "claude-fable-5",
    options: [
      { id: "claude-sonnet-5", label: "Claude-Sonnet-5" },
      { id: "claude-fable-5", label: "Claude-Fable-5" },
    ],
  },
  STATIC_CODEX_MODELS: {
    default: "gpt-5.6-luna",
    options: [{ id: "gpt-5.6-luna", label: "GPT-5.6-Luna" }],
  },
};

function sandbox({ bundle = BUNDLE_FIXTURE, bots = null } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "omb-"));
  mkdirSync(join(dir, "d"), { recursive: true });
  const paths = {
    dir,
    bundle: join(dir, "d", "index.js"),
    spec: join(dir, "spec.json"),
    bots: join(dir, "bots.json"),
    script: join(dir, "patch.js"),
  };
  writeFileSync(paths.bundle, bundle);
  writeFileSync(paths.spec, JSON.stringify(SPEC));
  writeFileSync(paths.script, patchScript());
  if (bots) writeFileSync(paths.bots, JSON.stringify(bots, null, 2));
  return paths;
}

function run(p) {
  return execFileSync(process.execPath, [p.script], {
    env: { ...process.env, DSH_OMB_BUNDLE: p.bundle, DSH_OMB_SPEC: p.spec, DSH_OMB_BOTS: p.bots },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

test("两段清单都换成在售的, 不卖的名字一个不剩", () => {
  const p = sandbox();
  run(p);
  const out = readFileSync(p.bundle, "utf8");
  for (const dead of ["claude-fable-5-1", "claude-haiku-4-5", "gpt-5.4-mini"]) {
    assert.ok(!out.includes(dead), `${dead} 还在清单里 —— 用户选中它就是一句 404`);
  }
  // 真的是能跑的 JS, 而且值就是我们给的那份。
  const claude = new Function(out + "\nreturn STATIC_CLAUDE_MODELS;")();
  const codex = new Function(out + "\nreturn STATIC_CODEX_MODELS;")();
  assert.deepEqual(claude, SPEC.STATIC_CLAUDE_MODELS);
  assert.deepEqual(codex, SPEC.STATIC_CODEX_MODELS);
  // 换的是那两段, 不是把周围的代码也吃掉了。
  assert.ok(out.includes("var CLAUDE_MODEL_ID"));
  assert.ok(out.includes("var OFFICIAL_CODEX_PROVIDER"));
  assert.ok(out.includes("function noise()"));
});

test("形状对不上就退出非零 —— 宁可 Pod 起不来也别静默不改", () => {
  const p = sandbox({ bundle: "var SOMETHING_ELSE = 1;\n" });
  assert.throws(() => run(p), (e) => e.status === 3);
});

test("选中已下架型号的机器人拨回默认; 在售的和别家驱动的不动", () => {
  const p = sandbox({
    bots: [
      { id: "b1", name: "Pesto", modelSelection: { instanceId: "c1", model: "claude-fable-5-1" } },
      { id: "b2", name: "Nova", modelSelection: { instanceId: "c1", model: "claude-sonnet-5" } },
      { id: "b3", name: "Gus", modelSelection: { instanceId: "g1", model: "grok-4.6" } },
      { id: "b4", name: "Cody", modelSelection: { instanceId: "x1", model: "gpt-5.4-mini" } },
    ],
  });
  run(p);
  const got = Object.fromEntries(
    JSON.parse(readFileSync(p.bots, "utf8")).map((b) => [b.name, b.modelSelection.model]),
  );
  assert.equal(got.Pesto, "claude-fable-5", "已下架的要拨回 claude 那家的默认");
  assert.equal(got.Cody, "gpt-5.6-luna", "codex 那家按 codex 的默认, 别串到 claude 去");
  assert.equal(got.Nova, "claude-sonnet-5", "在售的选择是用户自己定的, 不许动");
  assert.equal(got.Gus, "grok-4.6", "别家驱动的型号我们不管, 动了才是真的搞坏");
});

test("没有 bots.json (头一次开这格) 不算错", () => {
  const p = sandbox();
  run(p); // 不抛就算过
});
