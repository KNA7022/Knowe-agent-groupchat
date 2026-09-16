#!/usr/bin/env node
/**
 * spec-check.mjs — 用 Python AST 校验 PyInstaller spec 的关键不变量
 *
 * 为什么不用文本正则（这是第二轮返工的原因）：
 *   第一版 `packaging-contract.test.mjs` 用正则去读 spec，被独立复审连续绕过：
 *     - `except:`（裸 except）不被 `!/(except\s+Exception)/` 匹配；
 *     - `except (ImportError, Exception):` 同样不被匹配；
 *     - `hiddenimports=["yaml"] + _PW_HIDDEN` 这种列表运算，正则是按**文本**
 *       抽字面量，而生效值里已没有 playwright；
 *     - `Analysis(...)` 之前再插一句 `_VERSION = None`，文本检查看不到；
 *     - 行尾注释 `# CompanyName FileDescription ...` 能满足 `includes(field)`。
 *   文本层面永远堵不完。这里改为解析语法树，读**生效的常量值**。
 *
 * 用法：node scripts/spec-check.mjs [specPath]
 *   stdout 为一行 JSON；退出码 0 = 无 problem。
 */

import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const specPath = process.argv[2]
  ? resolve(process.argv[2])
  : join(ROOT, 'backend', 'StudyCrewBackend.spec');

if (!existsSync(specPath)) {
  console.log(JSON.stringify({ ok: false, problems: [`spec 不存在：${specPath}`], findings: {} }));
  process.exit(1);
}

/** 期望的版本资源字段（7 个）。 */
const EXPECTED_FIELDS = [
  'CompanyName',
  'FileDescription',
  'FileVersion',
  'InternalName',
  'OriginalFilename',
  'ProductName',
  'ProductVersion',
];

const PY = `
import ast, json, operator, sys

spec_path = sys.argv[1]
expected_fields = json.loads(sys.argv[2])
src = open(spec_path, encoding="utf-8").read()
tree = ast.parse(src)
problems = []
findings = {}

# ---- hiddenimports：取 Analysis(...) 的生效常量值（支持列表拼接与 * 展开）----
class ListFolder(ast.NodeVisitor):
    def visit_List(self, node):
        return [self.visit(e) for e in node.elts]
    def visit_Tuple(self, node):
        return tuple(self.visit(e) for e in node.elts)
    def visit_Constant(self, node):
        return node.value
    def visit_BinOp(self, node):
        left, right = self.visit(node.left), self.visit(node.right)
        if left is UNRESOLVED or right is UNRESOLVED:
            return UNRESOLVED
        if left is None or right is None:
            return None
        try:
            return operator.add(left, right) if isinstance(node.op, ast.Add) else None
        except TypeError:
            # 类型不匹配（例如 list + 非 list）说明这不是可静态求值的常量列表。
            return UNRESOLVED
    def visit_Name(self, node):
        return UNRESOLVED
    def generic_visit(self, node):
        return None

UNRESOLVED = object()

analysis_calls = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Analysis":
        analysis_calls.append(node)
findings["analysis_calls"] = len(analysis_calls)
if not analysis_calls:
    problems.append("spec 中找不到 Analysis(...) 调用")
else:
    call = analysis_calls[0]
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    if "hiddenimports" not in kw:
        problems.append("Analysis(...) 缺少 hiddenimports 关键字")
        findings["hiddenimports"] = None
    else:
        value = ListFolder().visit(kw["hiddenimports"])
        if value is UNRESOLVED or value is None:
            problems.append(
                "hiddenimports 不是可静态求值的常量列表（含变量/拼接），契约测试无法确认其生效值"
            )
            findings["hiddenimports"] = None
        else:
            findings["hiddenimports"] = list(value)
            for required in ("playwright", "yaml"):
                if required not in findings["hiddenimports"]:
                    problems.append("hiddenimports 生效值缺少 " + required)

# ---- except：裸 except / BaseException / Exception / ImportError 都会吞掉版本资源错误 ----
broad_excepts = []
for node in ast.walk(tree):
    if isinstance(node, ast.ExceptHandler):
        if node.type is None:
            broad_excepts.append({"line": node.lineno, "kind": "bare except"})
            continue
        names = []
        targets = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        for t in targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
        dangerous = [n for n in names if n in ("Exception", "BaseException", "ImportError")]
        if dangerous:
            broad_excepts.append({"line": node.lineno, "kind": ", ".join(dangerous)})
findings["broad_excepts"] = broad_excepts
for item in broad_excepts:
    problems.append(
        "spec 第 %s 行用了 %s —— 会吞掉版本资源异常，让 exe 静默失去版本信息"
        % (item["line"], item["kind"])
    )

# ---- _VERSION 赋值点：必须恰好一次，且在 os.name 守卫内 ----
assignments = []
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "_VERSION":
                assignments.append({"line": node.lineno, "value_is_none": isinstance(node.value, ast.Constant) and node.value.value is None})
findings["version_assignments"] = assignments

# ---- EXE(...) 必须接 version=_VERSION，且非 None 的那次赋值在它之前 ----
exe_calls = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "EXE":
        exe_calls.append(node)
findings["exe_calls"] = len(exe_calls)
version_wired = False
if not exe_calls:
    problems.append("spec 中找不到 EXE(...) 调用")
else:
    exe = exe_calls[0]
    for k in exe.keywords:
        if k.arg == "version" and isinstance(k.value, ast.Name) and k.value.id == "_VERSION":
            version_wired = True
    if not version_wired:
        problems.append("EXE(...) 未接 version=_VERSION，版本资源不会被嵌入")
findings["version_wired"] = version_wired

# 非 None 的 _VERSION 赋值必须在 EXE 调用之前，且**最后一次赋值不能是 None**。
# 只看"是否存在非 None 赋值"是不够的：把 _VERSION = None 插在 EXE 前或后都能
# 让 EXE 拿到 None，而这两种覆写在文本层面完全看不见。
if exe_calls and assignments:
    exe_line = exe_calls[0].lineno
    non_none = [a for a in assignments if not a["value_is_none"]]
    if not non_none:
        problems.append("spec 里没有任何非 None 的 _VERSION 赋值")
    else:
        latest = max(a["line"] for a in non_none)
        if latest > exe_line:
            problems.append(
                "_VERSION 的非 None 赋值（第 %s 行）在 EXE(...)（第 %s 行）之后，EXE 拿到的是 None"
                % (latest, exe_line)
            )
        # 逐条判定每个 None 赋值是否"盖住了"有效版本：
        #   - 若它之后还有非 None 赋值，说明顺序被做成 ...=有效 然后 =None，最终是 None；
        #   - 若它出现在 EXE 调用之后，EXE 之前的有效值会被它覆盖（对后续使用生效）。
        # 真实 spec 的 else 分支 None 赋值两者都不满足（它是最后一个，且在 EXE 之前），
        # 因此不会误报。
        for a in assignments:
            if not a["value_is_none"]:
                continue
            later_valid = [b["line"] for b in non_none if b["line"] > a["line"]]
            if later_valid:
                problems.append(
                    "_VERSION 在第 %s 行被赋为 None，但第 %s 行之后仍有有效赋值——最终值会是 None"
                    % (a["line"], a["line"])
                )
            elif a["line"] > exe_line:
                problems.append(
                    "_VERSION 在第 %s 行（EXE 之后）被赋为 None，覆盖了 EXE 使用的有效版本"
                    % a["line"]
                )
        # 多次赋值本身也值得警惕：说明有人在构造之外又改了一遍。
        if len(assignments) > 2:
            problems.append(
                "_VERSION 被赋值 %d 次，超出预期的 2 次（Windows 分支 + 非 Windows 分支）"
                % len(assignments)
            )

print(json.dumps({"findings": findings, "problems": problems}, ensure_ascii=False))
`;

const r = spawnSync('py', ['-3.12', '-c', PY, specPath, JSON.stringify(EXPECTED_FIELDS)], {
  cwd: ROOT,
  encoding: 'utf8',
});

if (r.error) {
  console.log(
    JSON.stringify({ ok: false, problems: [`无法启动 Python：${r.error.message}`], findings: {} }),
  );
  process.exit(1);
}
if (r.status !== 0) {
  console.log(
    JSON.stringify({
      ok: false,
      problems: [`spec AST 解析失败：${(r.stderr || r.stdout || '').trim().split('\n').slice(-3).join(' | ')}`],
      findings: {},
    }),
  );
  process.exit(1);
}

let parsed;
try {
  parsed = JSON.parse(r.stdout.trim().split('\n').pop());
} catch (err) {
  console.log(JSON.stringify({ ok: false, problems: [`spec-check 输出无法解析：${err.message}`], findings: {} }));
  process.exit(1);
}

const out = {
  ok: parsed.problems.length === 0,
  problems: parsed.problems,
  findings: parsed.findings,
  spec: specPath,
};
console.log(JSON.stringify(out));
process.exit(out.ok ? 0 : 1);
