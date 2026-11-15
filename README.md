# 原件保真 CSV 单元格修订器（Python 3.11）

读取 `fixtures/source.csv` 与 `patch.json`，输出 `corrected.csv` 与 `map.json`。
原件只以 `rb` 打开、绝不写入；所有改动一次性预检，任一错误整批拒绝，不发布部分结果。

## 入口

```bash
# 修订
python3 -m csv_fidelity \
  --source fixtures/source.csv --patch fixtures/patch.json \
  --corrected out/corrected.csv --map out/map.json

# 独立验证（读取原件/产物/补丁/映射）
python3 -m csv_fidelity.verify_cli \
  --source fixtures/source.csv --corrected out/corrected.csv \
  --patch fixtures/patch.json --map out/map.json

# 测试（不依赖 pytest）
python3 -m unittest discover -s tests -v
```

CLI 在写出前会先跑一次独立验证；输出先写入同目录临时文件，成功后才原子 `rename`，
失败时（`wrong-old-value`、`duplicate-cell`、malformed 等）退出码 2 且不产生任何文件。
若输出路径与源文件同一路径/硬链接，直接拒绝（`OUTPUT_IS_SOURCE`）。

## 设计要点

- **字节级扫描**（`csv_fidelity/scanner.py`）：不用 `csv`，直接按 UTF-8 字节定位。
  每个字段记录 `start/end`（含原包围引号，不含分隔逗号、记录终止符、BOM）与解码值。
  中文按字节偏移，可正确处理 `"中文"` 这类 8 字节词素。
- **方言**：UTF-8（可带 BOM）、逗号分隔、双引号包围、`""` 转义；引号内允许逗号/CR/LF/CRLF；
  记录终止符 CRLF/LF/裸 CR 可混用；末记录可无终止符；空字段合法。
  空行是零字段记录，按列数不等拒绝，不做空行跳过。不归一化任何换行。
- **严格拒绝**：裸字段内双引号、未闭合引号、闭引号后多余字节、非法 UTF-8、
  列数不等于表头、空文档。
- **寻址**：`row` 为表头后第一条数据记录起的 0 基逻辑行，表头不可修改；
  `column` 为 0 基物理列序；`expected` 为解码后精确字符串。
- **字段内替换**（`csv_fidelity/patcher.py`）：
  - 原带引号 → 仍带引号，内部 `"` 加倍；
  - 原无引号 → 仅当新值含 `,`、CR、LF 或 `"` 时才升级为引号字段，否则保持无引号；
  - 新旧值相同（含空字段）→ 直接复用原字段字节，绝不规范化；
  - 单列记录中的无引号空值会被渲染为 `""`：零长度词素等同于空行（零字段记录），
    必须加引号才能保留“一个空字段”；
  - 未改字段、表头、BOM、混合终止符、末行有无换行全部逐字节保留。
- **整批预检**：先校验 SHA256（补丁绑定源文件）、JSON 形状/类型、重复单元格，
  再由扫描结果校验行/列存在性与旧值；全部通过才生成输出。

## patch / map 结构

```json
{
  "source_sha256": "<64 hex>",
  "changes": [
    {"row": 0, "column": 1, "expected": "Alice", "value": "A, B"}
  ]
}
```

`map.json`：

```json
{
  "source_sha256": "...",
  "target_sha256": "...",
  "source_size": 92,
  "target_size": 94,
  "segments": [
    {"kind": "copy", "source_start": 0, "source_end": 28,
     "target_start": 0, "target_end": 28},
    {"kind": "replace", "source_start": 28, "source_end": 35,
     "target_start": 28, "target_end": 34,
     "cell": {"row": 0, "column": 1}, "old": "Alice", "new": "A, B"}
  ]
}
```

片段按源顺序排列，半开区间 `[start, end)` 完整覆盖两文件的每个字节、不重叠不遗漏；
零长度 `copy` 段不生成，零长度 `source` 范围只出现在空字段插入时且仍保持顺序。
相邻 `copy` 段已合并；`replace` 段按源偏移排序。

## 验证（`csv_fidelity/verify.py`）

1. 用 `csv.reader(newline="", strict=True)` 独立读取原件与产物，确认行数/列数一致、
   表头未动、恰好指定逻辑值改变，其余单元格逐值不变；
2. 逐段回放 `map`：`copy` 段源/产物字节必须相等，`replace` 段必须符合引号风格契约
   （带引号字段不得去引号、同值编辑必须保持原字节），且解码值等于 `new`；
3. 校验片段对源与产物完整平铺、顺序正确、无重叠遗漏；
4. 复核 BOM、表头词素、每条记录终止符、末记录终止符有无，以及未被替换单元格的原词素字节。

## 测试覆盖

`tests/test_patcher.py`（48 项）覆盖：夹具字节断言、中文字节偏移、跨行/转义引号、
不同替换长度、引号风格升级与保持、同值修订、空字段插入、无 BOM、裸 CR 终止符、
单列空值可表示性、末行无换行、随机往返、map 平铺与回放、验证器对篡改/BOM/终止符漂移的检测，
以及 CLI 成功/失败路径（失败不发布、拒绝覆盖源文件）。

已知方言取舍：采用 `csv.reader(newline="")` 一致的“裸 CR 也是记录终止符”，
因此裸 CR 不当作字段数据；引号字段内的 CR 仍是数据。
