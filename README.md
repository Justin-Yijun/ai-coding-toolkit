# ai_toolkit

本地受限环境（内网隔离 / 仅 CPU / 小上下文窗口）下的 **AI 辅助编程工具集**。
核心思路：**把"理解整个大文件"换成"喂入最小确定片段 + 确定性校验闭环"**，
让一台跑 9B 量化模型的 CPU 服务器，在夜间无人值守时持续产出**已验证可用**的代码。

## 设计三原则

1. **无状态 Skill**：每个工具只处理当前喂入的片段（单个函数 / 一段描述 / 项目骨架），
   不依赖全局变量或对话历史，天然规避上下文累积溢出。
2. **黄金法则闭环**：`生成 → 确定性校验（AST / Pytest / Mypy）→ 报错塞回 Prompt 迭代`，
   最大迭代 **3 次**硬编码。靠编译器/测试给客观信号，不靠模型自评。
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
│   ├── validator.py     # AST / 正则 / pytest / mypy 校验
│   └── text_utils.py    # 代码块提取、token 估算与裁剪
└── tools/               # 4 个无状态功能 Skill
    ├── ut_gen.py        # 单测生成（多框架：pytest/googletest/cpputest，探测并参照现有框架）
    ├── regex_gen.py     # 正则生成（正反例断言）
    ├── type_annotate.py # 类型注解（ast + mypy --strict）
    ├── product_gen.py   # 产品代码生成（项目骨架裁到 1500 tokens；Python 与 C/C++）
    └── summarize_gen.py # 目录总结（分而治之：骨架→逐文件分析→记录拼接；支持 focus / 深度模式）
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

### 4️⃣ 产品代码生成（product）—— 参照项目风格写新代码
```powershell
python main.py product --root . --req "新增一个带超时控制的重试装饰器，风格与现有 core 模块一致"
python main.py product --root ./myproject --req "新增一个 LRU 缓存类"
```
> 原理：先把项目骨架裁到 1500 tokens 作上下文，生成后走 AST（Python）/ 括号配平（C/C++）校验，最多迭代 3 轮。

### 5️⃣ 目录总结（summarize）—— 分而治之读大项目
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

### 6️⃣ 批处理（batch）—— 夜间无人值守
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

## 模型升级

仅需修改 `config.yaml`：
- 旧 9B 弱模型：`temperature=0.2`，`num_predict=1024`；
- 升级强模型：`temperature` 降到 `0.05~0.1`，`num_predict` 放开到 `4096+`。
详见 `core/llm_client.py` 顶部注释。
