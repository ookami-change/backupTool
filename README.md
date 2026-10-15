# BackupTool

一个零依赖的本地目录定时备份小工具：在浏览器里选择要备份的目录和存放位置，设置间隔，后台自动定时备份。

## 功能

- 图形界面选择本地目录（macOS / Windows / Linux(zenity) 原生文件夹选择框）
- 多个备份任务，每个任务独立设置备份间隔、保留份数、排除规则
- 两种备份方式
  - **目录快照**：每次生成一份完整目录，未变化的文件与上一份硬链接，只占用变化文件的空间（类似 Time Machine）
  - **ZIP 压缩包**：每次生成一个 zip
- 自动清理超出保留份数的旧备份
- 备份完成后可自动上传到 **腾讯云 COS 存储桶**（零依赖，内置 COS 签名）
- 立即备份、暂停/启用、查看最近状态，日志写入 `~/.backup_tool/backup.log`
- macOS 可一键设置登录后自动在后台运行

## 使用

需要 Python 3.8+，无需安装任何第三方库。

```bash
python3 backup_tool.py
```

启动后自动打开 http://127.0.0.1:8765 ，新建任务即可。服务运行期间会按间隔自动执行到期任务。

其他命令：

```bash
python3 backup_tool.py --no-browser          # 只启动后台服务
python3 backup_tool.py --port 9000           # 指定端口
python3 backup_tool.py run-once              # 立即执行所有启用的任务后退出（可配合 cron 使用）
python3 backup_tool.py install-autostart     # macOS：登录后自动后台运行
python3 backup_tool.py uninstall-autostart   # macOS：取消自动运行
```

## 上传到腾讯云 COS

1. 在 [访问管理 → API 密钥](https://console.cloud.tencent.com/cam/capi) 获取 SecretId / SecretKey（建议使用只授权目标存储桶读写的子账号）
2. 打开管理界面底部「腾讯云 COS 设置」，填写密钥、存储桶名称（如 `examplebucket-1250000000`）、地域（如 `ap-shanghai`）和可选的路径前缀，保存后点「测试连接」
3. 编辑任务，勾选「备份后上传到腾讯云 COS」

密钥也可以不写入配置，改用环境变量 `COS_SECRET_ID` / `COS_SECRET_KEY` 提供。

上传规则：

| 备份方式 | COS 上的位置 | 说明 |
| --- | --- | --- |
| ZIP 压缩包 | `<前缀>/<任务名>-<任务ID>/<时间>.zip` | 远端与本地一样只保留最近 N 份 |
| 目录快照 | `<前缀>/<任务名>-<任务ID>/latest/...` | 把最新快照增量同步过去：只上传有变化的文件，删除源目录已删的文件。如需远端保留历史版本，可在存储桶开启「版本控制」 |

上传失败时任务状态显示为失败并给出原因，本地备份不受影响，下个周期会自动重试（目录快照模式会从中断处续传）。

## 备份目录结构

```
<目标目录>/<任务名>-<任务ID>/
    20261015-120000/        # 快照模式
    20261015-130000.zip     # ZIP 模式
```

## 说明

- 配置保存在 `~/.backup_tool/config.json`（权限 600），可用环境变量 `BACKUP_TOOL_HOME` 修改
- 服务只监听 `127.0.0.1`，并拒绝跨站请求
- 目标目录位于源目录内时会自动跳过，避免递归备份
- 符号链接不会被备份
