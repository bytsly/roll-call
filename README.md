# 业余无线电台网点名软件

版权及开发：BH2VLF

## 组成

- `roll_call_app.py`：桌面客户端（Tkinter），版本 2.0
- `RELAY/`：中继服务（仅标准库），版本 1.1，含手机点名页与场次报名页
- `tools/pi_star_http_test.py`：Pi-Star / WPSD / J-Star 盒子呼号抓取
- `p2p_sync.py`、`master_library_sync.py`：P2P 同步与主库同步
- `client_updater.py`：客户端自动更新

## 运行

```powershell
pip install -r requirements.txt
python roll_call_app.py
```

中继：

```powershell
python RELAY\relay_server.py --host 0.0.0.0 --port 4570 --db relay_data.db
```

启动后浏览器访问 `http://<服务器IP>:4570/` 为手机点名页，`/signup` 为场次报名页。

## 配置

客户端读取 exe 同目录的 `app_config.ini`。中继令牌放在程序目录 `relay.token`，或通过 `--token` 指定。

## 打包

```powershell
pyinstaller 点名软件_最终版.spec
pyinstaller RELAY\relay_server.spec
```

## 版权

版权及开发 BH2VLF，详见 LICENSE。
