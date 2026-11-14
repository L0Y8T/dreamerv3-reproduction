---
name: gpu-arbiter
description: 本机只有一块 RTX 4090 Laptop（16 GB），但有多条线在抢。任何要占用 GPU 的动作（训练、推理、评测、吞吐探针、任何跑几分钟以上的 GPU 脚本）开始前必须先读本 skill，用 gpuq.ps1 拿令牌再启动；也用于回答「显卡现在忙不忙」「谁在用」「还要多久」。没拿令牌就启动 GPU 任务=违反硬规则。
---

# 一块 GPU，一个令牌

## 为什么有这个 skill

这台机器上已经因为"没有单一事实源"白烧过至少 8 小时机时：

| 日期 | 事故 | 代价 |
|---|---|---|
| 2026-11-11 12:19 | 停训练时先杀 `train.py`、后杀驱动，中间 1 秒驱动又拉起下一个 run | 脏启动 |
| 2026-11-11 13:03 | 从开始菜单重启 | 挂着的队列静默死亡，一整天没跑 |
| 2026-11-12 02:59 | Windows 更新连续重启三次 | 一个 run 跑到 232,128 步被杀，约 2 小时 |
| 全程 | 各队列用 `pgrep train.py` 判断"有没有人在跑" | 只能看见我们自己的进程，看不见别的窗口 |

**共同根因**：每条线各自猜。现在改成"问令牌服务"。

---

## 四条铁规则（违反即事故）

```
R1 独占，不抢占。
   一个时刻只有一个 GPU 任务持有令牌。任何人不得杀掉别人正在跑的 GPU 进程
   —— 这些训练没有中途 checkpoint，杀一次等于把别人几小时作废。

R2 先申请，再启动。
   要占 GPU 就先 acquire，拿不到就排队。排队是唯一合法的等待方式。

R3 租约 + 心跳。
   持有者每 60 秒心跳一次，租约 10 分钟。超时未续 + 宽限 5 分钟 = 视为已死，可被回收。
   这条专治"驱动静默死亡"。

R4 免占用窗口 20:00 → 次日 01:00 不发放令牌。
   用户要用电脑。要突破必须由用户明确指令，并加 -Override，审计日志会记下来。
```

---

## 开工前的固定动作（三步，缺一不可）

```powershell
$GPUQ = "$env:USERPROFILE\.dsh\skills\gpu-arbiter\gpuq.ps1"

# 第 1 步：体检。这一步会额外报告「系统是否待重启」——2026-11-12 那次就是被这个杀的
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GPUQ doctor

# 第 2 步：申请令牌（阻塞，直到拿到为止）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GPUQ acquire `
    -Label "cheetah-dreamer-s0" -MemMiB 13000 -EtaMin 264

# 第 3 步：起训练；训练期间后台每 60 秒 heartbeat；结束（成功或失败）都 release
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GPUQ heartbeat -Id <id>
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GPUQ release   -Id <id>
```

**最省事的写法是用 `run` 包住整条命令**（自动 acquire → 后台心跳 → 结束自动 release）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GPUQ run `
    -Label "cheetah-dreamer-s0" -MemMiB 13000 -EtaMin 264 `
    -Command "wsl.exe -d Ubuntu -u root -- bash /mnt/c/Users/10348/Desktop/工作/DreamerV3复现/wsl_cheetah_resume.sh"
```

---

## 命令表

路径固定：`C:\Users\10348\.dsh\skills\gpu-arbiter\gpuq.ps1`

| 命令 | 用途 |
|---|---|
| `status` | 谁占着、租约还剩多久、队列里有谁 |
| `acquire -Label x -MemMiB 13000 -EtaMin 264` | 阻塞申请 |
| `acquire -Try ...` | 试一下能不能拿，不真占；能拿退出码 0，不能拿退出码 2 |
| `heartbeat -Id <id>` | 续租（一般由包装脚本自动调） |
| `release -Id <id>` | 交还 |
| `log -Tail 20` / `log -Day 2026-11-13` | 审计流水 |
| `doctor` / `doctor -Fix` | 体检 / 顺手清掉陈旧租约 |
| `run -Label x -Command "..."` | 一键包住一条命令 |

**从 WSL 里调用**（Windows 路径要写成反斜杠形式）：

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
  "C:\Users\10348\.dsh\skills\gpu-arbiter\gpuq.ps1" status
```

或者用同目录的 `gpuq.sh`：

```bash
bash /mnt/c/Users/10348/.dsh/skills/gpu-arbiter/gpuq.sh status
```

---

## 状态与协议（文件即协议，任何窗口都能直接读）

```
C:\Users\10348\.gpuq\
├── state.json    当前持有者（不存在 = 空闲）
├── mutex.lock    原子互斥，只在改写 state 的瞬间存在
├── queue\        排队中的申请，一个文件一个
├── log.jsonl     审计流水：grant / heartbeat / release / steal / deny
└── config.json   阈值：窗口、预留显存、确认次数、租约与宽限
```

发放条件四条全过才给：

| # | 条件 | 默认 |
|---|---|---|
| 1 | 没有有效租约（或已过期超宽限） | 宽限 5 分钟 |
| 2 | 空闲显存 ≥ 申请值 + 预留 | 预留 1500 MiB |
| 3 | 利用率 < 10%，连续 3 次采样 | 约 3 分钟 |
| 4 | 不在 20:00–01:00 窗口内（除非 `-Override`） | — |

---

## 拿到令牌之后的义务

```
K: 每 60 秒心跳一次。忘了续租 = 别人会合法回收并接管。
K: 结束必须 release，成功失败都要。崩了不留 release 会让下一个人等 15 分钟。
K: 不许杀别人的 GPU 进程。看到冲突就排队，不要 "清理"。
K: 起跑前先查 logdir 会不会撞车：同名目录已存在但步数不够 = 上次被打断，
   必须先改名归档（带日期与被杀步数），否则新旧数据混在一起。
```

---

## 常见误判（都真实发生过）

| 误判 | 真相 |
|---|---|
| `pgrep -f train.py` 没结果 → GPU 空闲 | 只能看见我们自己的进程。别人的任务不叫 train.py |
| 现在 01:00 了 → 可以开跑 | 时间只是条件之一，还要显存与利用率 |
| 显存空着 → 没人在算 | 有 `util < 10%` 这一道，就是为了防这个 |
| 我 kill 了自己起的训练 → 干净了 | 驱动可能还在，1 秒内会拉起下一个 run（2026-11-11 就这样） |

---

## 已知的坑（带日期，别重犯）

```
X: 2026-11-10 —— PowerShell 5.1 读无 BOM 的 UTF-8 脚本会按 ANSI 解码，中文变乱码，
   甚至造出乱码文件夹（桌面上出现过 `宸ヤ綔`）。所有 .ps1 一律保持纯 ASCII。
X: 2026-11-11 12:19 —— 停任务必须先杀驱动、再杀训练。反了会触发"驱动以为跑完了"的竞态。
X: 2026-11-12 02:59 —— Windows 更新会强制重启。已写入
   HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU\NoAutoRebootWithLoggedOnUsers=1，
   但严格说要配「活动时间」才稳；`doctor` 每次都会报待重启状态，看到就要留意。
X: 不许用 `wsl bash -lc "..."` 传含中文和引号的命令 —— PowerShell 会吃掉引号，
   报 unexpected EOF。一律写成 .sh 文件再 bash <路径> 执行。
```

---

## 边界（明确不做）

```
X: 不做抢占、不做优先级插队 —— 因为不能杀进程。
X: 不做多任务共享显存 —— 我们的任务峰值约 12.7 GB，剩不下让第二个任务安全跑的量。
X: 不自动重试 —— 失败原因要人看日志，盲目重试会把"环境坏了"跑成"跑了一夜没结果"。
X: 不做常驻服务 —— 文件即协议，没有会死的守护进程。
```

---

## 看板（一眼看清所有任务与历史）

双击打开 `C:\Users\10348\Desktop\工作\GPU统筹\dashboard.html`，里面有时间线、正在跑的任务、历史任务表、被中断并归档的 run、系统重启记录、GPU 令牌流水。

页面每 30 秒自动重载；内容由 `dashboard.ps1` 重新生成（要持续更新就得有一个刷新循环在跑）：

```powershell
# 只生成一次
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\10348\Desktop\工作\GPU统筹\dashboard.ps1"

# 持续刷新（每 30 秒）
powershell -NoProfile -ExecutionPolicy Bypass -Command "while(`$true){ & powershell -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\10348\Desktop\工作\GPU统筹\dashboard.ps1' *> `$null; Start-Sleep -Seconds 30 }"
```

数据全部只读，不会影响任何正在跑的任务。

---

## 相关文件

| 文件 | 内容 |
|---|---|
| `C:\Users\10348\.dsh\skills\gpu-arbiter\gpuq.ps1` | 令牌程序本体（唯一副本，纯 ASCII） |
| `C:\Users\10348\.dsh\skills\gpu-arbiter\gpuq.sh` | WSL 侧薄封装 |
| `C:\Users\10348\Desktop\工作\GPU统筹\dashboard.ps1` | 看板生成器（UTF-8 **带 BOM**，因为含中文） |
| `C:\Users\10348\Desktop\工作\GPU统筹\dashboard.html` | 看板页面（双击打开） |
| `C:\Users\10348\Desktop\工作\GPU统筹\wsl_dash_data.sh` | 看板的数据探针（WSL 侧） |
| `C:\Users\10348\Desktop\工作\GPU统筹\GPU统排规范_2026-11-13.md` | 设计说明与事故档案 |

---

## 任务板（跨窗口沟通的唯一入口）

**任何窗口在占用 GPU 之前，先读这一页：**

```
C:\Users\10348\Desktop\工作\GPU统筹\任务板.md
```

里面有三样东西：① 今天的窗口期与可用时段 ② 待跑任务与排期（谁先谁后） ③ 执行流水（谁在什么时候跑到什么时候）。

**规矩**：开工前读板子 → 跑 `gpuq acquire` 拿令牌 → 结束 `gpuq release` → **在板子上回写一行**。不回写等于别人不知道你在跑。

**已知两个坑（都踩过）**：
1. 令牌必须在**起进程之前**拿。gpuq 的显存闸会把你自己的任务挡住（本机已占 12 GB 时 free 不足，acquire 永远失败）。
2. 竞争激烈时互斥锁可能超时，现在会打印 `WARN: mutex timeout`，看到就重试，不要当成"已拿到"。