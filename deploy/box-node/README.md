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

- 底片已存: 命名快照 **`dsh-node`** (无过期, 最多存 10 份, 源 Box 删了也还在)。
  里面是装好的整台节点: k3s + gVisor + 命名空间/配额/PVC/服务账号 + 围栏 + 准入策略。
- 已实测通过: 建节点 → 隧道 → k8s API 200 → 起 Pod → **确认在 gVisor 内核里**
  (`uname -r` = `4.19.0-gvisor`) → 应用机与 dhc-server 容器都能直连 Pod IP。
- 已实测通过: **停机 → 开机, 集群原样回来** (节点 Ready, 配额/PVC/服务账号/围栏/
  准入策略/已拉的镜像全在)。这是"背着"能成立的前提。
- **一个没通的环节**: 隧道传输, 见下面「未决: 隧道怎么走」。演练时用的 `ssh -w`
  在 Box 落到 baremetal 之后开不了 tun, 需要老板在两个方案里挑一个。

## 一、建/重建一台 (底片没了才需要)

```sh
export BOX_ENVFILE=/path/to/.env          # 里面要有 BOX_API_KEY
bash deploy/box-node/box.sh limits        # 先看还能不能开 (试用档只有 2 台并发)
BOX=$(bash deploy/box-node/box.sh new default 7200)

tar -cz deploy/k8s-node deploy/box-node | ssh user@<box> 'tar -xz -C /tmp'
ssh user@<box> "sudo DSH_APP_IP=<应用机公网IP> DSH_TUNNEL_NODE_IP=10.99.1.2 \
                DSH_TUNNEL_APP_IP=10.99.1.1 bash /tmp/deploy/box-node/provision.sh '<应用机 root 公钥>'"

bash deploy/box-node/box.sh snap $BOX dsh-node     # 存成底片
bash deploy/box-node/box.sh stop $BOX              # 停机 = 免费
```

`provision.sh` 可重复跑, 每步打 PASS/FAIL。

## 二、激活 (248 没了的那天)

```sh
BOX_ENVFILE=... bash deploy/box-node/activate.sh <box id>
# 或者从底片新开一台:
BOX_ENVFILE=... DSH_FROM_SNAPSHOT=dsh-node bash deploy/box-node/activate.sh
```

七步: 开机 → 钉主机密钥 → 隧道 → 取凭据 → API 200 → 起 Pod 验 gVisor → 应用机直连 Pod。
**它不切流量。** 全绿之后最后三步是人手做的 (脚本会把命令打出来):

1. `deploy/prod/.env`: `K8S_API_URL=https://10.99.1.2:6443`, `WORK_PROXY_CIDR=10.99.1.1/32`
2. `/root/dsh-k8s/{token,ca.crt}` 换成新节点的 (属主 `10001:10001`, 0600 —— 留成
   root 0600 的症状是产品域名 500 且回收循环每分钟抛 PermissionError, 2026-09-03 栽过)
3. `docker compose restart api`, 然后真开一个工作台点一遍

**镜像是冷的。** 第一个开 Coze/Dify 的人要等拉 5GB。切之前先预热 (见 k8s-node/README 的
「镜像预拉」), 或者接受第一个人等。

## 三、演练 (不影响 248)

地址段是错开的 —— 248 是隧道 10.99.0.x + 集群 10.42/10.43, Box 是 10.99.1.x +
集群 10.44/10.45, 单元名也带 `-box`。两条隧道能同时在, 所以演练随时可做:

```sh
BOX_ENVFILE=... bash deploy/box-node/activate.sh <box id>   # 全绿即可, 不要切 .env
systemctl disable --now dsh-tunnel-box
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
- 主机密钥每次都变 (快照不含机器身份)。所以 `tunnel-client-box.sh` 不预置 known_hosts,
  由 `activate.sh` 每次经 ascii.dev 的 API 通道取回来现钉 —— 那是比 TOFU 更硬的信任根。

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
装公钥的字段叫 `key` 不是 `publicKey`。

**7. 试用档 = 2 台并发 / 25 小时 / 只有 small 与 default / ttl 必须 ≤ 7200s。**
`box.sh limits` 随时可查。并发池是**和口袋专家的云电脑共用的**(同一把 `BOX_API_KEY`),
建节点前先 `box.sh ls` 看看那边有没有人在用。真要上生产, DSH 应该单开一把 key ——
否则两条产品线抢并发, 账单也分不开谁花的。

## 未决: 隧道怎么走

应用机需要**主动**连到节点的两样东西: `6443` (k8s API) 和**任意 Pod IP 的任意端口**
(Caddy 就是按 `X-Work-Upstream: <pod ip>:<port>` 反代的)。所以必须是三层通路, 端口转发
不够。248 上用的是 `ssh -w` L3 隧道, 在 Box 上不可靠 (见坑 5)。两条路, 都要老板点头:

**A. WireGuard (推荐)** —— 盒子出站拨到应用机。免疫地址变化、自动重连、开销比 ssh 低。
代价: 要在**阿里云安全组**给应用机开一个入站 UDP 端口 (实测 51820 现在是不通的,
从盒子打过去应用机上一个包都收不到)。这一步只有你能点。

**B. sshuttle** —— 只用已经开着的出站 ssh, 不用动安全组, 今天就能通。
代价: 应用机上要 `apt install sshuttle` (Ubuntu 源里有, 1.1.1), 而且它会在应用机上
下 iptables 规则做透明拦截 (只针对 10.44/16 与 10.45/16 两段, 不碰 248 的 10.42/10.43)。
装东西和改生产机的 iptables 这两件我没自己做。

选定之后剩下的活很小: 换掉 `tunnel-client-box.sh` 这一个文件, `activate.sh` 的其余
六步都不动。

## 退场

```sh
# 应用机
systemctl disable --now dsh-tunnel-box; rm -f /etc/systemd/system/dsh-tunnel-box.service \
  /usr/local/sbin/dsh-tunnel-box-local-up /root/.ssh/known_hosts_dsh_box; systemctl daemon-reload
rm -rf /root/dsh-k8s-box
# Box
bash deploy/box-node/box.sh stop <box id>     # 停机就不花钱了; 要彻底删再 rm
bash deploy/box-node/box.sh rm <box id>
```

命名快照 `dsh-node` 是单独的东西, 删 Box 不会带走它 —— 不想留就
`curl -X DELETE .../named-snapshots/dsh-node`。
