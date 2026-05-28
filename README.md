# 机器人内部售后 AI 助手

这个项目是给售后人员内部使用的机器人项目级问答与 Debug 辅助工具。它不是直接面向学生的聊天机器人，而是把学生问题、终端日志、功能包代码和 FAQ 结合起来，自动分流并生成售后人员可审核的诊断建议。

当前版本是本地 CLI MVP，用来验证拆分后的售后流程：

```text
学生问题 + 日志
  ↓
第一层：豆包审查（相关性 / 硬件风险 / 是否转人工）
  ↓
第二层：豆包分类（FAQ / 项目 Debug / 泛机器人问题 / 越界）
  ↓
第三层：简单问题 / FAQ
  ↓
第四层：项目级 Debug
  ↓
售后人员审核后回复学生
```

默认只读：不执行功能包命令、不修改项目文件、不直接让学生操作 Agent。

## 适用问题

常见输入包括：

```text
小车型号怎么看
雷达没有数据
串口打不开
功能包编译失败
节点启动失败
topic 没有输出
摄像头/底盘/IMU 异常
```

工具会先判断问题是否机器人售后相关，再决定走 FAQ 直接回复，还是进入项目级 Debug 检索。

## 当前能力

- 项目文件扫描和只读索引
- 忽略 `build/`、`devel/`、`install/`、`.git/`、`logs/` 等无关目录
- 识别 ROS 相关文件类型
- 第一层豆包审查
- 第二层豆包分类
- 第三层 FAQ 匹配
- 第四层项目相关文件检索
- 生成 Markdown 诊断报告
- 生成可交给豆包、Codex 或其他代码模型的 Debug Prompt
- 支持历史记录 JSONL
- 支持火山方舟豆包 OpenAI-compatible API 调用

重点索引文件包括：

```text
README.md
package.xml
CMakeLists.txt
launch/*.launch
config/*.yaml
src/
scripts/
msg/
srv/
urdf/
xacro
log
```

## 关键词配置

本地规则兜底使用的关键词放在 `config/keywords.toml`，主程序启动时读取：

- `[chinese_expansions]`：中文词到英文/ROS 关键词的扩展映射
- `[keywords].related`：机器人售后相关性关键词
- `[keywords].simple`：简单 FAQ 倾向关键词
- `[keywords].complex`：项目 Debug 倾向关键词
- `[keywords].hardware_risk`：硬件安全风险关键词

这个文件是普通配置，可以提交到 GitHub；API Key 仍然只放在 `doubao_config.local.json`。

## 项目结构

```text
robot-support-ai/
  README.md
  support_ai.py
  config/
    keywords.toml
  data/
    faqs.json
  indexes/
  reports/
  samples/
    mini_robot/
    cases/
  docs/
    product-requirements.md
```

## 快速试跑

建立示例功能包索引：

```powershell
python support_ai.py index --project samples/mini_robot --out indexes/mini_robot.json --name "Mini Robot"
```

查看项目概况：

```powershell
python support_ai.py inspect --index indexes/mini_robot.json
```

输入学生问题和日志，生成售后诊断报告：

```powershell
python support_ai.py ask `
  --index indexes/mini_robot.json `
  --question-file samples/cases/lidar_permission_question.txt `
  --log-file samples/cases/lidar_permission_log.txt `
  --out reports/lidar_permission_utf8.md `
  --history reports/history.jsonl
```

生成的 Markdown 报告会包含：

- 第一层审查结果
- 第二层分类结果
- FAQ 命中
- 检索到的项目文件
- 建议处理路径
- 可复制给第四层模型的完整诊断提示

## 示例结果

简单问题：

```text
怎么确认这个小车的型号？
```

预期处理：

```text
分类：simple_faq
处理：第三层 FAQ 直接回答
不进入第四层项目 Debug
```

复杂问题：

```text
雷达启动后没有 /scan 数据，终端提示 Permission denied
```

预期处理：

```text
分类：project_debug
处理：进入第四层项目 Debug
检索相关 launch/config/src/README/package.xml/CMakeLists.txt 片段
```

## 接入豆包 API

默认从本地文件 `doubao_config.local.json` 读取火山方舟配置。这个文件已加入 `.gitignore`，不要提交到 GitHub。

```json
{
  "api_key": "替换成你的 API Key",
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "model": "doubao-seed-2-0-lite-260215"
}
```

也可以用环境变量覆盖：

```powershell
$env:ARK_API_KEY="你的 API Key"
$env:AFTERSALES_DOUBAO_MODEL="doubao-seed-2-0-lite-260215"
```

`ask` 默认使用 `--triage-mode auto`：检测到豆包 Key 就调用豆包完成第一层审查和第二层分类；没有 Key 时回退到本地规则。强制使用豆包：

```powershell
python support_ai.py ask `
  --index indexes/mini_robot.json `
  --question-file samples/cases/lidar_permission_question.txt `
  --log-file samples/cases/lidar_permission_log.txt `
  --triage-mode doubao `
  --out reports/doubao_triage.md
```

需要豆包继续生成第四层诊断回答时，加上 `--call-llm`：

```powershell
python support_ai.py ask `
  --index indexes/mini_robot.json `
  --question-file samples/cases/lidar_permission_question.txt `
  --log-file samples/cases/lidar_permission_log.txt `
  --call-llm `
  --out reports/llm_answer.md
```

## 用在真实功能包

先为功能包建立索引：

```powershell
python support_ai.py index --project "D:\robot_ws\src\your_robot_pkg" --out indexes/your_robot_pkg.json --name "某型号小车"
```

之后售后人员只需要替换问题和日志：

```powershell
python support_ai.py ask `
  --index indexes/your_robot_pkg.json `
  --question "学生原始问题" `
  --log-file "D:\student_logs\case001.txt" `
  --out reports/case001.md
```

## 后续升级方向

- FastAPI 后端
- 网页表单
- FAQ 管理界面
- 多功能包版本管理
- 历史问答缓存
- 豆包自动生成回复
- 人工审核工作台

更完整的产品需求见 [docs/product-requirements.md](docs/product-requirements.md)。
