"""静态校验 web/index.html 中 Vue 组件的模板绑定是否都在 setup() return 里导出。

【为什么需要这个脚本】
  本项目 web/index.html 是「单文件 Vue 3 运行时编译」应用：6 个组件都用
  `template:` 反引号字符串定义。Vue 会把渲染函数包进 `with(_ctx){...}`，
  于是模板里标识符的解析顺序是：
      _ctx（setup return + props） → 模块作用域 → 全局
  若某个名字既不在 return/props，也不在模块作用域/全局里，渲染时抛
  ReferenceError，**整个组件渲染失败 → 页面或弹窗一片空白**，
  而控制台往往只有一行报错，极难定位。

【历史事故（同一类 bug 已发生两次）】
  - zcode 区块：模板加了、setup 函数写了，忘了加进 return → 白屏
  - monkeycode 区块：同上，另加 `refreshResource` 被模板 @click 引用但未导出
    → 整个账号页（含表格行）白屏

【用法】
  python scripts/web_binding_check.py [web/index.html]
  退出码 0 = 通过；1 = 有缺失（必须修）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ── 白名单：全局 & 模块作用域（模板里合法但不需要 return 的名字）─────────────
JS_GLOBALS = {
    "Math", "Number", "String", "Object", "Array", "JSON", "Date", "Boolean",
    "RegExp", "Promise", "Set", "Map", "WeakMap", "WeakSet", "Error",
    "TypeError", "RangeError", "Symbol", "BigInt", "Function", "Proxy",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "NaN", "Infinity",
    "console", "window", "document", "localStorage", "sessionStorage",
    "navigator", "location", "history", "alert", "confirm", "prompt",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "fetch", "Blob", "URL",
    "FormData", "FileReader", "Image", "CustomEvent", "Event",
    "structuredClone", "queueMicrotask", "globalThis",
}

VUE_GLOBALS = {
    "ref", "reactive", "computed", "watch", "watchEffect", "watchPostEffect",
    "onMounted", "onUnmounted", "onBeforeMount", "onBeforeUnmount",
    "nextTick", "defineComponent", "createApp", "provide", "inject",
    "toRefs", "toRef", "unref", "shallowRef", "triggerRef", "markRaw",
    "readonly", "shallowReactive", "h", "resolveComponent", "useSlots",
    "useAttrs", "Vue", "echarts",
}

# index.html 模块作用域里自定义的（with 会向外层作用域穿透，模板可直接用）
MODULE_SCOPE = {"api", "apiErr", "I", "toast"}

KEYWORDS = {
    "true", "false", "null", "undefined", "new", "typeof", "instanceof",
    "in", "of", "return", "if", "else", "void", "delete", "this", "function",
    "await", "async", "do", "while", "for", "let", "const", "var", "try",
    "catch", "throw", "class", "extends", "super", "yield", "case", "switch",
    "break", "continue", "default", "finally", "static", "get", "set",
}

WHITELIST = JS_GLOBALS | VUE_GLOBALS | MODULE_SCOPE

STR_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
TICK_RE = re.compile(r"`[^`]*`")
# 独立数字（前后不能是标识符字符），避免把 expiring_30d_total 拆坏
NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
IDENT_RE = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)")
DIR_RE = re.compile(
    r'\s(?:@[\w.:\-\[\]]+|v-[\w.:\-\[\]]+|:[\w.:\-\[\]]+)="([^"]*)"'
)
MUSTACHE_RE = re.compile(r"\{\{(.+?)\}\}", re.S)
COMP_HEAD_RE = re.compile(r"\.component\('(\w+)',\s*\{props:\[([^\]]*)\]")


def extract_components(text: str):
    """返回 [(name, props, setup_body, template_body), ...]"""
    marks = []
    for m in COMP_HEAD_RE.finditer(text):
        props = re.findall(r"'([^']+)'", m.group(2))
        marks.append((m.group(1), props, m.start(), m.end()))
    out = []
    for i, (name, props, _s, body_start) in enumerate(marks):
        end_limit = marks[i + 1][2] if i + 1 < len(marks) else len(text)
        chunk = text[body_start:end_limit]
        tm = re.search(r"\},template:`", chunk)
        if not tm:
            continue
        setup_body = chunk[: tm.start()]
        after = body_start + tm.end()
        em = re.search(r"`\s*\}\)", text[after:])
        if not em:
            continue
        out.append((name, props, setup_body, text[after : after + em.start()]))
    return out


def return_keys(setup_body: str) -> set[str]:
    cands = re.findall(r"return\{([^}]*)\}", setup_body)
    if not cands:
        return set()
    keys: set[str] = set()
    for part in cands[-1].split(","):
        key = part.strip().split(":")[0].strip().lstrip(".")
        if re.fullmatch(r"[A-Za-z_$][\w$]*", key):
            keys.add(key)
    return keys


def vfor_aliases(tpl: str) -> set[str]:
    out: set[str] = set()
    for m in re.finditer(r'v-for="\(?([^"]+?)\)?\s+in\s', tpl):
        for name in re.split(r"[,\s]+", m.group(1)):
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name.strip()):
                out.add(name.strip())
    return out


def template_idents(tpl: str) -> set[str]:
    exprs = MUSTACHE_RE.findall(tpl) + DIR_RE.findall(tpl)
    found: set[str] = set()
    for e in exprs:
        e = STR_RE.sub(" ", e)
        e = TICK_RE.sub(" ", e)
        e = NUM_RE.sub(" ", e)
        for m in IDENT_RE.finditer(e):
            name = m.group(1)
            if name in KEYWORDS or name.startswith("$"):
                continue
            pre, post = e[: m.start()].rstrip(), e[m.end() :].lstrip()
            # 跳过对象字面量的 key：{key: 或 ,key:
            if post.startswith(":") and (pre.endswith("{") or pre.endswith(",")):
                continue
            found.add(name)
    return found


def check(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    comps = extract_components(text)
    if not comps:
        print("❌ 未解析到任何组件，检查文件结构/正则")
        return 2

    total = 0
    for name, props, setup_body, tpl in comps:
        keys = return_keys(setup_body) | set(props) | {"p"} | vfor_aliases(tpl)
        used = template_idents(tpl)
        missing = sorted(x for x in used - keys if x not in WHITELIST)
        print(f"{'✅' if not missing else '❌'} [{name}] "
              f"props={len(props)} return={len(keys)} template={len(used)}")
        for x in missing:
            print(f"     缺少导出 → 模板引用会 ReferenceError: {x}")
        total += len(missing)

    print()
    if total:
        print(f"❌ 合计 {total} 处缺失绑定，会导致组件白屏")
        return 1
    print("✅ 全部模板绑定均已导出")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("web/index.html")
    sys.exit(check(target))
