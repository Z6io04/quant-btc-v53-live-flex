# 代理连接与 API 白名单

v53 的网络路径是：Python → 本机 Mihomo `127.0.0.1:17890` → 所选机场节点 → 欧易。

- `127.0.0.1`：服务器自己。
- `17890`：Mihomo 接收 HTTP 代理请求的本机端口。
- `config.json` 的 `proxy`：告诉本程序向这个入口发请求。不是浏览器网址，也不是欧易 API 白名单。
- Tailscale 的 `100.77.57.40`：你远程登录服务器用的地址，不是一般情况下欧易看到的公网来源。

v53 真钱版已显式读取 config.json 的代理字段，不需要给它另外设置 http_proxy 环境变量、network.json 或代理 .env。Mihomo 目录已在 `~/mihomo`，不会因为清理旧 v53 被删除。

## 检查

```bash
systemctl --user is-active mihomo-proxy.service
ss -ltn 'sport = :17890'
python3 "$HOME/btc-v53-live/launch.py" check
python3 "$HOME/btc-v53-live/launch.py" account-check
```

服务 active、端口监听、公开接口通过、私有账户检查通过是不同层次。API 白名单设置错误时，公开接口也可能正常，但账户检查失败。

## 白名单填哪个 IP

应填程序请求欧易时实际使用的公网出口 IP；不能把 `42.200.230.187` 当作每台机器、每个节点都通用的固定值。两个账户可以使用同一实际固定出口，但仍使用各自账户的 API。

在服务器通过同一代理查询当前出口参考：

```bash
curl --fail --silent --show-error --proxy http://127.0.0.1:17890 --noproxy '' --max-time 15 https://api.ipify.org
```

返回的地址代表这次查询的出口。如果机场按目的地分流，查询网站与欧易可能出口不同；以机场提供的固定出口信息、欧易私有接口检查为进一步依据。IP 查询不证明将来不变；切换节点、机场轮换出口后需要重新核对。不要因为报错就把白名单扩大成所有地址。

欧易支持每个 API 绑定多个 IP，官方建议绑定 IP：[欧易 API 文档](https://www.okx.com/docs-v5/#overview-api-key-security)。不要给量化 API 开提现权限，不要把密钥或订阅发到聊天中。
