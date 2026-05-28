# 机器人内部售后 AI 助手

这个项目是给售后人员内部使用的机器人项目级问答与 Debug 辅助工具。它不是直接面向学生的聊天机器人，而是把学生问题、终端日志、功能包代码和 FAQ 结合起来，自动分流并生成售后人员可审核的诊断建议。

当前版本是本地 CLI MVP，用来验证拆分后的售后流程：

```text
学生问题 + 日志
  ↓
第一层：AI 审查（只判断相关性，可用便宜模型）
  ↓
第二层：AI 分类（硬件风险 / 是否转人工 / FAQ / 项目 Debug / 越界）
  ↓
第三层：FAQ / 项目片段检索
  ↓
第四层：项目级 Debug 模型（由第三层检索结果决定是否进入）
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

工具会先判断问题是否机器人售后相关；第一层审查不通过时停止下探。硬件风险和是否需要人工介入由第二层判断。第二层分类后先进入第三层检索，再决定走 FAQ 直接回复，还是进入第四层项目级 Debug。

## 当前能力

- 项目文件扫描和只读索引
- 忽略 `build/`、`devel/`、`install/`、`.git/`、`logs/` 等无关目录
- 识别 ROS 相关文件类型
- 第一层 AI 相关性审查，可配置便宜模型
- 第二层 AI 安全判断和分类，可配置更强模型
- 第三层 FAQ 匹配和项目相关文件检索
- 第四层项目 Debug 模型提示与回答（按第三层检索结果进入）
- 生成 Markdown 诊断报告
- 生成可交给 AI 服务、Codex 或其他代码模型的 Debug Prompt
- 支持历史记录 JSONL
- 支持 OpenAI-compatible API 调用

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

## 项目结构

```text
robot-support-ai/
  README.md
  support_ai.py
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
- 第三层 FAQ 命中和项目检索结果
- 建议处理路径
- 需要进入第四层时，可复制给模型的完整诊断提示

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
处理：先做第三层项目检索
命中相关 launch/config/src/README/package.xml/CMakeLists.txt 片段后进入第四层项目 Debug
```

## 接入 AI API

默认从本地文件 `ai_config.local.json` 读取 OpenAI-compatible API 配置。这个文件不要提交到 GitHub。

```json
{
  "review": {
    "api_key": "替换成第一层 API Key",
    "base_url": "https://review-provider.example.com/api/v1",
    "model": "your-cheap-review-model"
  },
  "classification": {
    "api_key": "替换成第二层 API Key",
    "base_url": "https://classification-provider.example.com/api/v1",
    "model": "your-strong-classification-model"
  },
  "debug": {
    "api_key": "替换成第四层 API Key",
    "base_url": "https://debug-provider.example.com/api/v1",
    "model": "your-debug-model"
  }
}
```

每一层可以接不同厂商、不同 Key、不同 Base URL。也可以用环境变量分别覆盖：

```powershell
$env:AFTERSALES_REVIEW_AI_API_KEY="第一层 API Key"
$env:AFTERSALES_REVIEW_AI_BASE_URL="https://review-provider.example.com/api/v1"
$env:AFTERSALES_REVIEW_AI_MODEL="your-cheap-review-model"
$env:AFTERSALES_CLASSIFICATION_AI_API_KEY="第二层 API Key"
$env:AFTERSALES_CLASSIFICATION_AI_BASE_URL="https://classification-provider.example.com/api/v1"
$env:AFTERSALES_CLASSIFICATION_AI_MODEL="your-strong-classification-model"
$env:AFTERSALES_DEBUG_AI_API_KEY="第四层 API Key"
$env:AFTERSALES_DEBUG_AI_BASE_URL="https://debug-provider.example.com/api/v1"
$env:AFTERSALES_DEBUG_AI_MODEL="your-debug-model"
```

`ask` 默认使用 `--triage-mode auto` 调用模型服务完成第一层审查和第二层分类；本地关键词分流已移除，因此必须配置模型服务 Key。显式使用模型服务：

```powershell
python support_ai.py ask `
  --index indexes/mini_robot.json `
  --question-file samples/cases/lidar_permission_question.txt `
  --log-file samples/cases/lidar_permission_log.txt `
  --triage-mode llm `
  --out reports/llm_triage.md
```

需要模型服务继续生成第四层诊断回答时，加上 `--call-llm`：

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
- AI 服务自动生成回复
- 人工审核工作台

更完整的产品需求见 [docs/product-requirements.md](docs/product-requirements.md)。
