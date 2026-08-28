"""垫片自检: 用 **Ark 的报文形状** 打它, 确认转译与型号映射都对。

刻意不走 ComfyUI 的节点: 节点的内部结构版本间会变, 而垫片对外的契约
(comfy.org 那套 /proxy/... 路径与报文) 才是要钉住的东西。
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8199"
STUB = "http://127.0.0.1:9797"


def call(method: str, path: str, payload: dict | None = None, base: str = BASE):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    # 直连桩读回显时也得带 UA —— 桩有一道「UA 不合格就 403」的闸 (那是在替生产的
    # Cloudflare 把关, 见 stub_gateway._check_ua), 不带就只能读回一句 403。
    req.add_header("User-Agent", "DSHCloud-ShimCheck/1.0")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # 整条读回来再解 —— 曾经这里截 300 字节, 报文一长 json.loads 就失败,
        # 于是 \uXXXX 转义留在字符串里, 断言中文永远不成立 (在售型号从 2 个涨到
        # 4 个时当场触发, 而垫片其实是对的)。
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def main() -> int:
    # 官方节点发的是厂商公开名 (dreamina-), 垫片要映射到我们在售的 (doubao-)
    code, out = call(
        "POST",
        "/proxy/byteplus/api/v3/contents/generations/tasks",
        {
            "model": "dreamina-seedance-2-5-260628",
            "content": [{"type": "text", "text": "一只猫在雪地里奔跑"}],
            "resolution": "480p",
            "duration": 5,
        },
    )
    if code != 200 or not isinstance(out, dict) or not out.get("id"):
        print(f"  ✗ 建任务: {code} {out}")
        return 1
    task_id = out["id"]
    print(f"  ✓ 建任务 -> Ark 形状的 id={task_id}")

    code, out = call("GET", f"/proxy/byteplus-seedance2/api/v3/contents/generations/tasks/{task_id}")
    if code != 200 or not isinstance(out, dict):
        print(f"  ✗ 查任务: {code} {out}")
        return 1
    if out.get("status") not in ("queued", "running", "succeeded", "failed"):
        print(f"  ✗ 状态词不是 Ark 的那套: {out}")
        return 1
    print(f"  ✓ 查任务 -> status={out['status']}")

    # 生图: 网关给 b64, 垫片必须落盘再交出一个 url —— 官方节点按 data[0]["url"] 取图
    code, out = call(
        "POST",
        "/proxy/byteplus/api/v3/images/generations",
        {"model": "gpt-image-2", "prompt": "一只柴犬", "size": "1024x1024"},
    )
    if code != 200 or not isinstance(out, dict):
        print(f"  ✗ 生图: {code} {out}")
        return 1
    data = out.get("data") or []
    if not data or not data[0].get("url"):
        print(f"  ✗ 生图没给出 url (官方节点按 data[0][\"url\"] 取图): {out}")
        return 1
    print(f"  ✓ 生图 -> {data[0]['url']}")

    # 选一个没在售的型号: 必须给出**能照做**的错误, 而不是「请求失败」。
    # 官方节点的下拉写死了 2.5/2.0/Fast/Mini, 我们过滤不了 —— 用户选到没定价的
    # 那个时, 得从错误里看出该换成哪个。
    code, out = call(
        "POST",
        "/proxy/byteplus/api/v3/contents/generations/tasks",
        {"model": "dreamina-seedance-2-0-mini", "content": [{"type": "text", "text": "x"}],
         "resolution": "480p", "duration": 5},
    )
    if code != 404 or not isinstance(out, (dict, str)):
        print(f"  ✗ 未在售的型号应当回 404: {code} {out}")
        return 1
    # 错误路径返回的是原始报文文本, 里面是 \uXXXX 转义 —— 得先解回来再匹配中文,
    # 否则断言永远不成立 (2026-08-28 我就这么误报过一次)。
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            pass
    body = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    if "当前可用" not in body and "没有可用型号" not in body:
        print(f"  ✗ 错误里没有告诉用户该换成哪个: {body[:200]}")
        return 1
    print("  ✓ 未在售的型号 -> 404 且列出了可用型号")

    # ---- OpenAI 官方图像节点 (GPT Image) ----
    # 节点下拉里就是 gpt-image-2 / 1.5, 与我们在售的 id 一字不差, 所以这条几乎是
    # 直通; 唯一要钉住的是**图必须以 b64_json 交出去** —— 节点的
    # validate_and_cast_response 优先读它, 这条路径上一次落盘都不该发生。
    code, out = call("POST", "/proxy/openai/images/generations",
                     {"model": "gpt-image-2", "prompt": "一只柴犬", "size": "1024x1024", "n": 1})
    if code != 200 or not isinstance(out, dict):
        print(f"  ✗ OpenAI 生图: {code} {out}")
        return 1
    # 变量名不与上面那条 byteplus 生图共用 —— 末尾还要用那条的 url 去取图,
    # 覆盖掉就会拿着 None 去 urlopen。
    oai_data = out.get("data") or []
    if not oai_data or not oai_data[0].get("b64_json"):
        print(f"  ✗ OpenAI 生图没给 b64_json: {str(out)[:200]}")
        return 1
    if not out.get("usage"):
        print(f"  ✗ OpenAI 生图没把 usage 带回去 (节点要拿它算价): {str(out)[:200]}")
        return 1
    print(f"  ✓ OpenAI 生图 -> b64_json {len(oai_data[0]['b64_json'])} 字符 + usage")

    # DALL·E 节点写死发 dall-e-3, 我们不卖 —— 必须列出该换成哪个
    code, out = call("POST", "/proxy/openai/images/generations",
                     {"model": "dall-e-3", "prompt": "x"})
    body = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    if code != 404 or "当前可用" not in body:
        print(f"  ✗ OpenAI 未在售型号应当 404 并列出可用: {code} {body[:200]}")
        return 1
    print("  ✓ OpenAI 未在售型号 -> 404 且列出了可用型号")

    # ---- Wan 官方节点 (DashScope 原生形状) ----
    code, out = call(
        "POST", "/proxy/wan/api/v1/services/aigc/video-generation/video-synthesis",
        {"model": "wan2.7-t2v",
         "input": {"prompt": "一只猫在雪地里奔跑"},
         "parameters": {"size": "1920*1080", "duration": 5}},
    )
    if code != 200 or not isinstance(out, dict) or not (out.get("output") or {}).get("task_id"):
        print(f"  ✗ Wan 建视频: {code} {out}")
        return 1
    wan_task = out["output"]["task_id"]
    print(f"  ✓ Wan 建视频 -> DashScope 形状的 task_id={wan_task}")

    # 转译本身要被钉住: DashScope 的 input.prompt / parameters.size / duration
    # 必须变成网关的 prompt / resolution / duration。不断言这一条, 把折算档位
    # 那段改坏也不会红 —— 而它错了的表现是**按错档计价**, 用户看不出来。
    _, sent = call("GET", "/llm/v1/_debug/last-video", None, base=STUB)
    if sent.get("resolution") != "1080p":
        print(f"  ✗ size 1920*1080 应折成 1080p, 实际 {sent.get('resolution')!r}")
        return 1
    if sent.get("duration") != 5 or sent.get("prompt") != "一只猫在雪地里奔跑":
        print(f"  ✗ prompt/duration 没原样转过去: {sent}")
        return 1
    print(f"  ✓ 字段转译 -> resolution={sent['resolution']} duration={sent['duration']}")

    # 图生视频: 首帧走 input.img_url -> 网关的 image_url
    call("POST", "/proxy/wan/api/v1/services/aigc/video-generation/video-synthesis",
         {"model": "wan2.7-i2v", "input": {"prompt": "p", "img_url": "data:image/png;base64,AAA"},
          "parameters": {"resolution": "720P", "duration": 5}})
    _, sent = call("GET", "/llm/v1/_debug/last-video", None, base=STUB)
    if sent.get("image_url") != "data:image/png;base64,AAA" or sent.get("resolution") != "720p":
        print(f"  ✗ 图生视频的首帧/分辨率没转对: {sent}")
        return 1
    print("  ✓ 图生视频 -> img_url 转成 image_url, 720P 折成 720p")

    # 轮到终态。桩前两次故意回 PROCESSING —— 转译必须把它映成非终态,
    # 否则节点会当场判定失败。
    seen_running = False
    for _ in range(8):
        code, out = call("GET", f"/proxy/wan/api/v1/tasks/{wan_task}")
        status = ((out or {}).get("output") or {}).get("task_status") if isinstance(out, dict) else None
        if status in ("PENDING", "RUNNING"):
            seen_running = True
            continue
        break
    if not seen_running:
        print("  ✗ 从没见到非终态: 轮询转译可能把 PROCESSING 当成了终态")
        return 1
    if status != "SUCCEEDED":
        print(f"  ✗ Wan 轮询没到 SUCCEEDED: {out}")
        return 1
    video_url = out["output"].get("video_url")
    if not video_url:
        print(f"  ✗ Wan 成功了却没有 video_url (视频节点就是读这个键): {out}")
        return 1
    # 图像节点读的是 output.results —— 同一条路径两个节点共用, 少一个键就取不到结果
    if not (out["output"].get("results") or [{}])[0].get("url"):
        print(f"  ✗ Wan 成功了却没有 output.results[0].url: {out}")
        return 1
    print(f"  ✓ Wan 轮询 -> SUCCEEDED, video_url + results 都在")

    # Wan 的失败必须是 **HTTP 200 + 顶层 code/message** —— 节点就是这么抛错的,
    # 回 404 只会变成一句「请求失败」, 用户看不到该换成哪个型号。
    code, out = call(
        "POST", "/proxy/wan/api/v1/services/aigc/video-generation/video-synthesis",
        {"model": "wan2.5-t2v-preview", "input": {"prompt": "x"}, "parameters": {"duration": 5}},
    )
    if code != 200 or not isinstance(out, dict) or out.get("output"):
        print(f"  ✗ Wan 未在售型号应当回 200 且不带 output: {code} {out}")
        return 1
    if "当前可用" not in json.dumps(out, ensure_ascii=False):
        print(f"  ✗ Wan 的错误里没告诉用户该换成哪个: {out}")
        return 1
    print("  ✓ Wan 未在售型号 -> 200 + code/message (节点能把中文原话抛出来)")

    # ratio 必须转达: 丢了它, 用户在节点里选 9:16 竖屏会拿回 16:9 横屏 —— 而且
    # 两边都不报错。wan2.7 起百炼改用 resolution+ratio, 不再认 size。
    call("POST", "/proxy/wan/api/v1/services/aigc/video-generation/video-synthesis",
         {"model": "wan2.7-t2v", "input": {"prompt": "竖屏测试"},
          "parameters": {"resolution": "720P", "ratio": "9:16", "duration": 5}})
    _, sent = call("GET", "/llm/v1/_debug/last-video", None, base=STUB)
    if sent.get("ratio") != "9:16" or sent.get("resolution") != "720p":
        print(f"  ✗ ratio/resolution 没转达 (竖屏会变横屏): {sent}")
        return 1
    print("  ✓ Wan 2.7 -> resolution+ratio 都转达了 (竖屏不会变横屏)")

    # Wan 3.0: 首帧从 input.img_url 改到了 input.media[] 里 type=first_frame
    call("POST", "/proxy/wan/api/v1/services/aigc/video-generation/video-synthesis",
         {"model": "wan3.0-video",
          "input": {"prompt": "p", "media": [{"type": "reference_image", "url": "https://x/ref.png"},
                                             {"type": "first_frame", "url": "https://x/first.png"}]},
          "parameters": {"resolution": "1080P", "ratio": "16:9", "duration": 6}})
    _, sent = call("GET", "/llm/v1/_debug/last-video", None, base=STUB)
    if sent.get("image_url") != "https://x/first.png":
        print(f"  ✗ Wan 3.0 的首帧没从 media[] 里挑出来 (挑错了或漏了): {sent}")
        return 1
    print("  ✓ Wan 3.0 -> media[] 里的 first_frame 转成 image_url")

    # Wan 3.0 的 auto 时长发过来是 -1。按秒计价的东西不能按未知长度卖 ——
    # 网关会当成 1 秒, 而上游可能出到 30 秒。必须在垫片这里就拦住并说清怎么办。
    code, out = call(
        "POST", "/proxy/wan/api/v1/services/aigc/video-generation/video-synthesis",
        {"model": "wan3.0-video", "input": {"prompt": "p"},
         "parameters": {"resolution": "720P", "ratio": "16:9", "duration": -1}},
    )
    body = json.dumps(out, ensure_ascii=False) if isinstance(out, dict) else str(out)
    if code != 200 or (isinstance(out, dict) and out.get("output")):
        print(f"  ✗ auto 时长应当被拒且不建任务: {code} {body[:200]}")
        return 1
    if "duration" not in body and "秒数" not in body:
        print(f"  ✗ 错误里没说该去改 duration: {body[:200]}")
        return 1
    print("  ✓ auto 时长(-1) -> 被拦下, 并指明去 duration 里选具体秒数")

    # 型号在售、但**这个分辨率**不在售 (wan2.7 的 480p 厂商不提供): 错误里必须
    # 出现分辨率, 否则用户会去换型号 —— 而他该做的是换分辨率。
    code, out = call(
        "POST", "/proxy/wan/api/v1/services/aigc/video-generation/video-synthesis",
        {"model": "wan2.7-t2v", "input": {"prompt": "x"},
         "parameters": {"size": "854*480", "duration": 5}},
    )
    body = json.dumps(out, ensure_ascii=False) if isinstance(out, dict) else str(out)
    if code != 200 or (isinstance(out, dict) and out.get("output")):
        print(f"  ✗ 分辨率不在售应当回 200 且不带 output: {code} {body[:200]}")
        return 1
    if "480p" not in body:
        print(f"  ✗ 错误里没提分辨率, 用户会去换型号: {body[:220]}")
        return 1
    print("  ✓ 分辨率不在售 -> 错误里带上了 480p (不是笼统说「型号未开放」)")

    # Wan 的生图: DashScope 那侧是异步契约, 而网关的生图是同步的 —— 垫片得自己
    # 造任务号并在后台跑, 不能让 POST 阻塞。
    code, out = call(
        "POST", "/proxy/wan/api/v1/services/aigc/text2image/image-synthesis",
        {"model": "gpt-image-2", "input": {"prompt": "一只柴犬"},
         "parameters": {"size": "1024*1024"}},
    )
    img_task = ((out or {}).get("output") or {}).get("task_id") if isinstance(out, dict) else None
    if code != 200 or not img_task:
        print(f"  ✗ Wan 建图任务: {code} {out}")
        return 1
    for _ in range(20):
        code, out = call("GET", f"/proxy/wan/api/v1/tasks/{img_task}")
        status = ((out or {}).get("output") or {}).get("task_status") if isinstance(out, dict) else None
        if status in ("PENDING", "RUNNING"):
            time.sleep(0.5)
            continue
        break
    if status != "SUCCEEDED" or not (out["output"].get("results") or [{}])[0].get("url"):
        print(f"  ✗ Wan 图任务没出图: {out}")
        return 1
    print(f"  ✓ Wan 生图 -> {out['output']['results'][0]['url']}")

    # ---- Qwen 官方图像节点 (DashScope multimodal, 同步) ----
    code, out = call(
        "POST", "/proxy/qwen/api/v1/services/aigc/multimodal-generation/generation",
        {"model": "qwen-image-3.0-pro",
         "input": {"messages": [{"role": "user", "content": [{"text": "一只柴犬"}]}]},
         "parameters": {"size": "2048*2048", "n": 1, "seed": 42}},
    )
    if code != 200 or not isinstance(out, dict):
        print(f"  ✗ Qwen 生图: {code} {out}")
        return 1
    try:
        qwen_url = out["output"]["choices"][0]["message"]["content"][0]["image"]
    except (KeyError, IndexError, TypeError):
        print(f"  ✗ Qwen 的响应不是 output.choices[].message.content[].image: {str(out)[:250]}")
        return 1
    print(f"  ✓ Qwen 生图 -> {qwen_url}")

    # size 必须转达 —— 网关按尺寸分档计价 (pro 的 1K 与 2K 差整整一倍),
    # 丢了就会按 2K 收钱却出默认尺寸的小图, 两边都不报错。
    _, sent = call("GET", "/llm/v1/_debug/last-image", None, base=STUB)
    if sent.get("size") != "2048*2048":
        print(f"  ✗ Qwen 的 size 没转达: {sent}")
        return 1
    if sent.get("prompt") != "一只柴犬":
        print(f"  ✗ Qwen 的提示词没从 messages[].content[] 里取出来: {sent}")
        return 1
    print("  ✓ Qwen 字段转译 -> prompt 从 messages 里取出, size 原样转达")

    # 图生图: content 里混着 image 项, 要变成网关的 image_url
    call("POST", "/proxy/qwen/api/v1/services/aigc/multimodal-generation/generation",
         {"model": "qwen-image-3.0-pro",
          "input": {"messages": [{"role": "user", "content": [
              {"image": "https://x/in.png"}, {"text": "换成夜景"}]}]},
          "parameters": {"size": "1328*1328"}})
    _, sent = call("GET", "/llm/v1/_debug/last-image", None, base=STUB)
    if sent.get("image_url") != "https://x/in.png" or sent.get("prompt") != "换成夜景":
        print(f"  ✗ Qwen 图生图的参考图/提示词没拆对: {sent}")
        return 1
    print("  ✓ Qwen 图生图 -> content 里的 image 拆成 image_url")

    # 网关 200 却没出图 (内容审核拦截就是这样): 必须报错, 不能回一个空的
    # choices —— 节点拿空数组会抛一句看不懂的话, 用户不知道是被拦了。
    code, out = call(
        "POST", "/proxy/qwen/api/v1/services/aigc/multimodal-generation/generation",
        {"model": "qwen-image-3.0-pro",
         "input": {"messages": [{"role": "user", "content": [{"text": "__noimage__"}]}]}},
    )
    if code != 200 or out.get("output") or not out.get("code"):
        print(f"  ✗ 「200 但没出图」应当变成带 code 的失败: {code} {out}")
        return 1
    print(f"  ✓ 网关 200 但没出图 -> code={out['code']}")

    # 未在售同样要走 DashScope 的失败形状 (200 + 顶层 code/message)
    code, out = call(
        "POST", "/proxy/qwen/api/v1/services/aigc/multimodal-generation/generation",
        {"model": "qwen-image-9.9",
         "input": {"messages": [{"role": "user", "content": [{"text": "x"}]}]}},
    )
    if code != 200 or not isinstance(out, dict) or out.get("output"):
        print(f"  ✗ Qwen 未在售型号应当回 200 且不带 output: {code} {out}")
        return 1
    if "当前可用" not in json.dumps(out, ensure_ascii=False):
        print(f"  ✗ Qwen 的错误里没告诉用户该换成哪个: {out}")
        return 1
    print("  ✓ Qwen 未在售型号 -> 200 + code/message")

    # 别家的官方节点必须给出「未接通」而不是被误接。ComfyUI 里另有两家的路径
    # 也以 /images/generations 结尾 (kling / xai) —— 按后缀路由会把它们
    # 的报文误当成我们的。
    for path in ("/proxy/kling/v1/images/generations",
                 "/proxy/xai/v1/images/generations",
                 "/proxy/runway/v1/image_to_video"):
        code, out = call("POST", path, {"model": "x", "prompt": "y"})
        body = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        if code != 404 or "VendorNotWired" not in body:
            print(f"  ✗ {path} 应当回「未接通」, 实际 {code} {body[:120]}")
            return 1
    print("  ✓ 未接通的厂商 -> 404 VendorNotWired (没有被后缀误接)")

    # blob 是二进制, 不能用上面那个 (它 json.loads)
    try:
        with urllib.request.urlopen(data[0]["url"], timeout=30) as r:
            blob = r.read()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 取不回那张图: {type(exc).__name__}: {exc}")
        return 1
    if not blob.startswith(b"\x89PNG"):
        print(f"  ✗ 取回来的不是 PNG: {blob[:12]!r}")
        return 1
    print(f"  ✓ 图能取回 ({len(blob)} 字节, PNG)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
