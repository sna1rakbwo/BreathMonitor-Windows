# Windows 发布说明

## 给使用者

推荐发送 `BreathMonitor-Setup-1.1.0.exe`。双击安装后，从开始菜单打开“呼吸监测”。使用者不需要安装 Python。

运行条件：

- 64 位 Windows 10 1809 或更高版本，或 Windows 11。
- 电脑具有支持 BLE 的蓝牙适配器，并已在 Windows 设置中打开蓝牙。
- `BreathSensor-ESP32` 已通电，且没有被手机或另一台电脑占用。

Windows SmartScreen 可能对未签名的新应用显示“Windows 已保护你的电脑”。内部测试时可点击“更多信息 → 仍要运行”。正式公开发布前应购买代码签名证书并给安装包和主程序签名，不建议指导陌生用户长期绕过安全警告。

安装目录包含 `THIRD_PARTY_NOTICES.md`。公开或商业发布前还应补齐依赖许可证原文并复核 Qt LGPL 合规要求。

也可以发送 `BreathMonitor-Portable-1.1.0.zip`。解压整个文件夹后运行 `BreathMonitor.exe`，不能只单独复制 EXE。

## 在 Windows 上一键构建

1. 安装 64 位 Python 3.12，安装时勾选 Python Launcher。
2. 如需安装包，安装 Inno Setup 6；只需便携版可以跳过。
3. 双击项目根目录的 `build_windows.bat`。
4. 构建结束后从 `release` 目录取出安装包或便携版 ZIP。

脚本会建立隔离的 `.venv-build` 环境、安装冻结依赖、生成应用、运行内置算法自检，再创建发布文件。

## 使用 GitHub Actions 构建

将本项目放入独立 GitHub 仓库后，在 Actions 页面手动运行 `Build Windows app`，或推送 `v1.1.0` 形式的标签。构建完成后下载 `BreathMonitor-Windows-x64-v1.1.0` artifact。

## 发布前必须完成的实机检查

- Windows 10 和 Windows 11 各至少一台机器。
- 蓝牙关闭、没有 BLE 适配器、设备未通电时，错误提示可理解且不会卡死。
- 实际扫描到 `BreathSensor-ESP32`，订阅 Notify 后曲线持续更新。
- 连接、断开、再次连接至少各运行 5 次。
- 连续运行至少 30 分钟，确认没有停止刷新或内存持续增长。
- 安装、开始菜单启动、卸载和便携版启动均正常。

当前 macOS 开发机只能验证源码、算法和打包配置，不能替代上述 Windows BLE 实机验收。
