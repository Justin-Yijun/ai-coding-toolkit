# ai_toolkit

本地受限环境（内网隔离 / 仅 CPU / 小上下文窗口）下的 **AI 辅助编程工具集**。
核心思路：**把"理解整个大文件"换成"喂入最小确定片段 + 确定性校验闭环"**，
让一台跑 9B 量化模型的 CPU 服务器，在夜间无人值守时持续产出**已验证可用**的代码。

## 设计三原则

1. **无状态 Skill**：每个工具只处理当前喂入的片段（单个函数 / 一段描述 / 项目骨架），
   不依赖全局变量或对话历史，天然规避上下文累积溢出。
2. **黄金法则闭环**：`生成 → 确定性校验（AST / Pytest / Mypy / 防臆造 / grounded）
   → 报错塞回 Prompt 迭代`，最大迭代 **3 次**硬编码。靠编译器/测试/事实核对给客观信号，
   不靠模型自评。**没有真实构建系统时**（离线/无 toolchain），退化为确定性事实核对
   （项目里是否真有这个头文件/模块、日志里的引用是否有据可查）—— 拦不住所有错误，
   但能零成本拦住弱模型最常见的「编不存在的东西」这一大类幻觉。
3. **配置驱动**：模型、温度、输出长度、超时、重试全在 `config.yaml`，
   换模型只改配置不改代码。

## 目录结构

```
ai_toolkit/
├── main.py              # CLI 统一入口（子命令调度）
├── batch_scheduler.py   # 批处理引擎：优先级/断点续跑/重试/限速/报告
├── config.yaml          # 全局配置（含模型升级参数建议）
├── jobs.json            # 任务队列示例
├── demo.py              # 试跑示例源文件
├── requirements.txt
├── core/                # 基础设施层
│   ├── llm_client.py    # Ollama /api/generate 封装
│   ├── validator.py     # AST / 正则 / pytest / mypy / 防臆造 / grounded 校验
│   ├── project_facts.py # 项目级确定性事实抽取（已有头文件/模块/符号/命名风格；目录指纹缓存）
│   ├── log_facts.py     # 日志确定性事实抽取（关键行/重复报错折叠/结构化调用栈/十六进制/寄存器反查/源码定位）
│   ├── log_memory.py    # 日志案例记忆（长期·案例层：Jaccard 相似度检索历史案例）
│   ├── learned_registers.py # 架构规则记忆（长期·语义层：人工确认的寄存器规则，与外部 kb.json 解耦）
│   ├── ut_framework.py  # UT 框架探测与风格参照抽取（目录指纹缓存）
│   ├── lang_utils.py    # 多语言片段抽取（函数体/骨架/include/import）
│   └── text_utils.py    # 代码块提取、token 估算与裁剪
├── tools/               # 无状态功能 Skill
│   ├── ut_gen.py        # 单测生成（多框架：pytest/googletest/cpputest，探测并参照现有框架）
│   ├── regex_gen.py     # 正则生成（正反例断言）
│   ├── type_annotate.py # 类型注解（ast + mypy --strict）
│   ├── product_gen.py   # 产品代码生成（项目骨架 + 项目事实防臆造；Python 与 C/C++）
│   ├── log_analyze.py   # 日志问题分析（确定性抽取事实 + grounded 防幻觉校验）
│   └── summarize_gen.py # 目录总结（分而治之：骨架→逐文件分析→记录拼接；支持 focus / 深度模式）
└── tests/               # 确定性单测（project_facts/log_facts/log_memory/validator/缓存/黄金循环防幻觉）
```

## 一键环境搭建

> ⚠️ `requirements.txt` 含中文注释，简体中文系统上直接 `pip install -r` 会因 **gbk 解码** 报错。
> 下面的脚本已强制 `PYTHONUTF8=1` 绕过该坑，并自动建虚拟环境 `.venv`、装齐依赖。

**Windows（PowerShell）**
```powershell
./setup.ps1                                  # 外网
./setup.ps1 -Proxy http://10.144.1.10:8080   # 内网走代理
```

**Linux / macOS**
```bash
chmod +x setup.sh
./setup.sh                                   # 外网
./setup.sh http://10.144.1.10:8080           # 内网走代理
```

<details><summary>不用脚本？手动装（注意 gbk 坑）</summary>

```powershell
$env:PYTHONUTF8 = "1"      # 关键：否则中文注释触发 gbk 解码错误
pip install -r requirements.txt
# 内网加 --proxy http://10.144.1.10:8080
```
</details>

## 启动 Ollama（指定本机模型目录）

模型存放在 `C:\N-5CG4480WB2-Data\yijuzhao\Desktop\AI agent\modles`，
启动服务前先把该目录设给 `OLLAMA_MODELS`：

```powershell
$env:OLLAMA_MODELS = "C:\N-5CG4480WB2-Data\yijuzhao\Desktop\AI agent\modles"
ollama serve          # 另开一个终端保持运行
ollama list           # 确认能看到 qwen2.5-coder:7b
```

> 内网隔离环境无需 `ollama pull`，模型已离线放置在上述目录。

## 🚀 Demo 速查（复制即用）

> 以下命令均在项目根目录执行。若用了 `.venv`，把 `python` 换成 `.venv\Scripts\python.exe`（Win）
> 或 `./.venv/bin/python`（Linux）。所有任务**全离线**，只需本地 Ollama 跑着。

### 1️⃣ 生成单测（ut）
```powershell
# 为 demo.py 里的 add 函数生成 pytest 单测（生成后会在隔离环境实跑 pytest 校验）
python main.py ut --file demo.py --func add

# C++ 函数：自动探测项目现有框架（googletest/cpputest）并沿用其风格
python main.py ut --file src/calc.cpp --func add --root .
# 也可显式指定框架
python main.py ut --file src/calc.cpp --func add --framework googletest
```
> 产出：一段可直接入库的测试代码；打印 `[第N轮] 校验通过 ✅`。

### 2️⃣ 生成正则（regex）—— 靠正反例断言驱动
```powershell
python main.py regex --desc "匹配邮箱" `
  --pos alice@example.com bob.k@test.org `
  --neg not_an_email "@nope.com" "a@@b.com"
```
> 产出：一条满足全部正例、排除全部反例的正则；校验在本地用 `re` 断言。

### 3️⃣ 类型注解（type）—— ast + mypy --strict 双闸
```powershell
python main.py type --file demo.py
```
> 产出：给 `add(a, b)` 补上 `def add(a: int, b: int) -> int` 类似注解，并过 `mypy --strict`。

### 4️⃣ 产品代码生成（product）—— 参照项目风格写新代码，防止编不存在的东西
```powershell
python main.py product --root . --req "新增一个带超时控制的重试装饰器，风格与现有 core 模块一致"
python main.py product --root ./myproject --req "新增一个 LRU 缓存类"
```
> 原理：先把项目骨架裁到 1500 tokens 作上下文；同时用 `core/project_facts.py`
> 扫描项目抽取「已有头文件/可用模块/已存在符号/命名风格」等确定性事实，注入 Prompt
> 压制臆造，也直接喂给校验器核对。校验链：AST 语法（Python）/ 括号配平（C/C++）
> → import/`#include` 是否真实存在（`check_python_imports_resolve` /
> `check_cpp_includes_exist`）→ 新符号是否与项目重名（`check_no_symbol_redefinition`），
> 任一环节失败都会把具体错误塞回 Prompt 重试，最多 3 轮。
> 这套防臆造校验**不需要真实构建系统**（编译器/工具链缺失时也能跑），
> 拦住的是弱模型最常见的两类错：编一个不存在的头文件/模块名、悄悄和项目里已有的定义重名。>
> `core/project_facts.py` 扫描结果按「文件数 + 最大 mtime」目录指纹缓存在
> `<root>/.ai_toolkit/project_facts_cache.json`，项目未变化时直接复用，跳过全量重扫。
### 5️⃣ 日志问题分析（log）—— 小模型也不敢瞎编日志细节
```powershell
# 最简单：直接分析一段日志文本
python main.py log --file crash.log

# 接上源码根目录：能把日志里的 file:line 定位到真实代码，摘取真实上下文
python main.py log --file crash.log --src ../l1sw

# 再接上 chip-manual-kit 的知识库：日志里的十六进制值能反查出真实寄存器名
python main.py log --file crash.log --src ../l1sw --kb ../chip-manual-kit/data/knowledge.json

# 追问具体问题
python main.py log --file crash.log --question "这个故障和 DMA 有没有关系？"
```
> 原理（`core/log_facts.py` + `tools/log_analyze.py`）：模型**不会看到原始日志全文**，
> 只看确定性抽取出的「事实」——围绕 `ERROR/FATAL/assert/panic` 等关键词的上下文摘录、
> 日志里出现的十六进制 token、（可选）按地址/名字反查出手册里真实存在的寄存器、
> （可选）解析出的 `file:line` 定位到本地源码后摘取的真实代码片段。
> 模型只能在这些事实范围内推理，回答会用 `check_grounded_references` 校验：
> 任何提到的十六进制值或 `文件:行号` 只要不在事实集合里，就判定为臆造并打回重试。
> 超大日志文件（>20MB）只扫描尾部，避免爆内存；源码路径按后缀匹配本地文件，
> 遇到多个同名文件时宁可不展开上下文，也不给错的。
>
> **真实数据验证过（24MB / 11.8 万行 l1sw 现场日志，事先不告知模块）**：曾经暴露过两个坑，
> 已修好并补了回归测试——① 嵌入式/电信日志常把严重级别缩写成 `43/ERR`、`F6/ERR` 而不拼全
> `ERROR`，朴素的整词匹配会完全漏掉真实报错；② `Error=(0 0 0 0 0 0 0 0)` 这种全零元组是
> 健康遥测，若只按"包含 ERROR 关键词"匹配会被成千上万条这种行淹没摘录预算，反而把真正的
> 报错挤出去。现在按"高危词优先、全零标量/元组自动排除、预算不足时保留【更靠后】的窗口"
> 三条规则处理，修复后小模型（7B/9B）一次性正确定位到日志中段一处 SFP 激光器关闭失败
> （`EStatus_NotPresent`）并给出合理排查建议，全程约 2 分钟、无需人工干预。

#### 记忆层：用「时间 + 记忆」换小模型的等效能力
本工具的设计初衷之一：小模型 + 时间 + 记忆 ≈ 大模型的效果。日志分析在确定性事实
之外，进一步引入两层**长期记忆**（与本次调用的 `LogFacts` 这种**短期记忆**明确区分开）：

- **案例记忆**（`core/log_memory.py`，具体实例，可能过期）：每次 grounded 校验通过后，
  自动把「事实签名（十六进制 token / file:line / 严重程度类型）→ 结论」存成一条
  **未确认**案例，落盘在 `<source_root>/.ai_toolkit/log_cases.jsonl`；用
  `log-confirm` 人工确认某条结论正确后，之后再遇到高度相似（Jaccard 相似度）的
  日志会**直接复用结论、跳过模型调用**。未确认案例默认不注入 Prompt（见下方
  `inject_weak_cases`），避免小模型把「仅供参考的旧案例」和「本次真实证据」搞混。
- **架构规则记忆**（`core/learned_registers.py`，通用规则，几乎不过期）：通过
  `log-confirm --register-note "0xADDR=名称 描述"` 手工登记的寄存器地址含义，
  落盘在 `<source_root>/.ai_toolkit/learned_registers.json`，与外部
  chip-manual-kit 的 `knowledge.json` **完全解耦**、结果叠加使用。

两层记忆都**只在提供 `--src` 时启用**，无 `--src` 时行为与原无状态版本完全一致。

```powershell
# 分析日志：输出末尾会打印 case_id（未确认）或 reused_case_id（已确认命中）
python main.py log --file crash.log --src ../l1sw

# 确认某次结论正确，之后同类日志可直接复用、跳过模型调用
python main.py log-confirm --src ../l1sw --case-id abc123456789

# 顺便登记一条寄存器规则（架构规则记忆，人工确认过才会写入）
python main.py log-confirm --src ../l1sw --case-id abc123456789 --register-note "0x1010=CTRL_STATUS 控制状态寄存器"

# 禁用本次调用的记忆检索/自动入库（不影响已落盘的历史记忆）
python main.py log --file crash.log --src ../l1sw --no-memory
```

`config.yaml` 的 `log_analyze` 段可调：`enable_memory`、`weak_similarity_threshold`
（默认 0.4）、`strong_similarity_threshold`（默认 0.75，且必须 `confirmed=True` 才会
短路复用）、`inject_weak_cases`（默认 `false`，小模型安全默认）、`min_repeat_to_fold`
（默认 3：同类高危报错重复达到这个次数就折叠为一条【重复报错折叠】，避免刷屏式重复
报错占满摘录预算）。

此外 `core/log_facts.py` 现在还会解析出**结构化调用栈**（GDB/glibc backtrace、
Python traceback 均支持，`#0` 为最内层/崩溃点），连同折叠后的重复报错一并注入
Prompt；`core/validator.py` 新增 `check_answer_cites_primary_evidence`，在
grounded 校验（查十六进制值/file:line 是否真实存在）之外，进一步核对回答里
「第 N 行」式的引用是否真的出自这些证据——防止模型引用一个真实存在但无关的
行号来凑数（半臆造）。

### 6️⃣ 目录总结（summarize）—— 分而治之读大项目
面对「上下文窗口小、项目很大」：每个文件先抽骨架→逐个喂模型出摘要（**边做边落盘，天然断点续跑**）→超预算就分层压缩归并。

```powershell
# 通用总结（职责/接口/架构）
python main.py summarize --root ./myproject

# 带「关注点」的主题总结：只围绕你在乎的东西
python main.py summarize --root ./myproject --focus "这些模块用了哪些设计模式"

# 深度模式：对含 CUDA 标记的函数抽【完整实现体】分析，看到 __shared__/<<<>>> 等细节
python main.py summarize --root ./cuda_proj --deep `
  --focus "逐项指出使用的 CUDA 技巧：共享内存、warp 级原语、原子操作、内存合并、同步、kernel 启动配置、流/异步等，并说明每处用途"
```
> 产出：`<out>/<项目名>/summary.md`（整体架构综述 + 逐文件摘要）；
> 中间结果落在 `manifest*.jsonl`，**中途中断重跑会自动跳过已完成的文件**。
> `focus` / `deep` 也可写在 `config.yaml` 的 `summarize` 段里作为默认。

### 7️⃣ 批处理（batch）—— 夜间无人值守
```powershell
python main.py batch --jobs jobs.json
# 等价于： python batch_scheduler.py
```
> 按 `priority` 执行；成功写 `checkpoint.json`（断点续跑）；失败放队尾重试（≤3 次）；
> 任务间限速降温；结束生成 `report.md`。

### 兼容 `--task` 写法
```powershell
python main.py --task ut --file demo.py --func add   # 等价于子命令形式
```

## 单测的多框架支持（ut）

`ut` 会**自动扫描项目探测现有 UT 框架并照其风格生成**，支持：

| 框架 | 语言 | 确定性校验 |
| --- | --- | --- |
| `pytest` | Python | ast 语法 + 隔离环境实跑 pytest |
| `googletest` | C/C++ | 括号配对 + `TEST/TEST_F` 与 `EXPECT_*/ASSERT_*` 宏结构校验 |
| `cpputest` | C/C++ | 括号配对 + `TEST_GROUP/TEST` 与 `CHECK*/LONGS_EQUAL` 宏结构校验 |

```powershell
# C++ 函数生成单测（自动沿用项目现有框架，并截取一段现有测试作风格参照）
python main.py ut --file src/calc.cpp --func add --root .
# 也可显式指定框架
python main.py ut --file src/calc.cpp --func add --framework googletest
```

- 探测逻辑：扫描项目中的测试文件，按「头文件 include + 测试宏」打分选出主用框架；
- 参照注入：截取一段现有测试（默认 800 tokens）作 few-shot，让新单测沿用项目写法；
- C/C++ 离线无统一构建环境时，以「结构性校验」作为确定性闸门（可扩展为真实编译）。
- 扫描打分结果（最耗时的部分）按目录指纹缓存在 `<root>/.ai_toolkit/ut_framework_cache.json`，
  项目未变化时跳过重新读取/打分所有源文件。

> 默认框架可在 `config.yaml` 的 `ut.framework` 写死，留空则自动探测。

## 批处理（夜间无人值守）

```powershell
python main.py batch --jobs jobs.json
# 或直接： python batch_scheduler.py
```

- 按 `priority` 升序执行；
- 成功任务写入 `checkpoint.json`，中断后重跑自动跳过；
- 单任务失败放回队尾，最多重试 3 次；
- 任务间 `delay_between_jobs` 秒限速，给 CPU 降温；
- 结束生成 `report.md`（成功/失败统计 + 失败清单）。

## 跑测试

`tests/` 下是纯确定性单测（不连 Ollama），覆盖事实抽取（`project_facts`/`log_facts`）、
案例记忆与架构规则记忆（`log_memory`/`learned_registers`）、目录指纹缓存
（`project_facts`/`ut_framework`）、新增的防臆造/grounded/一致性校验，
以及用 `FakeClient` 隔离真实模型的黄金循环集成测试（含记忆层短路复用/自动入库行为）：

```powershell
python -m pytest tests/ -v
```

## 模型升级

仅需修改 `config.yaml`：
- 旧 9B 弱模型：`temperature=0.2`，`num_predict=1024`；
- 升级强模型：`temperature` 降到 `0.05~0.1`，`num_predict` 放开到 `4096+`。
详见 `core/llm_client.py` 顶部注释。
