# 本地资料包

园记自由版把写作流程与教育资料分开。当前内置六领域核心经验资料、网站普通班 36 项能力目录（341 条表现原句）、托育 24—36 个月目录（13 项能力、87 条表现原句）。出处见[资料来源清单](../app/data/SOURCES.md)。月评功能、月度课程包、慧园通观察指引原文及托幼发展文档原文不在园记自由版中。

导入文件只保存在当前电脑，不会上传到项目作者的服务器。请使用自己编写、获得许可或有其他合法使用依据的资料；项目的代码许可证不授予第三方资料的版权。即使移除全部资料，程序也能启动：没有对应指标就不选指标，没有知识依据就不生成虚构引用。

## ZIP 布局与限制

ZIP 根目录直接放以下数据，不套一层项目目录。可以只导入其中一种；新文件覆盖同路径的本机资料，其余已导入文件保留。不要放密钥、账号、儿童照片、班级名单、程序或脚本。

```text
indicators.json
nursery_indicators.json
medal_map.txt
ref/
  official-guide.json
  provided-observation-guide.json
  nursery-development.json
  core_健康.md
  core_语言.md
  core_社会.md
  core_科学.md
  core_数学.md
  core_艺术.md
  core-experience/
    core_indicators.json
    core/
      健康.txt
      语言.txt
      社会.txt
      科学.txt
      数学.txt
      艺术.txt
```

只接受这些固定路径；不接受月度课程或 Skill 文件。所有文件须为 UTF-8 文字。ZIP 最多 16 MiB，单文件最多 4 MiB，展开总量最多 32 MiB，最多 128 个文件。导入拒绝路径穿越、加密文件、软链接、重复路径及异常压缩比。

程序在临时目录合并并校验，全部通过才切换资料快照。失败不会修改当前有效资料。旧快照留在本机便于恢复。程序不执行资料内的脚本，不自动访问资料中写出的地址。

## 指标目录

下列结构用于导入已经核对的真实目录，不用于自创网站打分编号。网站功能需要教师自己的授权登录，以及该账号有权使用的班级和目录。

| 文件 | 结构 |
| --- | --- |
| `indicators.json` | 顶层数组。每项有正整数 `id`、`name`、`domain`、`subdomain`、`goal`（可为空）和 `levels`。`levels` 只允许键 `1`、`3`、`5`，值为原句数组。二、四档只记录水平，不填具体原句。 |
| `medal_map.txt` | 每行 5 列，以竖线分隔：网站能力项编号、名称、目标编号、封面标识、具体表现与档位的映射。最后一列以逗号分隔，每项用冒号连接两个编号。 |
| `nursery_indicators.json` | 顶层数组；除普通目录字段外，还需 `system_id: 35`、`medal_id`、`target_id`、`gradeVos`、`gradeDescVos`。`levels` 仅允许 `128`，且 `gradeVos[].id`、`gradeDescVos[].gradeId` 均为 `128`。 |

普通班的 `id` 是程序内部稳定编号，不是网站 `medal_id`，两者不得混用。程序按名称与 `medal_map.txt` 精确对应取得网站编号。内置普通班原句直接来自网站目录，不依赖被排除的慧园通指引。更换目录时应同时核对名称与映射；不能依数组排序推测编号。托育目录内的 `gradeDescVos[].medalId` 必须对应本项 `medal_id`。

## 知识依据

| 文件 | 结构 |
| --- | --- |
| `ref/official-guide.json` | 对象含 `chunks` 数组；每项需 `id/source/heading/domain/text/age_band/source_kind/location`，来源类型为 `official_guide`。文件名不代表内容已获官方认可，使用者须核对原件。 |
| `ref/provided-observation-guide.json` | 同上，来源类型为 `provided_reference`；关联指标时还需 `indicator_id` 及 `behavior_level`。这是私人参考资料的可选导入格式，仓库不附带慧园通原文。 |
| `ref/nursery-development.json` | 对象含 `system_id: 35`、`supported_levels: [128]`、`source_files` 和 `chunks`。`source_files` 列出来源文件名与 SHA-256；条目需来源文件及哈希、`behavior_level: 128`、月龄、领域、关联指标、`attainment_assessed: false`。类型为 `nursery_indicator` 或私人导入的 `nursery_development`。 |

文字结构示例（仅演示 schema，不是教育条文）：

```json
{
  "chunks": [
    {
      "id": "my-reference",
      "source": "自行核对的资料名称",
      "heading": "条目标题",
      "domain": "健康领域",
      "text": "填写有权使用的原文。",
      "age_band": "4-5岁",
      "source_kind": "provided_reference",
      "location": "页码或条目位置",
      "source_url": ""
    }
  ]
}
```

随库的 `ref/nursery-development.json` **只有 87 条官网托育指标派生索引**。每条保留来源 JSON 文件哈希和数组坐标，可逐字回查；其中没有托幼发展 DOCX 的 71 条段落。该文件沿用兼容文件名，不表示含有被排除的教材。

六份 `core_*.md` 供浏览核对，默认不用于自动分析。引用原始核心经验表时须同时提供 `core_indicators.json` 和对应 `core/*.txt`。索引保留 `source_sha256` 及每项的 `source_file/source_cells/source_lines`，坐标必须对应原表文字。程序校验哈希、行列、起止位置、标题和正文；不能把改写摘要当成原表导入。

一次行为不等于整项达标，没有记录不等于不会。年龄档与发展阶段应分开理解。知识库引用和网站打分分别核对，不自动互相替代。

## 命令行与本机接口

在项目根目录运行：

```shell
python app/resource_pack.py my-resources.zip
python app/resource_pack.py
```

第一条导入，第二条只输出本机资料文件清单和指标可用状态。可用环境变量 `KIDOBS_DATA_DIR` 指定独立测试数据目录。

本机服务提供：

- `GET /api/resources`：查看文件路径、体积和指标可用状态，不返回资料正文。
- `POST /api/resources/import`：`multipart/form-data`，字段名 `file` 为 ZIP。

接口沿用程序的本机访问保护。不要把本机服务直接开放到公网。导入成功后清除知识库与指标缓存，无需重新安装软件。
