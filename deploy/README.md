# Linux 部署：auto-eval-operation

仅使用带注释的 service 模板，不引入管理脚本。以下操作在使用 systemd 的 Linux
服务器上、项目根目录内执行。服务名为 `auto-eval-operation`。

## 1. 确认环境

先激活平时能正常运行项目的 Conda 环境，再查询以下字段，不要加 sudo：

```bash
# 运行用户 → User
whoami

# 项目根目录 → WorkingDirectory
pwd -P

# Python 绝对路径 → ExecStart 开头
python -c "import sys; print(sys.executable)"

# 环境 bin 目录 → Environment 中的 PATH
python -c "import os, sys; print(os.path.dirname(sys.executable))"
```

已有环境能运行项目时不需要重新安装依赖；新环境可先执行
`python -m pip install -e ".[web,video]"`。

## 2. 复制模板，修改本机配置

首次部署复制一份实际配置，之后直接编辑它，不覆盖已改好的配置：

```bash
cp -i deploy/auto-eval-operation.service.template deploy/auto-eval-operation.service
vi deploy/auto-eval-operation.service
```

按第 1 步的输出填写 `User`、`WorkingDirectory`、`Environment` 和 `ExecStart`，
其余配置先保持不变。`User` 是 Linux 用户名，不是 Conda 环境名。

- PATH 只填环境的 `bin` 目录；缺少外部命令会报错，不回退到系统目录。
- `ExecStart` 使用 Python 绝对路径，保留 `-u -m src.auto_eval.web.server`；
  `-u` 让日志及时输出，不需要写 `nohup`、`&` 或 `conda activate`。
- `WorkingDirectory` 必须是项目根目录。地址和端口由 `server.py` 末尾决定，
  当前为 `0.0.0.0:8502`，不是在 service 中传参数。
- 路径有空格时，用双引号包裹 `WorkingDirectory` 的值和 `ExecStart` 的 Python 路径。

项目自行读取 `.env`，密钥不要写进 service。运行用户需能读取数据并写入 `runs/`。
实际 `.service` 已被 Git 忽略，仓库只保留模板和说明。

## 3. 安装并启用自启动

确认没有在途评估，并停止旧 nohup 服务以免端口冲突，再执行：

```bash
sudo install -m 644 deploy/auto-eval-operation.service /etc/systemd/system/auto-eval-operation.service
sudo systemctl daemon-reload
sudo systemctl enable auto-eval-operation
sudo systemctl restart auto-eval-operation
sudo systemctl status auto-eval-operation --no-pager
```

`daemon-reload` 重新读取配置，不重启服务；`enable` 设置开机自启；
`restart` 立即启动或重启。实际使用的是 `/etc/systemd/system/` 中的副本。
状态应为 `active (running)`，远程访问还需防火墙或安全组允许对应端口。

## 4. 日常管理：每次一条命令

```bash
sudo systemctl restart auto-eval-operation       # 更新代码或 .env 后重启
sudo systemctl start auto-eval-operation         # 启动
sudo systemctl stop auto-eval-operation          # 停止，保留开机自启
sudo systemctl status auto-eval-operation        # 查看状态
sudo journalctl -u auto-eval-operation -n 100 -f  # 最近 100 行并跟踪日志
sudo systemctl disable --now auto-eval-operation # 停止并取消开机自启
```

日志默认进入 journal，不再写 `output.log`；`Ctrl+C` 仅退出日志查看。

## 5. 修改 service 配置之后

仅修改代码或 `.env`：直接 `restart`。修改了 service：重新安装、加载并重启：

```bash
sudo install -m 644 deploy/auto-eval-operation.service /etc/systemd/system/auto-eval-operation.service
sudo systemctl daemon-reload
sudo systemctl restart auto-eval-operation
```

服务自动拉起不代表批跑自动续跑；重启会中断在途任务，未完成条目需从历史重新评估。
