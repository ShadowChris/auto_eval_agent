# Linux 部署：auto-eval-operation

仓库只维护带中文注释的 `auto-eval-operation.service.template`，不引入管理脚本。
每台服务器复制为自己的 `auto-eval-operation.service`，手动修改后安装。
实际配置已通过本目录 `.gitignore` 忽略，不提交本机用户名和路径。
服务名固定为 `auto-eval-operation`，默认监听 `0.0.0.0:8502`。
以下命令在使用 systemd 的 Linux 服务器执行，不适用于 Mac。

## 1. 确认环境

激活平时运行项目的 Conda/venv 环境，进入项目根目录：

```bash
whoami
pwd
python -c "import sys; print(sys.executable)"
```

依赖须安装在这个 Python 环境中。首次部署可在项目根目录执行：

```bash
python -m pip install -e ".[web,video]"
```

## 2. 复制模板，修改本机配置

以下命令均在项目根目录执行。仅在首次部署、实际配置尚不存在时复制，
后续直接编辑原有 `.service`，避免覆盖服务器已经改好的配置：

```bash
cp -i deploy/auto-eval-operation.service.template deploy/auto-eval-operation.service
vi deploy/auto-eval-operation.service
```

根据第 1 步的输出，替换三类示例值：

- `User`：运行项目的普通用户。
- `WorkingDirectory`：项目根目录的绝对路径。
- `ExecStart` 中的 Python 绝对路径，以及 `Environment` 中同一环境的 `bin` 路径。

例如运行用户为 `eval`、项目目录为 `/opt/auto_eval_agent`、Python 为
`/opt/miniconda3/envs/eval/bin/python`，实际配置中相应的四行应改为：

```ini
User=eval
WorkingDirectory=/opt/auto_eval_agent
Environment="PATH=/opt/miniconda3/envs/eval/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/miniconda3/envs/eval/bin/python -u -m uvicorn auto_eval.web.server:app --host 0.0.0.0 --port 8502 --workers 1
```

该示例也需按实际环境替换，不会读取 shell 变量或自动推断路径。若路径有空格，
将完整 `WorkingDirectory` 值及 `ExecStart` 的 Python 路径分别用双引号包裹。
项目会自行读取根目录 `.env`，不要将 API Key 写进 service 文件。
运行用户需能读取项目、Python、数据及媒体文件，并能写入 `runs/`。

## 3. 安装并启用自启动

先确认没有在途评估，再停止原 nohup 服务，避免端口冲突。
不要停用名称相似但属于其他项目的服务。

在项目根目录执行：

```bash
sudo install -m 644 deploy/auto-eval-operation.service /etc/systemd/system/auto-eval-operation.service
sudo systemctl daemon-reload
sudo systemctl enable auto-eval-operation
sudo systemctl restart auto-eval-operation
sudo systemctl status auto-eval-operation --no-pager
```

`enable` 设置开机自启；`restart` 立即使用新配置启动，首次还没有运行时也会启动。
检查状态应为 `active (running)`，再访问 `http://服务器地址:8502/`；远程访问还需
服务器防火墙或安全组允许该端口，建议只放行可信网络。

三份文件的关系：仓库模板 → 本机修改后的 `.service` → `/etc/systemd/system/` 部署副本。
实际启动读取最后一份；服务启动后无需保持 SSH 连接。

## 4. 日常管理：每次一条命令

```bash
sudo systemctl restart auto-eval-operation       # 更新代码或 .env 后重启
sudo systemctl start auto-eval-operation         # 启动
sudo systemctl stop auto-eval-operation          # 停止，保留开机自启
sudo systemctl status auto-eval-operation        # 查看状态
sudo journalctl -u auto-eval-operation -n 100 -f  # 最近 100 行并跟踪日志
sudo systemctl disable --now auto-eval-operation # 停止并取消开机自启
```

日志默认进入系统 journal，不再追加 `output.log`；日志保留期限由服务器配置决定。
查看日志时按 `Ctrl+C` 只退出查看，不会停止服务。

## 5. 修改 service 配置之后

修改项目里的 service 文件不会自动更新部署副本，需要重新复制、加载并重启：

```bash
sudo install -m 644 deploy/auto-eval-operation.service /etc/systemd/system/auto-eval-operation.service
sudo systemctl daemon-reload
sudo systemctl restart auto-eval-operation
```

仅修改业务代码或 `.env` 时，无需重新复制配置，直接 `restart` 即可。
修改模板不会自动更新本机配置；后续模板升级时需手动对照更新 `.service`，不要直接覆盖路径。
项目目录和 Python 环境不能随意移动；移动后需同步修改 service 中的绝对路径。

服务自动拉起不代表批跑自动续跑。重启、停止或机器故障会中断在途任务；
已落盘结果可以从历史加载，未完成条目需重新评估。该配置也不检测进程卡死。
