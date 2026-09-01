// 一个进程把两半拼起来: /langgraph/* -> LangGraph (2024), 其余 -> 前端 (3001)。
//
// 为什么要这一层: 前端是**浏览器**去连 LangGraph 的, 所以那个地址必须是浏览器
// 能到的。直接写 http://localhost:2024 只在用户自己机器上成立 —— 在云工作台里
// 浏览器和容器不是一台机器。走同源的 /langgraph 前缀最省事: 不用开跨域, 也不用
// 再给 LangGraph 单独申请一个域名和一套鉴权。
import http from "node:http";

const UP = { "/langgraph": 2024 };
const DEFAULT_PORT = 3001;

http.createServer((req, res) => {
  let port = DEFAULT_PORT;
  let path = req.url;
  for (const [prefix, p] of Object.entries(UP)) {
    if (req.url === prefix || req.url.startsWith(prefix + "/") || req.url.startsWith(prefix + "?")) {
      port = p;
      path = req.url.slice(prefix.length) || "/";
      break;
    }
  }
  const up = http.request(
    { host: "127.0.0.1", port, path, method: req.method, headers: req.headers },
    (r) => { res.writeHead(r.statusCode || 502, r.headers); r.pipe(res); },
  );
  up.on("error", (e) => { res.writeHead(502); res.end("upstream: " + e.code); });
  req.pipe(up);
}).listen(3000, "0.0.0.0", () => console.log("[dsh] 前置反代 :3000 -> 前端 3001 / langgraph 2024"));
