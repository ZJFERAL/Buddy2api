"""真机渲染冒烟：用 headless Chrome 打开管理页，验证组件和「高级手动添加」弹窗真的渲染出来。

【为什么需要】
  web_binding_check.py 是静态检查；本脚本是**真机渲染**验证，能抓到静态检查覆盖不到的问题
  （模板语法、运行时异常、Vue 版本行为差异等）。历史事故中「弹窗白屏」就是靠它确认的。

【原理】
  index.html 是单文件 Vue 应用，主体逻辑在一个内联 <script> 里，挂载到 #app。
  本脚本把该 script 抽出来，塞进一个测试壳页面：
    1) 用与线上同版本的 Vue（CDN）
    2) 预写 localStorage.cb_gw_token / cb_gw_page=accounts，绕过登录直达账号页
    3) 打桩 api.* 避免真实网络请求
    4) 脚本跑完后点击「高级手动添加」，断言弹窗内容渲染成功
    5) 断言结果写入 <head data-test>，由 --dump-dom 抓取（不依赖控制台输出）
  无论通过与否都把 DOM dump 落到临时目录，便于排查。

【依赖】
  - 本机 Chrome（可用环境变量 CHROME_PATH 指定；默认探测常见安装路径）
  - 需要能访问 CDN（Vue 从 jsdelivr 加载，与线上一致）

【用法】
  python scripts/web_render_smoke.py [web/index.html]
  退出码 0 = PASS；1 = FAIL（页面/弹窗渲染异常）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# 弹窗里必须出现的文本；任一缺失即视为渲染不完整
REQUIRED_TEXTS = ["高级手动添加", "ZCode", "MonkeyCode", "添加 MonkeyCode 账号"]

HARNESS = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>render-smoke</title>
<script src="https://cdn.jsdelivr.net/npm/vue@3.4.21/dist/vue.global.prod.min.js"></script>
<script>
try{localStorage.setItem('cb_gw_token','smoke-token');localStorage.setItem('cb_gw_page','accounts')}catch(e){}
window.__ERRS__=[];
window.addEventListener('error',function(e){window.__ERRS__.push(String(e.message||e.error))});
</script></head><body><div id="app"></div>
<script>
__APP_JS__
</script>
<script>
(function(){
  function report(t,d){
    document.head.setAttribute('data-test',t);
    document.head.setAttribute('data-test-detail',d||'');
  }
  try{
    var stub=function(){return Promise.resolve({})};
    api.get=stub;api.post=stub;api.put=stub;api.del=stub;
  }catch(e){}

  setTimeout(function(){
    try{
      var btns=[].slice.call(document.querySelectorAll('button'));
      var target=btns.filter(function(b){
        return (b.textContent||'').indexOf('高级手动添加')>=0;
      })[0];
      if(!target){report('FAIL-no-button','buttons='+btns.length);return}
      target.click();
      setTimeout(function(){
        var md=document.querySelector('.ov .modal');
        if(!md){
          report('FAIL-no-modal','errs='+(window.__ERRS__||[]).join(' | '));
          return;
        }
        var txt=md.textContent||'';
        var need=__NEED__;
        var miss=need.filter(function(k){return txt.indexOf(k)<0});
        if(miss.length){
          report('FAIL-content','miss='+miss.join(',')+' errs='+(window.__ERRS__||[]).join(' | '));
        }else{
          report('PASS','modal text len='+txt.length);
        }
      },300);
    }catch(e){report('FAIL-exc',String(e))}
  },800);
})();
</script></body></html>
"""


def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("web/index.html")
    if not target.exists():
        print(f"❌ 找不到 {target}")
        return 2

    chrome = find_chrome()
    if not chrome:
        print("⚠️  未找到 Chrome/Edge，跳过真机渲染测试（可用 CHROME_PATH 指定）")
        return 0

    text = target.read_text(encoding="utf-8")
    m = re.search(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)
    if not m:
        print("❌ 未找到内联 <script>")
        return 2
    app_js = m.group(1)
    if "</script" in app_js:
        print("❌ 内联脚本包含 </script，需转义后再内嵌")
        return 2

    harness = (
        HARNESS.replace("__APP_JS__", app_js)
        .replace("__NEED__", "[" + ",".join(f"'{t}'" for t in REQUIRED_TEXTS) + "]")
    )

    tmp = Path(tempfile.mkdtemp(prefix="wb_smoke_"))
    page = tmp / "harness.html"
    dump = tmp / "dump.html"
    page.write_text(harness, encoding="utf-8")

    print(f"chrome : {chrome}")
    print(f"harness: {page}")
    with open(dump, "w", encoding="utf-8") as fh:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-first-run", f"--user-data-dir={tmp / 'profile'}",
             "--virtual-time-budget=8000", "--dump-dom", page.as_uri()],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=240,
        )

    html = dump.read_text(encoding="utf-8", errors="replace")
    mm = re.search(r'data-test="([^"]*)"', html)
    dd = re.search(r'data-test-detail="([^"]*)"', html)
    result = mm.group(1) if mm else "NO-RESULT"
    detail = dd.group(1) if dd else ""

    print(f"result : {result}")
    print(f"detail : {detail}")
    print(f"dump   : {dump}")

    if result == "PASS":
        print("\n✅ 管理页与「高级手动添加」弹窗渲染正常")
        return 0
    print("\n❌ 渲染失败（页面/弹窗可能白屏）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
