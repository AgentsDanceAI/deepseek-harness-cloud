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

## 退场

```sh
# 节点
systemctl disable --now sshd-dsh-tunnel; rm -f /etc/ssh/sshd_tunnel_config /etc/ssh/dsh_tunnel_authorized_keys /usr/local/sbin/dsh-tunnel-up /etc/systemd/system/sshd-dsh-tunnel.service
/usr/local/bin/k3s-uninstall.sh      # 注意: 它会 iptables-save | grep -v KUBE- | iptables-restore, 重写一遍规则表
# 应用机
systemctl disable --now dsh-tunnel; rm -f /etc/systemd/system/dsh-tunnel.service /usr/local/sbin/dsh-tunnel-local-up
```

## 还没做的

- 用户数据在节点本地盘 (PVC 是 local-path), 与 ECI 那边的 NAS 不是同一份:
  同一个人在 k8s 产品和 ECI 产品里看到的是两个家目录。「個人成品」页对 k8s
  产品是空的 (`offline_workspace_dir` 返回 None)。
- 隔离是容器级 (共享内核), ECI 是每实例一台微 VM。要补的话在节点上给
  k3s 配 gVisor (`runtimeClassName`), 后端加一个字段即可。
- 单节点, 节点挂了这些产品整体不可用 —— 回落到 ECI 只要改 `WORK_BACKEND_PRODUCTS`。
