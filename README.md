# codex-merge

当前会话遇到账号访问限制或 token 额度不足时，指定会话 ID，把它从一个 `CODEX_HOME` 迁移到另一个。工具使用 Codex 的 app-server 创建 fork，并把它导入目标 HOME。目标会话获得新的 ID；此后两边可以分别继续，对话不会自动同步。

## 运行要求与入口

- Python 3.10+、可用的 `codex` 命令，以及支持 `fcntl` 的系统。
- 只有一个 Python 脚本，不需要安装 Python 包。这个仓库所在机器已把 `codex-merge` 链接到 `~/.local/bin`。
- 在其他机器上，可以从仓库目录创建命令入口：

```bash
mkdir -p ~/.local/bin
ln -s "$(pwd)/codex_merge.py" ~/.local/bin/codex-merge
codex-merge --version
```

确保 `~/.local/bin` 在 `PATH` 中；也可以直接运行 `python3 codex_merge.py`。若 Codex CLI 不在 `PATH` 中，把 `--codex-bin /path/to/codex` 放在子命令前，例如 `codex-merge --codex-bin /path/to/codex fork SOURCE TARGET SESSION`。

## 选择账号目录

```bash
codex-merge homes
```

`homes` 扫描 `~/.codex`、`~/.codex_*`、`~/.codex-*`，并包含当前环境变量 `CODEX_HOME` 指向的目录。输出中的名称、唯一首字母短名和 `current` 都可用于后续命令；也可以直接传目录路径。未被扫描到的自定义目录需要用路径指定。

`auth.json present` 只表示文件存在，**不验证登录状态**，也不会读取或显示凭据。默认 HOME 的短名取系统用户名首字母；有冲突时不生成短名。先运行 `homes`，再使用本机实际显示的名称。

## 复制会话

```bash
codex-merge SOURCE TARGET SESSION --dry-run
codex-merge SOURCE TARGET SESSION
codex-merge SOURCE TARGET SESSION --resume
```

`SOURCE` 和 `TARGET` 是 `homes` 列出的名称或目录路径。`SESSION` 使用当前会话的完整 UUID；也可以使用至少 4 个字符、能够唯一匹配的 UUID 前缀。完整写法为 `codex-merge fork SOURCE TARGET SESSION`。

例如，本机 `homes` 若显示 `primary` 和 `secondary`，可以运行：

```bash
codex-merge primary secondary 01a0ad83-f4e0-4000-8000-000000000000 --dry-run
codex-merge primary secondary 01a0ad83-f4e0-4000-8000-000000000000 --resume
```

成功后会打印新会话 ID、备份目录、核对过的 turn 数和带有目标 `CODEX_HOME` 的 `codex resume` 命令。`--resume` 会在完成导入后直接运行该命令。

`--dry-run` 只读检查源会话的历史链、所需历史边界及目标已有 rollout 是否冲突，并显示目标目录是否存在。它不会创建 fork，也不会执行完整的数据库导入与 app-server 验证；正式运行仍可能发现其他问题。

## 数据与限制

- 源 HOME 只读。工具在隔离的临时 HOME 中调用 Codex app-server，然后把 fork 和所需历史导入目标 HOME。
- `auth.json`、配置、记忆和缓存不会复制；**会话内容会复制**，其中可能包含提示词、工具输出、路径或敏感信息。
- 导入前，目标已有的 history 和 state SQLite 数据库会备份到 `<TARGET>/backups/session-merge-*`。请按会话数据的敏感级别保护这些备份。
- 遇到目标已有的同名且内容不同的 rollout，工具会在临时 HOME 中重映射历史链 ID，避免覆盖目标文件。导入完成后会通过目标 app-server 的 `thread/read`、`thread/resume` 和 turn 数进行核对。
- 备份不会在失败后自动恢复；若导入中途失败，目标可能留有部分文件或数据库记录。先查看报错和备份，再处理目标 HOME。
- 压缩的 `.jsonl.zst` rollout 暂不支持。最好等源会话当前回复结束后再复制，以获得稳定的一次性快照。

查看完整参数：`codex-merge --help`、`codex-merge fork --help`。
