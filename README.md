# BackupTool

一个零依赖的定时备份小工具：在浏览器里选择要备份的本地目录（可多选），按间隔自动增量上传到**腾讯云 COS 存储桶**，本地不额外占用空间。

## 功能

- 系统原生目录选择框，支持多选（macOS 按住 ⌘；Linux 需 zenity；Windows 每次选一个，可多次添加）
- 多个备份任务，每个任务可包含多个目录，独立设置间隔（默认 120 分钟）和排除规则
- 增量上传：只上传新增或修改过的文件，4 个文件并发上传，中断后下次自动续传
- 可选「同步删除」：本地删除的文件也从 COS 删除（默认关闭，COS 上保留）
- 立即备份、暂停/启用、查看最近状态，日志写入 `~/.backup_tool/backup.log`
- macOS 可一键设置登录后自动在后台运行

## 使用

需要 Python 3.8+，无需安装任何第三方库。

```bash
python3 backup_tool.py
```

启动后自动打开 http://127.0.0.1:8765 ：

1. 在「腾讯云 COS 设置」填写 SecretId / SecretKey（在 [访问管理 → API 密钥](https://console.cloud.tencent.com/cam/capi) 获取，建议使用只授权目标存储桶的子账号）、存储桶名称（如 `examplebucket-1250000000`）、地域（如 `ap-shanghai`）和可选的路径前缀，保存后点「测试连接」
2. 新建任务：点「选择目录…」选择一个或多个目录，保存即可

服务运行期间会按间隔自动执行到期任务。

其他命令：

```bash
python3 backup_tool.py --no-browser          # 只启动后台服务
python3 backup_tool.py --port 9000           # 指定端口
python3 backup_tool.py run-once              # 立即执行所有启用的任务后退出（可配合 cron 使用）
python3 backup_tool.py install-autostart     # macOS：登录后自动后台运行
python3 backup_tool.py uninstall-autostart   # macOS：取消自动运行
```

## COS 上的目录结构

```
<路径前缀>/<任务名称>/<目录名>/<目录内的相对路径>
```

例如任务「工作资料」包含 `~/Documents/合同` 和 `~/Pictures/扫描件`，前缀为 `backups`：

```
backups/工作资料/合同/...
backups/工作资料/扫描件/...
```

同一任务内目录名不能重复，任务名称不能重复。

## 说明

- COS 上保存的是每个文件的最新版本；如需保留历史版本，可在存储桶开启「版本控制」
- 某个目录暂时不存在（如移动硬盘未接入）时会跳过并提示，不会删除它在 COS 上的文件
- 上传失败的文件会在任务状态中提示，下个周期自动重试
- 配置保存在 `~/.backup_tool/config.json`（权限 600），密钥也可改用环境变量 `COS_SECRET_ID` / `COS_SECRET_KEY` 提供；可用环境变量 `BACKUP_TOOL_HOME` 修改配置目录
- 已上传文件的记录保存在 `~/.backup_tool/manifests/`，删除后下次会全量重新上传
- 服务只监听 `127.0.0.1`，并拒绝跨站请求
- 符号链接不会被备份
