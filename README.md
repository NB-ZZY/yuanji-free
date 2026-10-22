# 幼儿观察助手 · 社区版

帮助幼儿园教师整理照片、撰写随手记和观察记录，再由教师审核、导出或提交到自己的幼师口袋账号。

**无需激活码，不接入项目作者的收费、授权或更新服务器。** AI 使用你自己配置的模型与 API，调用费用由对应服务商收取。照片理解需要所选模型支持图片输入。

第一次使用电脑程序的老师，请先看 [从下载到第一条记录](docs/teacher-guide.md)。想自动认出照片中的幼儿，再看 [腾讯云人脸识别手把手教程](docs/tencent-face-guide.md)。不开人脸识别也能用。

## 快速开始

1. 安装 **Python 3.12**。Windows 安装时勾选 `Add python.exe to PATH`。
2. 下载本仓库的 ZIP 并完整解压，或执行：

   ```sh
   git clone https://github.com/NB-ZZY/kidobs-community.git
   cd kidobs-community
   ```

3. Windows 双击 `start.cmd`；macOS 在终端执行 `python3 launch.py`。首次运行会在项目目录创建 `.venv` 并安装依赖。
4. 在「设置」填写自己的 API 地址、模型名称和 API Key，保存。
5. 点击「打开登录窗口」，登录自己的幼师口袋账号，选择班级并同步名单。
6. 导入照片、选择幼儿，点击生成，审核文字和指标后再导出或发布。

也可以把下面这句话交给 WorkBuddy、Codex 等本地编程助手：

> 请克隆并运行这个仓库，先阅读 AGENTS.md 和 README.md，在项目内创建虚拟环境。启动设置页后由我填写自己的模型 API，不要替我发布记录。

Windows 桌面窗口使用 WebView2；如果提示缺少运行时，请按提示安装。macOS 使用系统 WebKit。Linux 可先使用下面的浏览器模式；桌面窗口需要自行安装对应系统的 GTK/Qt 依赖。

## 可以做什么

- 照片分批整理，多张照片合并成一条记录，人工关联幼儿。
- 随手记、观察记录表，支持重写、批量处理和教师审核。
- 观察分析按内置及使用者导入的依据检索；没有依据时不冒充已经完成循证。
- 普通年级与托班流程分开，托班资料仅使用 24—36 个月档。
- 查看当前账号可访问的班级相册，将所选照片加入本地队列。
- 生成 Word，保存草稿，恢复本地回收站内的记录。

网站对接只操作你登录后有权访问的班级。第三方网站接口发生变化时，对接功能可能需要更新；本项目不代表幼师口袋官方。

## 配置自己的模型

优先使用设置页。支持智谱、DeepSeek 和 OpenAI 兼容接口。API 地址应是包含 API 版本的基础地址，程序会追加 `/chat/completions`。

也可以将 `.env.example` 复制为 `.env` 后填写。`launch.py` 会读取它。优先级是：已有系统环境变量 > `.env` > 设置页保存的配置。环境变量覆盖的字段会在设置页提示。

密钥只保存在本机配置中。设置接口仅返回“已设置”状态，留空保存不会覆盖原密钥。不要把 `.env` 或本机数据目录提交到仓库。

## 资料与写作流程

公开版包含网站指标目录、托育体系 24—36 个月指标及核心经验参考资料。来源和各自性质见 [资料来源](app/data/SOURCES.md)。`app/data/writing/`、`app/data/observation/` 保存自编写作流程，可直接阅读和修改。

**观察表允许围绕已知事件补写情节，生成的是待教师核实的草稿。** 补写状态保存在内部字段，正文不会自动插入补写提示；随手记仍按其事实规则生成。如只需实况，在老师说明中写“只按实况记录，不补写”。请在发布前核对过程、对话和结论。

公开版不含月度评价功能、《完整儿童·共享备课》课程包、慧园通教材原文、实际班级名单和照片，也不包含发码、支付及服务器部署材料。可导入自己的观察参考资料，格式见 [资料包说明](docs/resource-packs.md)。

腾讯云人脸识别适配代码保留，默认关闭。启用时使用者需要开通自己的服务、填写自己的 SecretId 和 SecretKey；本项目不提供共用密钥。

## 数据位置

社区版使用独立目录，不读取原商业版数据库：

| 系统 | 默认目录 |
| --- | --- |
| Windows | `%APPDATA%\kidobs-community` |
| macOS | `~/Library/Application Support/kidobs-community` |
| Linux | `~/.config/kidobs-community` |

可以用 `KIDOBS_DATA_DIR` 指定其他目录。里面保存本机配置、登录会话、草稿、照片和私有资料。备份时先正常退出程序，再复制整个数据目录。

## 开发与检查

```sh
python launch.py --setup-only
python launch.py --selftest
python launch.py --serve --port 8765
```

最后一个命令只监听本机 `127.0.0.1`，浏览器打开 `http://127.0.0.1:8765`。扫码登录窗口需用桌面模式；浏览器模式主要用于界面开发。

运行回归检查（不调用真实 AI 或云识别服务）：

```sh
# Windows
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe tools/check.py

# macOS / Linux
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python tools/check.py
```

如安装了 Node.js，检查程序还会验证前端脚本。测试使用临时目录与合成资料，不读取老师的数据目录。

已有用户更新源码时，正常退出程序后执行 `git pull`，重新运行启动脚本。社区版不会下载或强制安装商业版安装包。

## 许可证

项目代码与自编写作流程按 [MIT](LICENSE) 许可证开放，允许修改和商用，分发时保留版权与许可证声明。第三方参考原文、依赖及使用者导入的资料不因本项目的 MIT 声明而改变权利归属，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
