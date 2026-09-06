# 备用工作台节点 (ascii.dev Box)

248 是组内共享的 GPU 机 —— 不是我们的资产, 哪天被收回去, 16 格工作台一起消失。
这个目录是那一天的**后备**: 一台在 ascii.dev Box 上、与 248 等价的 k3s 节点, 平时
**停机 (免费)**, 要用的时候一条命令唤醒。

不是重写。`K8sBackend`、manifests、gVisor、网络围栏、准入策略、OSS 同步、STS ——
一行代码都没动, 因为换掉的是**机器**不是**后端**。切过去只改 `.env` 两行加一份凭据。

    deploy/k8s-node/    248 那套 (manifests / netpol / gvisor / 准入策略) —— 备用节点直接复用
    deploy/box-node/    只放"因为是 Box 所以不一样"的那几样

| | 248 | Box (default 档) |
|---|---|---|
| 规格 | 16C / 123G (共享, 给别人留 6C/64G) | 4C / 8G 独占 (large 8C/16G, xlarge 16C/32G) |
| 给 Pod 的量 | ~10C / 56Gi | ~3C / 5.6Gi |
| 内核 / cgroup | 5.10 / v1 (一年没重启) | 6.8 / v2 |
| 位置 | 新加坡 | 欧洲 (Hetzner 或 baremetal, **每次 resume 都可能换**) |
| 成本 | 0 (借的) | 运行 $0.036/h, **停机 0** |
| 装得下的产品 | 全部 16 格 | 除 Coze (16G) 外都行; Coze 要 xlarge |

## 现在的状态 (2026-09-06)

**整条链已经跑通并且冷启动验过**。底片是命名快照 **`dsh-node`**(无过期, 最多 10 份,
源 Box 删了也还在), 里面是装好的整台节点: k3s + gVisor + 命名空间/配额/PVC/服务账号
+ 围栏 + 准入策略 + WireGuard。平时 Box 停机 = **不花钱**。

`activate.sh` 全绿的六步 (最后一轮是**停机 → 开机之后一个字没改**直接跑出来的):

1. Box 唤醒
2. WireGuard 自己握上手 (盒子出站拨, 地址换了也不用管)
3. token / ca.crt 取回应用机
4. k8s API 走隧道 200
5. 真起一个 Pod: `uname -r` = `4.19.0-gvisor` (确认在沙箱里, 不是"起来了"就算)
   + `dshwork-data` 卷绑上、写得进读得回 (用户数据走的就是这条路)
6. 应用机宿主与 `dhc-server` 容器都能直连 Pod IP (Caddy 反代靠的就是这个)

演练全程 248 那条隧道一直 `active`。

## 一、建/重建一台 (底片没了才需要)

```sh
# 凭据默认从应用机的 /root/dsh-k8s-box/box.env 读 (BOX_API_KEY + BOX_ORG), 不用设环境变量
bash deploy/box-node/box.sh limits        # 应该显示 standard; 显示 trial 就是 org 没带上
BOX=$(bash deploy/box-node/box.sh new default 7200)

# 应用机: 生成密钥并拿到公钥 (已经跑过就跳过, 重跑不会换密钥)
bash deploy/box-node/tunnel-wg-144.sh init

tar -cz deploy/k8s-node deploy/box-node | ssh user@<box> 'tar -xz -C /tmp'
ssh user@<box> "sudo DSH_WG_SERVER_PUBKEY=<上面那个公钥> DSH_WG_ENDPOINT=<应用机IP:51820> \
                DSH_TUNNEL_NODE_IP=10.99.1.2 DSH_TUNNEL_APP_IP=10.99.1.1 \
                bash /tmp/deploy/box-node/provision.sh"
# provision 末尾会打印盒子的公钥, 拿回来登记:
bash deploy/box-node/tunnel-wg-144.sh peer <盒子公钥>
bash deploy/box-node/tunnel-wg-144.sh up

bash deploy/box-node/box.sh snap $BOX dsh-node     # 存成底片
bash deploy/box-node/box.sh stop $BOX              # 停机 = 免费
```

`provision.sh` 可重复跑, 每步打 PASS/FAIL。

## 二、激活 (248 没了的那天)

```sh
bash deploy/box-node/activate.sh <box id>
# 或者从底片新开一台:
DSH_FROM_SNAPSHOT=dsh-node bash deploy/box-node/activate.sh
```

六步: 开机 → 隧道握手 → 取凭据 → API 200 → 起 Pod 验 gVisor → 应用机直连 Pod。
**它不切流量。** 全绿之后最后三步是人手做的 (脚本会把命令打出来):

1. `deploy/prod/.env`: `K8S_API_URL=https://10.99.1.2:6443`, `WORK_PROXY_CIDR=10.99.1.1/32`
2. `/root/dsh-k8s/{token,ca.crt}` 换成新节点的 (属主 `10001:10001`, 0600 —— 留成
   root 0600 的症状是产品域名 500 且回收循环每分钟抛 PermissionError, 2026-09-03 栽过)
3. `docker compose restart api`, 然后真开一个工作台点一遍

**镜像是冷的。** 第一个开 Coze/Dify 的人要等拉 5GB。切之前先预热 (见 k8s-node/README 的
「镜像预拉」), 或者接受第一个人等。

## 三、演练 (不影响 248)

地址段是错开的 —— 248 是隧道 10.99.0.x + 集群 10.42/10.43 + `tun0`, Box 是 10.99.1.x +
集群 10.44/10.45 + `dshbox0`。两条隧道能同时在, 所以演练随时可做:

```sh
bash deploy/box-node/activate.sh <box id>   # 全绿即可, 不要切 .env
bash deploy/box-node/tunnel-wg-144.sh down
bash deploy/box-node/box.sh stop <box id>
```

`activate.sh` 会把 `systemctl is-active dsh-tunnel` (通往 248 的那条) 打出来 —— 演练
全程它必须一直是 active。

## 四、Box 这个平台的几个坑 (都是实测撞出来的)

**1. 快照不收 `/var`。** 收的是 `/etc /usr /opt /root /srv /home/user` 和 docker 命名卷。
k3s 默认把数据放 `/var/lib/rancher`, 照默认装, 停一次机整个集群连同几十 GB 镜像一起
没了 —— 而且 resume 回来 k3s 会"干净地"重新初始化, **不报错, 只是空的**。所以
`data-dir: /opt/dsh-k3s`, `root-dir` 也跟着走。

**2. 快照会把 k3s 的解包目录留成空壳。** k3s 是自解压的, 头一次跑把二进制解到
`<data-dir>/data/<sha>/bin/`。停机再开机后, 那个目录还在、里面是空的 (集群数据
`agent/` `server/` 和 173M 的 containerd 镜像都好好的, 唯独这一坨没了)。而 k3s 只看
目录在不在就决定要不要解包 → `exec: "k3s-server": executable file not found`, 每 5 秒
重启一次, 日志里只有这一行。`provision.sh` 装了一个 `ExecStartPre` 自查闸: 解包目录在
但 `k3s-server` 不在就删掉重解 (几秒)。装完第二次停机/开机实测: 零次报错, 直接 active。

**3. resume = 换一台机器, 地址和主机密钥都会变。**
- 公网地址变 (实测: `195.201.218.72` → `94.130.26.233` → 纯 IPv6)。
- **`ip` 字段可能是 IPv6**, 落到 baremetal 时就是 —— 应用机没有 v6 出口, 直接不可达。
  这时 `sshEndpoint` 才有值, 给的是 IPv4:高端口。`box.sh ssh` 按 sshEndpoint → IPv4
  的顺序取, 脚本一律用它, **别用 `ip`**。
- 主机密钥每次都变 (快照不含机器身份), `known_hosts` 里的旧条目会让 ssh 直接拒连。
  隧道改成 WireGuard 之后**日常不再依赖 ssh** (它只认公钥, 不认地址); 只有装机/重装
  要 ssh 进去传文件时才会撞上, 那时先 `ssh-keygen -R <地址>` 再连, 或者干脆
  `bash box.sh run <box> ...` 走 ascii.dev 的 API 通道。

**4. 除 22 外的入站端口全被挡。** 实测: 自己在盒子里起的 50005 从外面不通, 22 通。
`POST /boxes/{id}/host` 只开 HTTPS 路由 (`https://<子域>-<端口>.on.ascii.dev`, 最多 50 个,
默认带 `_token`), 不是裸 TCP。所以隧道只能走 22 或者出站。

**5. 落在哪台机器不由我们定, 能力也跟着变。** 头一次是 hetzner 的 VM (公网 IPv4,
sshd 能开 tun); resume 之后落到 `machineProvider: baremetal` (私网 IPv4 + 公网 IPv6 +
sshEndpoint 代理, **sshd 开不了 tun**, `-w any:0` 与 `-w any:any` 都是
`channel 0: open failed`)。root 自己 ioctl 建 tun 是好的, `PermitTunnel point-to-point`
也确实生效了 (`sshd -T` 查过), 就是 sshd 开不出来。**结论: `ssh -w` 在 Box 上不是
可靠传输**, 别把备用节点建在它上面。

**6. REST 路径和文档正文对不上。** 以 `https://docs.ascii.dev/openapi/box-v1.yaml`
为准: 是 `/boxes/{id}/sshkey` 不是 `/ssh-key`, `/boxes/{id}/host` 不是 `/hosting`,
存命名快照是 `POST /named-snapshots {boxId,name}` 不是 `POST /boxes/{id}/snapshots/{name}`,
删除是 `DELETE /boxes/{id}` 不是 `POST /boxes/{id}/delete`, 装公钥的字段叫 `key` 不是 `publicKey`。

**7. 付费记在哪个钱包上, 要显式指定 (`BOX_ORG`)。** 2026-09-06 实测: 订阅买在
`AgentsDance` 这个 org 上, 而 API key 默认走 `Personal` 钱包 —— 于是账号明明扣过款,
`limits` 还报 `trial`、开到第 3 台就 `limit_reached: Trial accounts can run 2 concurrent
boxes`。**症状看起来像"付款没到账", 实际是问错了钱包。** 带上 org 之后同一把 key 立刻是
standard/100 台 (实测连开 10 台, 每台约 1 秒, 全部删除干净)。

```sh
bash deploy/box-node/box.sh orgs                    # 列钱包
BOX_ORG=<id> bash deploy/box-node/box.sh limits     # 哪个是 standard 用哪个
```

`box.sh` 默认从应用机的 `/root/dsh-k8s-box/box.env` 读 `BOX_API_KEY` 与 `BOX_ORG`
(转成 `X-Box-Org` 头), 所以不用每次记得设; `BOX_ORG` 为空时它会在 stderr 上吵一句 ——
这个失败是静默的, 不吵就会有人以为「付过钱怎么只能开 2 台」。**故意不放
`deploy/prod/.env`**: 那份被 compose 以 `env_file` 注进 dhc-server, 而它根本不用 Box。⚠️ 口袋专家那条线的
`backend/dataset/agent_box.py` **没有**带 org, 所以云电脑现在是花在个人的试用额度上。

**8. 试用档 = 2 台并发 / 25 小时 / 只有 small 与 default / ttl 必须 ≤ 7200s。**
`box.sh limits` 随时可查。并发池是**和口袋专家的云电脑共用的**(同一把 `BOX_API_KEY`),
建节点前先 `box.sh ls` 看看那边有没有人在用。真要上生产, DSH 应该单开一把 key ——
否则两条产品线抢并发, 账单也分不开谁花的。

## 隧道 (WireGuard)

应用机需要**主动**连到节点的两样东西: `6443` (k8s API) 和**任意 Pod IP 的任意端口**
(Caddy 就是按 `X-Work-Upstream: <pod ip>:<port>` 反代的)。所以必须是三层通路, 端口
转发不够。

248 上用的是 `ssh -w`; Box 上不行 (见坑 5), 改成 **WireGuard, 由盒子出站拨过来**:

    盒子 dsh0 (10.99.1.2)  --UDP-->  应用机 dshbox0 (10.99.1.1):51820

- 应用机侧的对端**不写 Endpoint** —— 盒子每次 resume 换地址, WireGuard 记住最近一次
  握手的来源即可。所以 resume 之后不用改任何配置, 这正是选它而不是 ssh 的原因。
- 盒子侧 `AllowedIPs` 只有 `10.99.1.1/32`: 节点不需要经隧道去别处, 而 AllowedIPs
  同时是"只接受这个对端发来的这些源地址"的白名单, 写宽了等于把围栏拆了。
- 密钥在两端的 `/etc/wireguard` 里 (Box 那份进快照, 所以**重装/resume 都不用重配对端**)。
  `provision.sh` 只在没有密钥时才生成 —— 换密钥会让应用机登记的对端当场失效, 而症状
  只是"隧道就是不通", 没有一处会说是为什么。
- 方向是单向的: 应用机主动连节点, 节点**不需要**反过来碰应用机 (工作台 Pod 调网关
  走公网, netpol 的出站白名单把整个 10/8 排除了)。所以应用机侧的围栏
  (`dsh-box-wg-fence`, 由 wg-quick 的 PostUp 挂上) 只放行"已建立"的回程包 —— 盒子那把
  私钥即使泄了也主动进不来。节点侧的围栏沿用 `k8s-node/tunnel-firewall.sh` (只放行到
  6443 与转发到 10.44/10.45)。

**前提: 阿里云安全组放行应用机的入站 UDP 51820。** 只有控制台能点。没放行时的症状是
接口起着、`wg show` 里 `latest handshake` 一直空 —— `activate.sh` 第 2 步会直接这么报。
WireGuard 对没带正确密钥的包一个字节都不回, 所以开着这个端口不增加可被扫描的面。

应用机上装了 `wireguard-tools` (内核模块本来就有)。查状态:

```sh
bash deploy/box-node/tunnel-wg-144.sh status
wg show dshbox0 latest-handshakes        # 第二列非 0 = 通了
```

## 退场

```sh
# 应用机
bash deploy/box-node/tunnel-wg-144.sh remove
rm -rf /root/dsh-k8s-box
# Box
bash deploy/box-node/box.sh stop <box id>     # 停机就不花钱了; 要彻底删再 rm
bash deploy/box-node/box.sh rm <box id>
```

命名快照 `dsh-node` 是单独的东西, 删 Box 不会带走它 —— 不想留就
`curl -X DELETE .../named-snapshots/dsh-node`。
