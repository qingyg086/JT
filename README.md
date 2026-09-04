# J&T 坐席实时状态监控

这个项目用于监控 J&T 内部网页“坐席实时状态”：

- 每 60 秒统计全表在线人数、离线人数、通话栏为“空闲”的人数。
- 在线人数达到阈值后，人数变化才提醒，不视为异常。
- 离线人数达到阈值后进入异常，人数变化继续提醒，恢复到阈值以下发送恢复通知。
- 离线异常提醒会带上离线坐席的分机号，方便定位具体设备。
- 空闲人数小于等于阈值后进入异常，人数变化继续提醒，恢复到阈值以上发送恢复通知。
- 支持手动“查询并推送”：点击一次按钮，读取一次当前全表并推送一次飞书查询结果。
- 当前版本先支持控制台和日志提醒；飞书机器人配置项已预留，后续填入 Webhook 即可接入。
- 处理登录过期、页面/API 请求失败，并避免重复提醒。

## 图形界面运行方式

Mac 上运行：

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 web_ui.py
```

Windows 上运行：

```bat
python -m pip install -r requirements.txt
python -m playwright install chromium
run_ui.bat
```

运行后会自动打开本地网页。如果没有自动打开，在浏览器里输入：

```text
http://127.0.0.1:8765
```

打开 UI 后：

1. 填写飞书 Webhook；暂时不接飞书也可以留空。
2. 填写在线、离线、空闲阈值。
3. 点击“保存配置”。
4. 点击“登录极兔系统”，在弹出的浏览器里扫码登录。
5. 登录成功后，程序会自动回到坐席页面，查询一次当前全表并推送一次飞书结果。
6. 如果误点登录，直接关闭弹出的极兔浏览器即可，程序会显示“登录已取消”，不会反复自动弹出。
7. 如需再次手动留痕，点击“查询并推送”，程序会查询一次当前全表并推送一次飞书结果。
8. 如需持续监控，点击“开始监控”。

程序会在 UI 的“当前状态”里显示总人数、在线、离线、通话空闲和最近检查时间，并在“提醒记录”里显示触发过的提醒。日志仍会写入 `runtime/logs/monitor.log`。

## 稳定版登录处理

极兔内部网站是否会自动退出登录，取决于它自己的登录有效期、扫码策略和公司安全设置，程序不能阻止网站让账号过期。

当前 UI 版已经加入稳定处理：

- 如果检测到登录过期，监控会暂停，不会继续用错误页面统计人数。
- UI 的“登录状态”会显示“等待重新扫码”。
- 如果已配置飞书，程序只会发送一次“需要重新登录”的提醒，避免重复刷屏。
- 重新扫码成功后，程序会自动继续监控；也可以在 UI 点击“登录极兔系统”主动触发重新检测。
- 页面请求失败但不是登录过期时，程序会保留原来的自动重试逻辑。

## 命令行运行方式

如果不想使用 UI，也可以继续用命令行。

## Windows 命令行步骤

1. 安装 Python 3.10 或更高版本。
2. 在本目录打开命令行，执行：

```bat
python -m pip install -r requirements.txt
python -m playwright install chromium
copy config.example.json config.json
```

3. 编辑 `config.json`：

- 设置 `thresholds.online`、`thresholds.offline`、`thresholds.idle`。
- 暂时可以不填 `notify.feishu_webhook`，程序会先在控制台和日志里提醒。
- 后续接飞书时，再填写 `notify.feishu_webhook`；如果飞书机器人启用了签名校验，再填写 `notify.feishu_secret`。

4. 先运行勘察：

```bat
run_discovery.bat
```

程序会打开浏览器。如果出现扫码登录，请让内部人员扫码。勘察结果会保存到 `runtime/discovery/`。

5. 当前数据源：

- 已确认页面业务接口是 `POST {GATEWAY.SER}/seatedStateRecord/page`。
- 由于网关前缀还没有完全确认，当前程序先用浏览器自动化翻页读取全表。
- 页面当前每页 10 条、共 36 条时，程序会从第 1 页翻到最后一页后汇总统计。

6. 启动监控：

```bat
run_monitor.bat
```

## 当前提醒逻辑

- 在线人数达到阈值后提醒；如果人数不变，不重复提醒；如果人数变化，继续提醒。
- 离线人数达到阈值后视为异常；人数变化继续提醒；异常提醒包含离线分机号；恢复到阈值以下发送恢复通知。
- 通话栏等于“空闲”的人数小于等于阈值后视为异常；人数变化继续提醒；恢复到阈值以上发送恢复通知。
- 点击“查询并推送”会立即推送一条“坐席状态查询结果”，不受异常阈值限制，用于保证一次人工查询对应一次飞书记录。
- 每次检查失败会发送一次失败提醒，并在下一轮自动重试。

## 日志

日志保存在：

```text
runtime\logs\monitor.log
```

## 数据读取注意

当前版本会操作浏览器翻页读取数据。运行时不要手动切换这个浏览器窗口里的页面，否则可能影响本轮统计。

## 打包成 EXE

在 Windows 上执行：

```bat
build_windows.bat
```

生成文件夹在：

```text
dist\jt-seat-monitor-ui\
```

把整个 `dist\jt-seat-monitor-ui` 文件夹发给客户，不要只发里面的 exe。客户双击：

```text
jt-seat-monitor-ui.exe
```

首次运行时仍可能需要扫码登录。登录状态保存在程序目录下的 `runtime/browser-profile`。

如果你没有 Windows 电脑，Mac 上不建议直接打 Windows EXE。更稳的选择是：

- 把源码文件夹发给客户，让客户在她的 Windows 电脑上运行 `build_windows.bat`。
- 或者把项目上传到私有 GitHub 仓库，用 GitHub Actions 的 Windows 环境打包，再下载生成的 `dist\jt-seat-monitor-ui` 文件夹。

## 用 GitHub Actions 打包 Windows 版本

项目已经包含 GitHub Actions 配置：

```text
.github\workflows\build-windows.yml
```

推荐把 `jt-seat-monitor` 这个文件夹作为一个独立 GitHub 仓库上传。上传后操作：

1. 打开 GitHub 仓库页面。
2. 点击顶部 `Actions`。
3. 选择左侧 `Build Windows UI`。
4. 点击 `Run workflow`。
5. 等待运行完成。
6. 打开这次运行记录。
7. 在页面底部 `Artifacts` 下载：

```text
jt-seat-monitor-ui-windows
```

下载后解压，会得到：

```text
jt-seat-monitor-ui-windows.zip
```

再解压这个 zip，里面会有：

```text
jt-seat-monitor-ui\
```

把整个 `jt-seat-monitor-ui` 文件夹发给客户。客户双击：

```text
jt-seat-monitor-ui.exe
```

注意：不要把你自己电脑上的 `config.json` 发给客户，里面可能包含你测试时填写的飞书 Webhook 或 Secret。GitHub Actions 打包时会自动使用 `config.example.json` 生成干净的 `config.json`。
