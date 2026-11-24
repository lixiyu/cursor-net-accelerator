# 菜单栏客户端（macOS）

极简菜单栏 App：拉起并保活本仓库的 `cursor-net-accelerator run`，一键打开状态页。

- 安装包名：`CursorNetAccelerator.app`（英文路径）
- 系统显示名：**Cursor 网络加速器**

## 行为

- 启动 App → 子进程跑 `cursor-net-accelerator run`
- 子进程异常退出 → App 内自动再拉起
- 退出 App → 先 `stop`（中和 DNS）再退；App 被强杀时由 wrapper 发现父进程没了并停掉子进程，不留孤儿
- 菜单：打开状态页（`http://127.0.0.1:8789/`）、打开服务日志、退出

日常用本 App；CLI 仍可调试，但不要和 App 同时起两份 `run`（会抢端口）。

## 依赖

- macOS 13+
- Xcode Command Line Tools（`swift`）
- 已安装的 `cursor-net-accelerator`（仓库 `.venv` 或 PATH）

## 开发运行

```bash
# 仓库根目录先装好 CLI
python3 -m venv .venv && source .venv/bin/activate && pip install -e .

cd apps/macos-menubar
./scripts/run-dev.sh
```

菜单栏应出现闪电图标；点「打开状态页」。

## 打包 .app

```bash
cd apps/macos-menubar
./scripts/pack-app.sh
# 安装到应用程序（Finder 里显示「Cursor 网络加速器.app」）
CURSOR_NET_ACCEL_INSTALL=1 ./scripts/pack-app.sh
open "/Applications/Cursor 网络加速器.app"
```

说明：磁盘包名用中文，是为了应用程序列表里看到中文；内部可执行文件仍叫 `CursorNetAccelerator`。

若找不到 CLI，任选其一：

```bash
export CURSOR_NET_ACCEL_BIN=/Users/xiyu/code/my-open-source/cursor-net-accelerator/.venv/bin/cursor-net-accelerator

# 或写入
# ~/Library/Application Support/cursor-net-accelerator/menubar.json
# { "bin": "/绝对路径/.venv/bin/cursor-net-accelerator" }
```

## 日志

- 服务：`~/Library/Application Support/cursor-net-accelerator/data/run.log`
- 单实例锁：`…/menubar.lock`
