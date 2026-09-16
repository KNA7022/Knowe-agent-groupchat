#!/usr/bin/env node
/**
 * spec-check.mjs — 对 PyInstaller spec 做结构检查
 *
 * ⚠ 定位说明（很重要）：**这是 lint，不是契约。**
 *   它已被两轮独立复审击穿（文本正则版、启发式 AST 版），结论是：
 *   "匹配手写启发式"永远堵不完（别名 catch、exec/globals 写入、decoy 调用……）。
 *   真正兜底的两件事是：
 *     1. spec 里的版本块**没有任何 try/except**，写错类名直接让构建崩
 *        （见 backend/StudyCrewBackend.spec 的注释）；
 *     2. `scripts/packaging-contract.test.mjs` 里**读真实 exe** 的产物级断言。
 *   本文件只负责把明显错误在构建前就报出来，以及在本机**执行求值**验证
 *   `_VERSION` 真的不是 None（这一条能覆盖别名/动态写入等启发式看不见的情况）。
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

const PY = `
import ast, json, os, subprocess, sys, textwrap

spec_path = sys.argv[1]
src = open(spec_path, encoding="utf-8").read()
tree = ast.parse(src)
problems = []
findings = {}

# ── 1. Analysis / EXE 必须各自唯一（decoy 调用会让"取第一个"的检查失效） ──
def calls_named(name):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name]

analysis_calls = calls_named("Analysis")
exe_calls = calls_named("EXE")
findings["analysis_calls"] = len(analysis_calls)
findings["exe_calls"] = len(exe_calls)
if len(analysis_calls) != 1:
    problems.append("spec 应恰好有 1 个 Analysis(...) 调用，实际 %d 个（多余的调用会让检查与构建脱节）" % len(analysis_calls))
if len(exe_calls) != 1:
    problems.append("spec 应恰好有 1 个 EXE(...) 调用，实际 %d 个（decoy 调用可让 version= 接线检查失效）" % len(exe_calls))

# ── 2. hiddenimports 必须是可静态求值的常量列表 ──
class ListFolder(ast.NodeVisitor):
    def visit_List(self, node):
        return [self.visit(e) for e in node.elts]
    def visit_Tuple(self, node):
        return tuple(self.visit(e) for e in node.elts)
    def visit_Constant(self, node):
        return node.value
    def visit_Set(self, node):
        # 不能用集合推导式：它本身也是 ast.Set，会无限递归。
        return set(self.visit(e) for e in node.elts)
    def visit_BinOp(self, node):
        left, right = self.visit(node.left), self.visit(node.right)
        if left is UNRESOLVED or right is UNRESOLVED:
            return UNRESOLVED
        if left is None or right is None:
            return None
        try:
            return operator.add(left, right) if isinstance(node.op, ast.Add) else None
        except TypeError:
            return UNRESOLVED
    def visit_Name(self, node):
        return UNRESOLVED
    def generic_visit(self, node):
        return None

UNRESOLVED = object()
if analysis_calls:
    kw = {k.arg: k.value for k in analysis_calls[0].keywords if k.arg}
    if "hiddenimports" not in kw:
        problems.append("Analysis(...) 缺少 hiddenimports 关键字")
        findings["hiddenimports"] = None
    else:
        value = ListFolder().visit(kw["hiddenimports"])
        if value is UNRESOLVED or value is None:
            problems.append("hiddenimports 不是可静态求值的常量列表（含变量/拼接/set），无法确认生效值")
            findings["hiddenimports"] = None
        else:
            findings["hiddenimports"] = list(value)
            for required in ("playwright", "yaml"):
                if required not in findings["hiddenimports"]:
                    problems.append("hiddenimports 生效值缺少 " + required)

# ── 3. 版本块不得有任何 except（失败必须响，不能静默） ──
#
# 这里是**结构性**约束而不是"异常类型白名单"：白名单能被别名、属性访问、
# 元组、exec 绕过（复审已实测）。版本块的正确做法是根本不写 try/except，
# 写错类名就让 PyInstaller 直接崩。所以规则是：出现 except 就是问题。
except_handlers = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
findings["except_lines"] = except_handlers
for line in except_handlers:
    problems.append(
        "spec 第 %s 行存在 except —— 版本块不应捕获任何异常（写错类名必须让构建崩，而不是静默产出没有版本信息的 exe）"
        % line
    )

# ── 4. _VERSION 赋值：只认直接赋值；动态写入视为不可证明 ──
assign_lines = []
bad_forms = []
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "_VERSION":
                assign_lines.append(node.lineno)
    # 增强赋值 / 注解赋值 / 下标赋值 都说明写法超出预期
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "_VERSION":
        bad_forms.append((node.lineno, "AugAssign"))
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "_VERSION":
        bad_forms.append((node.lineno, "AnnAssign"))
    # globals()["_VERSION"] = ... / setattr / exec / eval
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Subscript) and getattr(getattr(t.value, "func", None), "id", None) == "globals":
                bad_forms.append((node.lineno, "globals()[...]"))
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("exec", "eval"):
        bad_forms.append((node.lineno, getattr(node.func, "id")))
findings["version_assign_lines"] = sorted(assign_lines)
findings["bad_version_forms"] = bad_forms
for line, kind in bad_forms:
    problems.append("spec 第 %s 行用 %s 写入版本相关状态——静态检查无法证明其结果，禁止使用" % (line, kind))

# ── 5. EXE(...) 必须恰好一次接 version=_VERSION，且在最后一个 _VERSION 赋值之后 ──findings["version_wired"] = False
if exe_calls:
    exe = exe_calls[0]
    version_arg = None
    for k in exe.keywords:
        if k.arg == "version":
            version_arg = k.value
    if version_arg is None:
        problems.append("EXE(...) 未传 version=，版本资源不会被嵌入")
    elif not (isinstance(version_arg, ast.Name) and version_arg.id == "_VERSION"):
        problems.append("EXE(...) 的 version= 不是直接的 _VERSION（别名/表达式会让静态检查失效）")
    else:
        findings["version_wired"] = True
        exe_line = exe.lineno
        effective = [l for l in assign_lines if l < exe_line]
        findings["effective_assign_line"] = max(effective) if effective else None
        if not effective:
            problems.append("EXE(...) 之前没有任何 _VERSION 赋值，嵌入的会是 None")
        elif max(effective) != max(assign_lines):
            # 有效赋值之后又出现了赋值：可能是覆写（危险），也可能是 else 分支（安全）。
            # 这里不做过强判断，交给第 6 步的执行求值裁决。
            findings["later_assigns_after_effective"] = [l for l in assign_lines if l > max(effective)]

# ── 6. 执行求值：真的跑一遍版本赋值，看 _VERSION 最终是不是 None ──
#
# 这是唯一能覆盖"别名 catch / 动态写入 / 复杂分支"的检查——它不猜语法，真的求值。
# 做法：把"含 _VERSION 赋值的 if 语句"**整体**作为探针语句执行，这样 spec 自己的
# sys.platform 守卫会自然选出正确分支；再加上无条件的 _VERSION 赋值。
# 直接逐条执行赋值语句（忽略 if）会让两个分支都跑，最终值被 else 覆盖而误报。
version_assigns = [
    n for n in ast.walk(tree)
    if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "_VERSION" for t in n.targets
    )
]
version_assigns.sort(key=lambda n: n.lineno)

# 与版本相关的顶层语句：import、含 _VERSION 赋值的 If/IfExp、无条件 _VERSION 赋值。
probe_stmts = []
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        probe_stmts.append(node)
        continue
    if isinstance(node, ast.If):
        if any(isinstance(sub, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_VERSION" for t in sub.targets
        ) for sub in ast.walk(node)):
            probe_stmts.append(node)
        continue
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "_VERSION" for t in node.targets
    ):
        probe_stmts.append(node)
probe_stmts.sort(key=lambda n: n.lineno)

code = None
if version_assigns and probe_stmts:
    lines = ["import json, os, sys", "sys.platform = 'win32'"]
    for node in probe_stmts:
        seg = ast.get_source_segment(src, node)
        if seg:
            lines.append(textwrap.dedent(seg))
    lines.append("__picked = _VERSION")
    lines.append("print(json.dumps({'is_none': __picked is None, 'repr_len': len(str(__picked))}))")
    code = "\\n".join(lines)

    # 先自检：生成的探针脚本本身必须能编译，否则是检查器的 bug 而不是 spec 的问题。
    try:
        compile(code, "<probe>", "exec")
    except SyntaxError as exc:
        dump = os.path.join(os.environ.get("TEMP", "/tmp"), "spec-check-probe-failed.py")
        with open(dump, "w", encoding="utf-8") as handle:
            handle.write(code)
        problems.append(
            "探针脚本自身语法错误（检查器 bug，非 spec 问题）：%s；已导出到 %s" % (exc, dump)
        )
        code = None

if code is not None:
    run = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    findings["probe_returncode"] = run.returncode
    if run.returncode != 0:
        # ImportError/异常别名 catch 都会走到这里：这正说明版本块会静默失败。
        problems.append(
            "版本块执行求值失败（说明它会静默失败或写法异常）：%s"
            % (run.stderr.strip().splitlines()[-1] if run.stderr.strip() else "unknown")
        )
    else:
        try:
            result = json.loads(run.stdout.strip().splitlines()[-1])
        except Exception as exc:
            problems.append("版本块执行求值输出无法解析：%s" % exc)
        else:
            findings["version_is_none_at_runtime"] = result["is_none"]
            findings["version_repr_len"] = result["repr_len"]
            if result["is_none"] or result["repr_len"] == 0:
                problems.append("执行求值显示 _VERSION 最终为 None/空——exe 将失去版本资源")
else:
    problems.append("spec 里没有任何可求值的 _VERSION 赋值，版本资源不会被嵌入")

print(json.dumps({"findings": findings, "problems": problems}, ensure_ascii=False))
`;

const r = spawnSync('py', ['-3.12', '-c', PY, specPath], {
  cwd: ROOT,
  encoding: 'utf8',
  env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
});

if (r.error) {
  console.log(
    JSON.stringify({ ok: false, problems: [`无法启动 Python：${r.error.message}`], findings: {} }),
  );
  process.exit(1);
}
if (r.status !== 0) {
  // 输出完整 traceback：只看最后一行会丢掉真正的原因。
  const tail = (r.stderr || r.stdout || '').trim().split('\n').slice(-15).join(' | ');
  console.log(JSON.stringify({ ok: false, problems: [`spec 解析/求值失败：${tail}`], findings: {} }));
  process.exit(1);
}

let parsed;
try {
  parsed = JSON.parse(r.stdout.trim().split('\n').pop());
} catch (err) {
  console.log(
    JSON.stringify({ ok: false, problems: [`spec-check 输出无法解析：${err.message}`], findings: {} }),
  );
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
