# 第三方依赖与参考资料

## 代码与自编流程

项目作者的代码及自编写作流程使用根目录的 MIT 许可证。第三方的名称、标识、参考原文和依赖软件仍属于各自权利人；本项目不声称自己创作了这些内容，也不以代码许可证替第三方资料授予新权利。

## Python 依赖

启动器从 Python 包仓库安装依赖，源码仓库不包含这些依赖的完整副本。下表根据此次验证环境中的包元数据整理；准确条款以对应版本包内的许可证为准。打包分发时还应保留实际包含的间接依赖的许可证。

| 包 | 许可证 | 项目 |
| --- | --- | --- |
| FastAPI | MIT | https://github.com/fastapi/fastapi |
| Uvicorn | BSD-3-Clause | https://github.com/encode/uvicorn |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| Requests | Apache-2.0 | https://github.com/psf/requests |
| Pillow | MIT-CMU | https://github.com/python-pillow/Pillow |
| openpyxl | MIT | https://openpyxl.readthedocs.io/ |
| python-docx | MIT | https://github.com/python-openxml/python-docx |
| pywebview | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| Tencent Cloud IAI Python SDK | Apache-2.0 | https://github.com/TencentCloud/tencentcloud-sdk-python |
| HTTPX（仅测试） | BSD-3-Clause | https://github.com/encode/httpx |

## 教育参考资料

- `app/data/indicators.json`、`nursery_indicators.json` 和 `medal_map.txt`：幼师口袋网站的能力目录及指标原句。目录 ID 是接口字段，不是账号授权；实际访问范围由使用者的网站账号决定。
- `app/data/ref/core-experience/`：维护者提供的六领域核心经验原表提取文本及细项索引。原出版物与编者信息未完整核实，索引保留文件哈希与坐标用于核对，不将提取文本称为项目原创。
- `app/data/ref/core_*.md`：核心经验整理资料，原出处待核验；与原表细项区别展示，不作为官方指标原文。
- 托班依据索引由已列明的网站 24—36 个月指标派生，未附带慧园通托幼发展教材原文。

逐文件来源与用途见 [app/data/SOURCES.md](app/data/SOURCES.md)。这些参考原文不包含在项目代码的 MIT 再授权范围内。维护者同意随社区版提供的资料，不代表原平台或出版方为本软件背书；二次分发相关原文时应核对其适用的授权条件。

## 写作参考

自编中文表达流程借鉴阮一峰的 [中文技术文档的写作规范](https://github.com/ruanyf/document-style-guide)中的清晰表达思路。仓库没有复制该规范全文，也没有将技术文档规范当成幼儿发展评价标准。

## 不包含的内容

月度评价代码、月评写作流程、《完整儿童·共享备课》课程包、慧园通教材原文、真实教师/幼儿数据、照片、商用密钥、发码工具和支付后端未随本仓库公开。使用者后续导入的资料由使用者管理，不能随代码备份一并提交到公开仓库。
