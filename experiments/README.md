# Anet 网络故障实验

`netem_lab.sh` 在临时 Linux network namespace 中建立真实 veth/TCP/TLS 链路，依次注入：

1. 无故障基线；
2. 单向 120 ms 延迟与 30 ms 抖动；
3. 单向 50 ms 延迟、10 ms 抖动和 5% 丢包；
4. 单向 50 ms 延迟和 1 Mbit/s 限速，传输 262,144 字节机器载荷；
5. 单向 100% 丢包黑洞，并验证 Directory Carrier failover。

运行环境需要 Linux/WSL、`iproute2`、`tc netem` 和免交互 sudo：

```bash
cd <SOURCE_ROOT>
bash experiments/netem_lab.sh runtime/experiments
```

脚本使用唯一 namespace/veth 名称和临时 `/tmp/anet-netem.*` 目录。无论成功、失败或中断，trap 都会终止 namespace 进程、删除 namespace/veth，并只删除经过前缀校验的临时目录。

输出为逐探针 JSONL 和黑洞探针 JSON。实验结果只证明指定内核故障模型中的行为，不能替代真实跨运营商和 DPI 环境验证。
