# py-adb / py-fastboot

基于 Termux-API 的纯 Python adb/fastboot 工具，支持免 Root。

通过 Termux 的 `termux-usb` 获取 USB 文件描述符，直接使用 usbfs ioctl 和 ADB/Fastboot 协议通信。不依赖官方 `adb`/`fastboot`、`libusb` 或 `pyusb`。

项目包含两个独立的单文件 Python zipapp：

- `py-adb`：ADB daemon、设备列表、shell、push/pull、reboot。
- `py-fastboot`：Fastboot 设备列表、`getvar`、`getvar all`、`info`、reboot。

## 目录结构

```text
.
├── py-adb                 # 编译后的 ADB 单文件二进制
├── py-fastboot            # 编译后的 Fastboot 单文件二进制
├── python-adb/            # py-adb 源码和测试
│   ├── __main__.py
│   ├── termuxadb/
│   └── tests/
├── python-fastboot/       # py-fastboot 源码和测试
│   ├── __main__.py
│   ├── termuxadb/
│   └── tests/
├── build.sh
├── LICENSE
└── README.md
```

两个源码目录各自包含运行所需的 `termuxadb` 子模块，因此可以独立构建、独立运行。

## 环境要求

- Android 上的 Termux
- Python 3.10 或更新版本
- `termux-api` 包和 Termux:API 应用
- USB OTG 连接的目标设备

检查 `termux-usb`：

```bash
pkg install termux-api
termux-usb -l
```

## 安装

把两个二进制复制到 Termux 的 `$PREFIX/bin`：

```bash
cp py-adb py-fastboot "$PREFIX/bin/"
chmod 700 "$PREFIX/bin/py-adb" "$PREFIX/bin/py-fastboot"
hash -r
```

不要同时运行官方 `adb` server 或 `termux-adb` server，否则 USB 接口会被占用。需要时先停止它们：

```bash
adb kill-server
termux-adb kill-server
```

## ADB 使用

```bash
py-adb start
py-adb devices
py-adb status
py-adb shell getprop ro.product.model
py-adb shell echo hello
py-adb push local.txt /sdcard/local.txt
py-adb pull /sdcard/local.txt pulled.txt
py-adb reboot bootloader
py-adb stop
```

`py-adb start` 会启动后台 daemon 并复用同一条 ADB 连接，成功时输出：

```text
 - 守护进程已启动 [设备状态: device]
```

`py-adb devices` 只输出状态和序列号，例如：

```text
device: abc1234
```

除 `start`、`stop`、`daemon`、`help`、`version` 外，其他命令在 daemon 未运行时会自动启动它，并且不显示启动提示。`push`/`pull` 会显示源与目标、进度条、已传输大小和速度，成功后输出带 `[OKAY]` 的结束行；`py-fastboot flash` 使用相同样式。

`py-adb shell` 不带参数时进入逐行 REPL；命令之间不保留 `cd` 和环境变量。需要完整 shell 时使用：

```bash
py-adb shell "sh -c 'cd /sdcard && ls'"
```

## Fastboot 使用

先把设备重启到 bootloader：

```bash
py-adb reboot bootloader
```

然后：

```bash
py-fastboot devices
py-fastboot getvar product
py-fastboot getvar all
py-fastboot info
py-fastboot info unlocked
py-fastboot flash boot boot.img
py-fastboot reboot
py-fastboot reboot-bootloader
```

`py-fastboot devices` 输出格式与 `py-adb devices` 一致：

```text
fastboot: abc1234
```

`info` 和 `getvar all` 会逐行输出 bootloader 返回的全部 INFO 变量，包括分区表、`serialno`、`product`、`unlocked`、`secure`、`max-download-size` 等。`info` 后可以跟关键词过滤，采用子串匹配，例如 `py-fastboot info lock` 只输出包含 `lock` 的行（如 `unlocked: yes`）。输出会自动把 `key:value` 规范为 `key: value`。

Fastboot 模式切换后 USB 设备路径会变化，Termux 可能再次请求宿主 USB 权限；这是 `termux-usb` 权限，不是 ADB RSA 授权。

## 构建

```bash
./build.sh
```

脚本会分别构建：

```text
py-adb
py-fastboot
```

也可以手动构建：

```bash
rm -rf /tmp/py-adb-build
mkdir -p /tmp/py-adb-build
cp python-adb/__main__.py /tmp/py-adb-build/__main__.py
cp -R python-adb/termuxadb /tmp/py-adb-build/termuxadb
find /tmp/py-adb-build -type d -name __pycache__ -exec rm -rf {} +
python3 -m zipapp /tmp/py-adb-build \
  -p "/data/data/com.termux/files/usr/bin/python3" \
  -o py-adb
chmod 700 py-adb
```

Fastboot 把上面的 `python-adb`、`py-adb` 替换为 `python-fastboot`、`py-fastboot` 即可。

## 测试

```bash
cd python-adb && python3 -m unittest discover -s tests -v
cd ../python-fastboot && python3 -m unittest discover -s tests -v
```

当前测试覆盖 ADB 握手/流/同步、Fastboot 协议、USB 描述符、Termux fd 传递和 usbfs 请求编号。

## 说明

- ADB 密钥默认复用 `~/.android/adbkey` 和 `~/.android/adbkey.pub`；首次选择后会记录在 `~/.config/termux-pyadb/key_path`，避免后续在 Android 密钥和 JSON 密钥之间切换导致重复授权。`py-adb status` 会显示当前 `密钥文件`、SHA256 和 MD5。
- daemon socket 默认在 `~/.config/termux-pyadb/min.sock`。
- 目标设备的 `/data/misc/adb/adb_keys` 中需要包含当前公钥；如果已包含，正常握手不会重复弹出 RSA 授权。
- 如果 pyadb 启动时报接口忙，先停止官方 `adb`/`termux-adb` server，而不是强制 detach USB 驱动。
- `py-fastboot flash 分区 镜像` 使用 Fastboot download/flash 流程。
- `py-fastboot` 的帮助、错误和操作结果均为中文；Fastboot 变量名和值保持原始英文。

## License

MIT，见 [LICENSE](LICENSE)。
