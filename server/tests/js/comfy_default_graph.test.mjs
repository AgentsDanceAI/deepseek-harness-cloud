/* ComfyUI 那张"未保存的工作流" —— 拿 products.py 真发下去的那段脚本, 对着上游
 * bundle 的**真实形状**跑一遍, 再把它产出的 JS 真执行一次。
 *
 * 为什么要有它: 上游前端里写死了一张默认图 (v0.34.1 是 Z-Image Turbo), 它要
 * z_image_turbo_bf16.safetensors 这类**本地模型文件**, 而我们这一格是纯编排器,
 * 一个都没有 —— 用户一打开就是两个红节点加"发现 2 个错误"(老板 2026-09-15 的
 * 截图)。我们靠原地改 `window.comfyAPI.defaultGraph.defaultGraph` 把它换掉。
 *
 * 这条路径**坏了不会有任何报错**: 上游哪天不再往 window 上挂那个对象, 或者
 * loadGraphData 不再 clone 而是直接用, 换图就静默失效, 红节点原样回来。
 *
 * 下面 UPSTREAM_DEFAULT_GRAPH 抄的是 v0.34.1 真 bundle 里 `var Lu={...}` 的形状
 * (节点列表截短了, 键名和层级没动), 抄的是**上游的样子**, 不是我们逻辑的复制品。
 *
 * 跑法: node --test server/tests/js/comfy_default_graph.test.mjs
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
function generatorScript() {
  const src = readFileSync(PRODUCTS, "utf8");
  const m = src.match(/_COMFY_DEFAULT_GRAPH = r"""([\s\S]*?)"""\n/);
  assert.ok(m, "products.py 里找不到 _COMFY_DEFAULT_GRAPH —— 名字改了就把这里一起改");
  return m[1];
}

// v0.34.1 的 `var Lu={...}`: 就是这两个加载器让用户看到红框。
const UPSTREAM_DEFAULT_GRAPH = {
  last_node_id: 71,
  last_link_id: 82,
  nodes: [
    { id: 9, type: "SaveImage", pos: [1280, 320], inputs: [{ name: "images", type: "IMAGE", link: 80 }] },
    {
      id: 68,
      type: "UNETLoader",
      properties: {
        "Node name for S&R": "UNETLoader",
        models: [{ name: "z_image_turbo_bf16.safetensors" }],
      },
      widgets_values: ["z_image_turbo_bf16.safetensors", "default"],
    },
    { id: 69, type: "CLIPLoader", widgets_values: ["qwen_3_4b.safetensors", "lumina2", "default"] },
  ],
  links: [],
  groups: [],
  config: {},
  extra: {},
  version: 0.4,
};

// 我们的预置图 (dsh_cloud/example_workflows/01 生图 (AI Store).json 的形状)。
const OUR_WORKFLOW = {
  id: "dshcloud-image-001",
  revision: 0,
  last_node_id: 2,
  last_link_id: 1,
  nodes: [
    { id: 1, type: "DSHCloudImage", widgets_values: ["一只柴犬在雪地里奔跑", "gpt-image-2", "1024x1024", 1] },
    { id: 2, type: "SaveImage", widgets_values: ["dshcloud"] },
  ],
  links: [[1, 1, 0, 2, 0, "IMAGE"]],
  groups: [],
  config: {},
  extra: {},
  version: 0.4,
};

/** 跑真脚本, 返回它写出来的那份 JS。 */
function buildExtension(workflows) {
  const dir = mkdtempSync(join(tmpdir(), "dsh-comfy-"));
  const src = join(dir, "example_workflows");
  mkdirSync(src);
  for (const [name, body] of Object.entries(workflows)) {
    writeFileSync(join(src, name), JSON.stringify(body), "utf8");
  }
  const out = join(dir, "js", "dsh_default_graph.js");
  execFileSync("python3", ["-c", generatorScript()], {
    env: { ...process.env, DSH_COMFY_WORKFLOWS: src, DSH_COMFY_EXT_JS: out },
    encoding: "utf8",
  });
  return readFileSync(out, "utf8");
}

/** 照上游的样子搭一个 comfyAPI, 把扩展跑进去, 返回同一个对象。 */
function runAgainstUpstream(js, ns) {
  const warnings = [];
  const fn = new Function("globalThis", "console", js);
  fn({ comfyAPI: ns ? { defaultGraph: ns } : undefined }, { warn: (m) => warnings.push(m), log() {} });
  return warnings;
}

test("默认图被换成我们那张能直接跑的, 而且是原地改", () => {
  const js = buildExtension({
    "01 生图 (AI Store).json": OUR_WORKFLOW,
    "02 生视频 (AI Store).json": { ...OUR_WORKFLOW, nodes: [{ id: 1, type: "DSHCloudVideo" }] },
  });
  const dg = structuredClone(UPSTREAM_DEFAULT_GRAPH);
  const ns = { defaultGraph: dg, defaultGraphJSON: JSON.stringify(UPSTREAM_DEFAULT_GRAPH) };

  assert.deepEqual(runAgainstUpstream(js, ns), []);

  // loadGraphData() 克隆的是模块里那个常量, 和 ns.defaultGraph 是同一个对象 ——
  // 所以判据是"同一个对象的内容变了", 不是"ns.defaultGraph 指向了新对象"。
  assert.equal(ns.defaultGraph, dg, "必须原地改; 重新赋值的话模块里的常量还是老图");
  assert.deepEqual(dg.nodes.map((n) => n.type), ["DSHCloudImage", "SaveImage"]);
  assert.equal(JSON.stringify(dg).includes("z_image_turbo_bf16"), false, "上游那张图必须一点不剩");
  assert.equal(JSON.stringify(dg).includes("qwen_3_4b"), false);
  assert.equal(JSON.parse(ns.defaultGraphJSON).nodes.length, 2, "JSON 那份也要一起换");
});

test("取排第一的那张 (文件名前缀 01/02 就是顺序)", () => {
  const js = buildExtension({
    "02 生视频 (AI Store).json": { ...OUR_WORKFLOW, nodes: [{ id: 1, type: "DSHCloudVideo" }] },
    "01 生图 (AI Store).json": OUR_WORKFLOW,
  });
  const dg = structuredClone(UPSTREAM_DEFAULT_GRAPH);
  runAgainstUpstream(js, { defaultGraph: dg });
  assert.deepEqual(dg.nodes.map((n) => n.type), ["DSHCloudImage", "SaveImage"]);
});

test("预置图自带的 id 要去掉 —— 留着会和用户新开的工作流撞", () => {
  const js = buildExtension({ "01 生图 (AI Store).json": OUR_WORKFLOW });
  assert.equal(js.includes("dshcloud-image-001"), false);
});

test("上游哪天不再挂 comfyAPI.defaultGraph: 出声警告, 不炸页面", () => {
  const js = buildExtension({ "01 生图 (AI Store).json": OUR_WORKFLOW });
  assert.deepEqual(runAgainstUpstream(js, undefined).length, 1);
  // 形状变了 (比如 nodes 不再是数组) 也不许硬改。
  const odd = { defaultGraph: { nodes: "nope" } };
  assert.equal(runAgainstUpstream(js, odd).length, 1);
  assert.equal(odd.defaultGraph.nodes, "nope");
});
