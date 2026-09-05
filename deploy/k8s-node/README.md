# k8s 工作台节点 (WORK_BACKEND=k8s)

一台**常驻**机器上的单节点 k3s, 给按秒计费的 ECI 补一条"热节点"路: 镜像已经在
本地, 没有机房调度那 25 秒, Pod 起来就是应用自己的启动时间 (nginx 实测 1.7 秒,
ECI 同样的东西 34 秒)。后端实现见 `server/app/workbackend.py` 的 `K8sBackend`,
按产品分派见 `RoutedBackend` (`WORK_BACKEND_PRODUCTS=pi=k8s,openmanus=k8s`)。

首个节点是组内共享的 GPU 机 <node> (新加坡, 16C/123G, 与应用机不在一个 VPC)。
下面所有步骤都以"不重启、不碰机器上现有服务"为前提, 都能整条退掉。

## 1. 节点上装 k3s

```sh
cp k3s-config.yaml /etc/rancher/k3s/config.yaml      # 先看一遍里面的数字
curl -sfL https://get.k3s.io -o install.sh
INSTALL_K3S_CHANNEL=stable INSTALL_K3S_SYMLINK=skip INSTALL_K3S_SKIP_SELINUX_RPM=true sh install.sh server
k3s kubectl get nodes        # Ready 即可
k3s kubectl apply -f manifests.yaml
```

配置里每一项都有理由, 改之前读注释。几条踩过的:

- **`fail-cgroupv1=false`**: kubelet ≥ 1.36 默认拒绝 cgroup v1 (内核 5.10 的
  Alibaba Cloud Linux 3 就是 v1), 少这一行 k3s 起不来, 日志只在 journal 里。
- **`data-dir: /mnt/k3s`**: 镜像与容器根都在这; 系统盘小的机器别用默认路径。
- **`INSTALL_K3S_SYMLINK=skip`**: 机器上别人已经有 /usr/bin/kubectl 与 ctr,
  安装脚本会往 /usr/local/bin 放同名软链, PATH 里它排前面 —— 等于换掉别人的命令。
- **`system-reserved`**: 共享机上这是硬保险 —— kubelet 把 Pod 总量卡在
  capacity 减去它 (kubepods cgroup), 配额 (manifests.yaml) 只是第二道。改它要
  `systemctl restart k3s` (容器不受影响, 但别在拉镜像时重启, 拉取会被打断)。
- **镜像预拉**: 全切之前先把 30 个镜像拉到节点 (一个跑 `sh -c exit 0` 的多容器
  Pod 就行, 见 git 历史里的 prewarm-images), 串行拉约 20 分钟; 否则第一个打开
  Coze 的人要等它拉 5 GB。
- **`WORK_PROXY_CIDR=<TUNNEL_APP_IP>/32`**: Pod 看到的来源永远是隧道地址 (宿主与
  Caddy 容器都被 MASQUERADE 成它), OpenClaw 那类只认信任代理的产品按这个配。
- kubelet 会把 `vm.overcommit_memory` 设成 1、`kernel.panic` 设成 10 (它自己的
  内核参数清单)。装之前看一眼现值, 别在一台 `panic_on_oops=0` 的机器上惊讶。

私有镜像 (ghcr) 的拉取凭据**不用手工建**: dhc-server 配了 `WORK_REGISTRY_*`
(ECI 用的同一份) 就会在第一次建 Pod 前把 Secret `K8S_IMAGE_PULL_SECRET`
(默认 `dshwork-registry`) 写进命名空间 —— 这就是 Role 里给 secrets 权限的原因。
没配凭据的自部署可以自己建一个同名的 `docker-registry` Secret, 或者用公开镜像。

## 2. 应用机与节点之间的隧道 (不在一个 VPC 时)

应用机 (dhc-server 与 Caddy 所在) 要能**直接打到 Pod IP** —— 它把 Pod IP 写进
`X-Work-Upstream` 交给 Caddy 反代, 与 ECI 的内网 IP 是同一条路。两台机不在一个
VPC、又打不通对等连接时, 走一条 `ssh -w` 三层隧道:

```
应用机 <TUNNEL_APP_IP>  <-- tun0 over ssh :<TUNNEL_PORT> -->  <TUNNEL_NODE_IP> 节点
路由: 10.42.0.0/16 (Pod)  10.43.0.0/16 (Service)  经 tun0
```

- 节点侧 `tunnel-sshd-node.sh`: 另起一个 sshd 实例 (端口 <TUNNEL_PORT>, 只认应用机的一把
  key, 只允许 tun, 没有 shell/转发), **不碰机器原有的 sshd**。key 上的强制命令
  `dsh-tunnel-up` 给 tun0 配地址, 并在设备消失时退出。
- 应用机侧 `tunnel-client-app.sh`: systemd 服务 `dsh-tunnel` 常驻 `ssh -w`,
  掉线 5 秒重连; `LocalCommand` 在隧道建好后配本地 tun0 与两条路由。
  **单元文件里写 `%%T`**: `%T` 是 systemd 自己的占位符 (= /tmp), 会先于 ssh 展开。
- 安全组只放 TCP 时 WireGuard 用不了 (UDP), 这就是选 ssh 的原因; 同地域公网
  时延 1.3 ms, TCP-over-TCP 对这点流量没有影响。

Pod 侧的 API server 只监听节点自己 (6443 不在安全组放行范围), dhc-server 经隧道
用 `https://<TUNNEL_NODE_IP>:6443` 访问; 证书 SAN 里要有这个地址 (`tls-san`)。

## 3. dhc-server 这边

把 token 与 CA 放到应用机 `/root/dsh-k8s/{token,ca.crt}`, compose 已把该目录
只读挂到 `/run/dsh-k8s`。**属主必须是容器里跑服务的那个用户** (Dockerfile 里的
`dsh-cloud`, uid 10001), 目录 0755、文件 0600:

```sh
chmod 0755 /root/dsh-k8s; chown 10001:10001 /root/dsh-k8s/token /root/dsh-k8s/ca.crt
```

留成 root 0600 的症状 (2026-09-03 首次上线): 产品域名 500, 而且回收循环每分钟
抛一次 PermissionError —— 那一版里它会把**所有后端**的计量回收一起拖停。`.env`:

```
K8S_API_URL=https://<TUNNEL_NODE_IP>:6443
WORK_BACKEND=k8s                  # 全部产品; 或保持 eci 并用下一行只切几个
# WORK_BACKEND_PRODUCTS=pi=k8s,openmanus=k8s
WORK_PROXY_CIDR=<TUNNEL_APP_IP>/32      # Pod 看到的代理来源是隧道地址
```

`K8S_TOKEN_FILE` / `K8S_CA_FILE` 在 compose 里给定; `K8S_NAMESPACE` / `K8S_DATA_PVC`
的默认值与 manifests.yaml 一致。2026-09-03 先切 pi 试了一轮, 当天全切。

验证 (应用机上):

```sh
curl --cacert /root/dsh-k8s/ca.crt -H "Authorization: Bearer $(cat /root/dsh-k8s/token)" \
  https://<TUNNEL_NODE_IP>:6443/api/v1/namespaces/dsh/pods       # 200
docker exec dhc-server curl -s http://<某个 Pod IP>:<端口>/  # 容器里直连 Pod
```

## 4. 用户数据: 正本在 OSS (K8S_SYNC_OSS_*)

节点是公司机器, 挂不到老板账号的 NAS (跨账号跨 VPC); OSS 内网端点整个地域可达,
不分账号不分 VPC。于是每个 Pod 多两个容器 (`workbackend.K8sBackend._sync_containers`):

- `dsh-restore` 初始化容器: 起动前 `rclone sync oss:<bucket>/<prefix>/<hexid> /data`。
  OSS 是正本 —— 本地多出来的会被删。OSS 上没这个用户就保留本地 (新用户/迁移前)。
  拉失败不落 `/data/.dsh-restored` 标记, 同步器见不到标记不推 (半份数据不能上去)。
- `dsh-syncer` 原生 sidecar (initContainer + restartPolicy Always): 每 `K8S_SYNC_INTERVAL_S`
  推一次 home/workspace; 收到 TERM 时全量推 (含数据库目录)。原生 sidecar **在应用容器
  退出之后**才收 TERM (实测: app TERM → app 退出 → syncer TERM), 所以推上去的
  MySQL/Postgres 目录是停机后的一致状态。`terminationGracePeriodSeconds` 随之改为
  `K8S_SYNC_GRACE_S` (180)。
- rclone 参数: `--metadata` 保 mode/uid/gid/mtime, `--links` 保符号链接,
  `--s3-directory-markers --create-empty-src-dirs` 保空目录 (Postgres 数据目录里有一堆)。
- 凭据: dhc-server 把 `K8S_SYNC_OSS_ACCESS_KEY_ID/SECRET` 写成 Secret `dshwork-oss`,
  两个同步容器 `envFrom` 取; **应用容器拿不到** (用户的智能体跑在里面)。
  **密钥必须只对这一个 bucket 有权限**: 它落在公司机器上, 有 root 的人都读得到。
  RAM 策略 (把 bucket 名换掉):

  ```json
  {"Version": "1", "Statement": [{"Effect": "Allow", "Action": ["oss:*"],
    "Resource": ["acs:oss:*:*:dshcloud-work", "acs:oss:*:*:dshcloud-work/*"]}]}
  ```

**本地那份是工作副本, 不是正本**: 开了 OSS 同步后, 用户目录挂的是**带上限的
emptyDir** (`K8S_WORK_DISK_GB`, 默认 20 GiB), 不再是 PVC。理由:

- Pod 一删本地就释放。留在 PVC 上时每个"用户 × 产品"的副本永远留着 —— 实测某天
  22 个目录 717 MB 而当时**零个**工作台在跑, 按用户数线性堆积。
- PVC 没有任何配额机制, emptyDir 的 `sizeLimit` 是唯一能给单个工作台设上限的形态。
  超限 kubelet 驱逐该 Pod (那次会话未推送的改动会丢), 但写不满整块盘 —— 那块盘上
  还有公司的 docker 和别人的 workspace。
- 节点整体另有 `eviction-hard: nodefs.available<10%` 兜底; 为此 kubelet 的
  `root-dir` 必须指到 /mnt (见 k3s-config.yaml), 否则 emptyDir 落在系统盘上,
  nodefs 也盯错盘。

没开 OSS 同步的部署仍然用 PVC (`K8S_DATA_PVC`) 且不设上限 —— 那时数据只有本地这一份,
超限即销毁。

已知取舍: Pod 异常死亡会丢最后一次推送之后的改动 (间隔默认 5 分钟); 起动多一步
下载, 单容器产品秒级, Coze 那种几百 MB 实测 1 秒 (内网)。

迁移 (一次性, 在应用机上, `.env` 里已有 K8S_SYNC_OSS_* 时):

```sh
bash deploy/k8s-node/migrate_to_oss.sh          # NAS 上每个 <hexid>/ -> oss:<bucket>/<prefix>/<hexid>/
```

节点本地盘上已经有的目录 (切 OSS 之前那几天的) 用 `migrate-node-to-oss.yaml` 这个
Job 在节点上推 (凭据取集群里的 Secret, 不经人手); 先推节点 (新)、再推 NAS (旧),
两边都是 `--update`, 新的不被旧的盖掉。

## 5. 网络围栏 (安全)

**工作台里跑的是用户敲进去的任意命令**, 而节点是公司的共享机、落在公司内网上。
2026-09-03 迁移完成后实测 (从一个普通工作台 Pod 里打):

| 目标 | 迁移后 (无策略) | 加上 netpol.yaml |
|---|---|---|
| 节点 22 / 6379 / 6443 / 9300 / 111 | 可达 | 拒绝 |
| 节点上另一条线的 redis (免密, `PING` 回 `+PONG`) | 可达可写 | 拒绝 |
| 公司内网其它机器 | 可达 | 拒绝 |
| 节点上各 docker 网桥 (同机其它容器) | 可达 | 拒绝 |
| 生产机 (隧道对端 <TUNNEL_APP_IP>) | 可达 | 拒绝 |
| 邻居用户的工作台 Pod | 可达 | 拒绝 |
| 阿里云元数据 100.100.100.200 | 可达 (该机没绑 RAM 角色, 未泄凭据) | 拒绝 |
| 集群 API (匿名 401) | 可达 | 拒绝 |
| 公网 / 我们的网关 / OSS 内网端点 | 可达 | 可达 (产品要用) |

ECI 时代这些都被安全组挡着 —— 迁到 k8s 是一次**隔离降级**, 这份策略把那一档补回来:

```sh
k3s kubectl apply -f netpol.yaml     # 前提: k3s 没有 disable-network-policy
```

三个必须知道的细节:

- **关着网络策略时这个文件照样 apply 成功**, `get netpol` 也照样列出来, 但一条都不生效。
  唯一可靠的验证是从 Pod 里真打一次。
- **验证要看耗时, 不能只看 `nc -z` 的返回码**: 被策略拒绝是 0ms 的 refused, 而
  `nc -z` 在某些 busybox 构建上会把它读成成功。判据用
  `curl --connect-timeout 3 -w '%{http_code}' telnet://IP:PORT` 加计时。
- **新 Pod 有一个约 3 秒的窗口**策略还没下发到它 (实测 t=0 通、t=3 已挡)。这段时间里
  跑的是我们自己的启动脚本, 用户还没拿到页面, 所以不是可利用的口子 —— 但别把"启动
  瞬间打得通"当成策略没生效。

命名空间还打了 PodSecurity `enforce=baseline`: 拒掉 privileged / hostPath / hostNetwork
—— dhc-server 那个 token 有 create pod 权限, 没有这道闸, 它一旦泄漏就等于共享机的 root。

## 6. 内核隔离: gVisor (K8S_GVISOR_PRODUCTS)

网络围栏挡的是"从容器往外打"; 挡不住的是**内核漏洞逃逸** —— 容器与宿主共用内核, 且
以 root 跑, 而节点是台一年没重启的共享机。这台机没有嵌套虚拟化 (`/dev/kvm` 不存在),
Kata/Firecracker 上不了; gVisor 不需要虚拟化, 是这里唯一能补内核那一档的办法。

原理一句话: 容器的系统调用被一个用户态内核 (Sentry) 截走, 不再直达宿主内核; Sentry
自己又被 seccomp 锁在约五十个宿主 syscall 里。逃逸要连破两层。

节点上装 (k3s **不会**自动识别 runsc, 要手工告诉它的 containerd):

```sh
URL=https://storage.googleapis.com/gvisor/releases/release/latest/x86_64
for f in runsc containerd-shim-runsc-v1; do curl -sfL -o $f $URL/$f; curl -sfL -o $f.sha512 $URL/$f.sha512; done
sha512sum -c *.sha512 && install -m 0755 runsc containerd-shim-runsc-v1 /usr/local/bin/
cp containerd-config-v3.toml.tmpl /mnt/k3s/agent/etc/containerd/   # 追加 runsc 运行时 (config-v3 = containerd 2.x)
install -D -m 0644 runsc.toml /etc/containerd/runsc.toml
systemctl restart k3s && grep -c runsc /mnt/k3s/agent/etc/containerd/config.toml   # 应 > 0
k3s kubectl apply -f runtimeclass-gvisor.yaml
```

验证不能只看 Pod 起没起 —— 进去看 `uname -r`, gVisor 里是 `4.19.0-gvisor`, `dmesg`
第一行 "Starting gVisor..."。

`K8S_GVISOR_PRODUCTS` 支持按产品列 (`pi,claude-code,...`)、`*` (全部) 和 `*,-coze` (全部但
例外)。**线上是 `*`** (2026-09-05 起): 每个产品都给用户代码执行 (shell 工具 / 自定义节点 /
代码节点), 没有哪个"只是填表单"; 用 `*` 也让新接的产品默认进 gVisor, 不靠人记得加名单。
实测代价 (同一节点):

| 负载 | runc | gVisor |
|---|---|---|
| `npm install express` | 1 秒 | 7 秒 |
| 纯计算 2e8 次循环 | 1 秒 | 1 秒 |
| DNS / 网关 / 围栏 / emptyDir subPath | 正常 | 正常 |

先开的是**用户能拿到 shell 敲任意命令**的那几格 (2026-09-04), 次日全量。Coze/Dify 那种
十容器栈 (MySQL/ES/Milvus) IO 最吃亏, 冷启动会更慢, 但它们的代码节点 / 沙箱 / 插件
一样是用户代码, 留在 runc 等于把最重的栈放在最薄的隔离上。

另: 应用容器与我们的初始化/同步容器都去掉了 `K8S_DROP_CAPS` 里的 capability
(默认 NET_RAW/MKNOD/SYS_CHROOT/SETFCAP —— 历史逃逸链的常客, 工作台没有一个用得上)。
**产品自带的伴随容器保留 SYS_CHROOT**: bitnami 系镜像 (Coze 的 redis/etcd/elasticsearch)
入口脚本以 root 起再 `chroot --userspec=1001 /` 降权, 去掉它三个中间件各重启 10 次,
日志最后一行 `chroot: cannot change root directory to '/'` (2026-09-04 全员回归抓到);
Dify 用官方 postgres/redis 镜像 (gosu) 不受影响。用户代码不在伴随容器里跑, 留这一个不亏。
没有开 `allowPrivilegeEscalation: false`: uid 1000 的产品 (Claude Code/Codex) 可能要 sudo。

## 7. 隧道对端的防火墙 (tunnel-firewall.sh)

隧道把生产机 (<TUNNEL_APP_IP>) 直接接到节点上。没有这道墙时, 生产机能碰到节点上所有监听
0.0.0.0 的东西 —— 22、免密 redis 6379、kubelet 10250、frps 7000、别的组员的 python 服务
—— 而且节点开着 ip_forward, 生产机加一条路由就能经节点进公司内网。生产机是对公网开的
web 服务器, 假设它有一天被打穿。

```
scp tunnel-firewall.sh 248:/root/dsh-k8s-staging/
bash /root/dsh-k8s-staging/tunnel-firewall.sh install   # 装到 /usr/local/sbin, 挂进 dsh-tunnel-up
```

只碰 `-i tun0` 的包, 两条自己的链 (DSH-TUN-IN / DSH-TUN-FWD): 入向只放行到本机 6443,
转发只放行到 10.42/16 与 10.43/16, 其余记日志后丢。规则不持久, dsh-tunnel-up 在每次
隧道会话建立时重放。验证 (在生产机): `bash -c 'cat </dev/null >/dev/tcp/<TUNNEL_NODE_IP>/6379'`
应超时, 6443 应通。退场: `dsh-tunnel-firewall remove`。

## 8. 准入策略: 命名空间里的 Pod 必须在 gVisor 里 (admission-gvisor.yaml)

dhc-server 的服务账号能在 dsh 命名空间随意建 Pod; 令牌放在对公网开的生产机上。没有
这条策略, 偷到令牌的人建一个 runc + root 的 Pod 就直接对着节点内核动手。PodSecurity
baseline 管不到 runtimeClassName, 所以用 ValidatingAdmissionPolicy (集群级, 服务账号改不了):
runtimeClassName 必须是 gvisor; 不许 privileged / hostPath / hostNetwork / hostPID /
hostIPC / hostPort / 加 capability。

```
k3s kubectl apply -f admission-gvisor.yaml
k3s kubectl -n dsh run probe --image=busybox --restart=Never -- true   # 应被拒
```

**前提是 `K8S_GVISOR_PRODUCTS=*` 已上线并逐个验过**, 否则 runc 的产品全部起不来。

## 退场

```sh
# 节点
systemctl disable --now sshd-dsh-tunnel; rm -f /etc/ssh/sshd_tunnel_config /etc/ssh/dsh_tunnel_authorized_keys /usr/local/sbin/dsh-tunnel-up /etc/systemd/system/sshd-dsh-tunnel.service
/usr/local/bin/k3s-uninstall.sh      # 注意: 它会 iptables-save | grep -v KUBE- | iptables-restore, 重写一遍规则表
# 应用机
systemctl disable --now dsh-tunnel; rm -f /etc/systemd/system/dsh-tunnel.service /usr/local/sbin/dsh-tunnel-local-up
```

## 还没做的

- 「個人成品」页对 k8s 产品是空的 (`offline_workspace_dir` 返回 None): 正本在 OSS,
  应用机上还没挂 (可以 ossfs 只读挂 bucket 再指 K8S 的本地路径)。
- 容器仍以 root 跑 (多数产品镜像假定 root); gVisor 之内的 root。节点内核
  5.10, 一年没重启 —— 打补丁要重启, 那是机器管理员的决定。
- 单个工作台有 20 GiB 上限, 但**没有全局上限**: 14 个工作台各写满仍会超过这块盘的
  余量。兜底是 kubelet 的 nodefs<10% 驱逐 —— 它保的是节点 (以及同机的 docker), 代价是
  驱逐我们自己的 Pod。
- 单节点, 节点挂了这些产品整体不可用 —— 回落到 ECI 只要改 `WORK_BACKEND_PRODUCTS`。
