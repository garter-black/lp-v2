# M4 固定市场执行与运维草稿

本批实现离线可验证的实盘入口，**未部署、未开实盘、未投放私钥**。
代码、服务模板和下列命令均不授予合入、部署、真实交易或 48–72 小时运行权限。
实际启动前仍需逐项满足 #5 的门禁并获得 Ray 对目标主机、钱包、市场、上限和窗口的明文批准。
既有 `run --capital-usdc`、`lp-v2.service` 和 `deploy/deploy-lp-v2` 继续是只读路径。

## 启动输入与凭据

只支持 Linux、Python 3.12+ 和固定 `polymarket-client==0.2.0`，钱包必须为既有
`POLY_PROXY`。启动前从显式签名文件离线推导 signer 与 Proxy 地址，核对 L2 的
signer、wallet 和 `signature_type=1`；不匹配会在 SDK 钱包准备之前拒绝。
不自动发现密钥、派生 L2 凭据、创建钱包或提供 EOA/Deposit Wallet 模式。

以下文件均由 `lp-v2` 用户拥有，为无符号链接的普通文件，权限 **0600**；
其父目录也应限制访问。文件路径显式传入，不把秘密放进环境变量、进程参数、
Git、stdout 或 journal：

| 文件占位 | 内容 |
| --- | --- |
| `/etc/lp-v2/signing-key` | 仅该钱包的签名私钥文本，独立于 L2 文件 |
| `/etc/lp-v2/credentials.json` | 既有 L2 `key`、`secret`、`passphrase`、`signer_address`、`wallet_address`、`signature_type` |
| `/etc/lp-v2/relayer.json` | 既有 Relayer API 的 `key` 与 `address`，供 SDK Proxy 免 gas merge 使用 |

L2 凭据不能替代 Relayer 凭据。模板只留路径，不创建、分发或申请任何凭据。
真实凭据的准备与投放另需授权。

持久状态目录 `/opt/lp-v2/state` 由 `lp-v2` 独占、权限 **0700**。
保留其中的 `journal.sqlite`、WAL 与锁/进程结果文件；不要清空或复制一套状态来并发运行。
进程锁避免同一状态目录启动两个执行者；运维仍须保证同一钱包没有其他执行者。

下面只展示未来获批后的参数形状；占位值不能直接运行：

```sh
lp-v2 run --live --capital-usdc <已批准上限> --market <已批准condition_id> \
  --code-sha <已批准40位SHA> --private-key-file /etc/lp-v2/signing-key \
  --credentials-file /etc/lp-v2/credentials.json \
  --relayer-credentials-file /etc/lp-v2/relayer.json --state-dir /opt/lp-v2/state
```

`--market` 只允许一个完整 condition id；启动时固定该市场的两个 token、资金上限和 UTC
启动时间，不在运行中重读配置。`run.env` 仅放 `LP_V2_MARKET`、`LP_V2_CAPITAL_USDC`、
`LP_V2_CODE_SHA` 等非秘密身份。改上限只能改该文件并在另行批准的停止/重启后生效；
降低上限前必须核对现有占用。HTTP 仍只有 GET，没有修改、stop 或 restart 接口。

## 循环、恢复与停止

每 10 秒先读回自己的订单历史、成交、实际持仓和余额，重新对账后再运行已有 Engine。
仅发固定 token 的 BUY、post-only、GTD 限价单，到期为意图时间 +1 小时；不支持 SELL、
吃单、市价单或 cancel-all。原始意图时间保留，交易所到期字段按整数秒核对。

地区必须为明确 `blocked=false`，或者明确 `blocked=true`、`country=JP` 且账户明确
`closed_only=false`。缺失、类型错误、其他受限地区均不挂；`/healthz` 保留实际
blocked 身份，不把 JP 例外显示为地区检查通过。

SQLite 先持久写意图与签名身份，再发请求。拒绝、UNKNOWN、撤单尝试、成交、merge 回执及
每轮占用分项均追加记录，不能改写历史。重启后先用流水与交易所重新对账，恢复原成本与补腿
上限；不重发已存在身份的下单或 merge。未知订单不能仅凭活动订单列表缺失判为拒绝。
补腿按对侧持仓实际平均成本计算上限，旧单未确认终结前不替换。
固定市场跌出开仓流动性/中间价条件后，仍保留经验证的行情供已有持仓补腿/merge，
同时 eligible=false 禁止新开仓。费用、进行中状态、接单状态、身份、tick 和行情时效等执行硬检查仍生效；
普通只读扫描继续排除这些不符合开仓条件的市场。

merge 走 SDK Proxy 免 gas 路径。ack、现金增加或 relayer 的确认各自都不足以释放预留：
必须同一交易成功链上回执、该钱包 pUSD 入账日志、两个指定 token 的销毁日志，且实际余额
增加和两个 token 减少均与份数相等。缺任一证明保留 UNKNOWN 与资金预留，不盲目重发。
SDK 内部对已分类的限流/明确拒绝可能重试；适配器不会在超时或重启后再次提交该 merge 身份。
不相干的现金流或持仓变化会使严格读回无法核对，需人工调查，不能乐观放行。

未来获批停止命令为：

```sh
lp-v2 stop --state-dir /opt/lp-v2/state
```

stop 使用同一受保护目录里的进程锁、Linux 启动身份与 pidfd 发送 SIGTERM；陈旧、外来或
无法核验的进程不发信号。执行进程停止新挂，只按已知自己的订单号撤单，持续读回。
只有自有活动单为零、无可能仍在途的未决下单才返回 0；读回失败或 90 秒排空超时返回非 0，
写明订单、UNKNOWN、库存份数/成本及有时间戳的估值（缺新鲜行情则 UNKNOWN）。
残余未配对库存原样持有，不做清库存交易。stop 命令最多等 110 秒取得同一启动身份的最终结果。

`deploy/lp-v2-live.service` 与 `deploy/run-live` 是独立草稿，含 `ExecStop` 和
`TimeoutStopSec=150`，不把既有只读部署脚本改成实盘启动器。
尚未在目标主机验证 systemd、进程停止、重启、SDK 网关或链上行为。
服务发生失败后重启时仍先对账；不要把自动重启当作解除 UNKNOWN 的办法。

## 收益、时效与证据

`/summary` 保持 schema_version=1 与原七个 m4 字段，新增 `net_profit` 净额；
现有第 14 列在净收益率旁显示净额，仍为 14 列。
计分逐自有订单 GET；当天奖励读取全部分页，份额与返佣只读 GET，每 60 秒刷新一次，
按市场、钱包、资产及当天 **UTC** 过滤，缺数据/错误身份/不完整读回显示 UNKNOWN。
`freshness.income_as_of` 是收入源实际读回时间，`as_of` 只是状态快照发布时间。
失败轮次不把旧数值收益作为当前收益重新发布；账本最后已知占用附 `accounting` 状态与时间。

净收益计算为：

```text
net_profit = 当日奖励 + 当日返佣 + 当日配对毛利润
             + 库存毛浮动盈亏相对日初基准的变化 - 当日实际费用
net_yield_pct_per_day = net_profit / 当日已观察区间的时间加权平均占用 × 100
lp_yield_pct_per_day = 当日实际奖励 / 同一平均占用 × 100
```

配对毛利润与库存毛成本排除费用，费用仅扣一次；Ledger 原有资金和已实现利润仍使用
包含费用的成本。真实适配器目前只接受已验证零 maker fee 的已确认成交；费用不明、
未结算成交或费用市场不参与乐观计算。不是用配置的潜在奖励率代替实际奖励。

平均占用按已记录轮次间的占用作阶梯积分，属于观察频率下的时间加权估计，
不是每个链上事件的连续精确积分。分母缺失或为零时收益率 UNKNOWN，不能拿启动上限代替。
金额是当日累计，不把不足一天的数据外推成全年或保证回报。

连续轮次跨 UTC 日时（10 秒目标周期，实际间隔不超过 60 秒），保存新日首次观察到的持仓估值及真实行情时间作为开盘基准。
该时间可能稍晚于 00:00，明确记为 `observed_boundary`，不伪称精确午夜成交价。
重启沿用已有当天基准；停机跨日、持仓但缺开盘观察基准时，净收益 UNKNOWN，不能事后回填。
无隔夜库存时可由完整成交/回执历史证明基准为零。读回失败也会打断连续观察证明。

每轮 journal 的 `occupation` 保存资金分项、最后核验时间、未配对份数/成本/行情估值及
UNKNOWN 清单，失败轮次也记录。导出证据应脱敏，不把私有状态或凭据提交到公开仓。
本地离线测试、CI、评审、合入、部署、48–72 小时现场验收是不同结果，不能互相替代。
