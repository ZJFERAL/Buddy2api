#!/usr/bin/env node
/**
 * 阿里云无痕验证码求解器（独立 Node 脚本）。
 *
 * 机制翻译自 ZCode Proxy（zcode-api）captcha-happy.ts：
 *   1. cookie priming：先 GET https://zcode.z.ai/ 拿初始 cookie（5 分钟缓存）
 *   2. CDN 磁盘缓存：~/.zcode-captcha-cdn-cache/<sha1(url)>，SDK 脚本落盘
 *   3. happy-dom 模拟浏览器（WebGL/canvas/Worker/matchMedia/OffscreenCanvas 桩）
 *   4. 注入阿里云无痕 SDK（o.alicdn.com）→ initAliyunCaptcha +
 *      getInstance().startTracelessVerification() → 回调查到 verifyParam
 *   5. stdout 输出 "VERIFY_PARAM=xxx"
 *
 * 用法: node solver.js <sceneId> <region> <prefix>
 * 依赖: happy-dom（同目录 npm install；缺依赖时输出友好错误）
 *
 * 说明：完整反检测（指纹混淆/事件代理）以 zcode-api captcha-happy.ts 为蓝本，
 * 本文件为实现可独立复刻的基础版；若线上对拍不过，请将
 * ZCODE_CAPTCHA_SOLVER_JS 指向 zcode2api 仓库已验证的 solver.js。
 */
const path = require("path");
const fs = require("fs");
const os = require("os");
const crypto = require("crypto");

const [scene, region, prefix] = process.argv.slice(2);
if (!scene || !region || !prefix) {
  console.error("usage: node solver.js <sceneId> <region> <prefix>");
  process.exit(2);
}

const CDN_CACHE_DIR = path.join(os.homedir(), ".zcode-captcha-cdn-cache");
// SDK 地址（按序回退）。注意：旧版带版本号的路径
//   /captcha-web/2.1.9/aliyun-captcha.min.js
// 已被阿里云下线（实测 404），线上现行入口为不带版本号的 captcha-frontend。
// 这里保留旧地址仅为兜底，主入口以现行地址为准。
const SDK_URLS = [
  "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js",
  "https://o.alicdn.com/captcha-web/aliyun-captcha.min.js",
  "https://o.alicdn.com/captcha-web/2.1.9/aliyun-captcha.min.js",
];
const PRIME_URL = "https://zcode.z.ai/";
let happyDom = null;

try {
  happyDom = require("happy-dom");
} catch (e) {
  console.error("缺少 happy-dom 依赖，请在 solver.js 同目录执行: npm i happy-dom");
  process.exit(3);
}

const { Window } = happyDom;

function cachePath(url) {
  const sha = crypto.createHash("sha1").update(url).digest("hex");
  return path.join(CDN_CACHE_DIR, sha);
}

async function fetchWithCache(url, opts = {}) {
  const cp = cachePath(url);
  if (fs.existsSync(cp)) {
    const hit = fs.readFileSync(cp);
    // 缓存校验：真 SDK 必含 initAliyunCaptcha；否则视为脏缓存重取
    if (hit.includes("initAliyunCaptcha")) return hit;
    try {
      fs.unlinkSync(cp);
    } catch (e) {
      /* ignore */
    }
  }
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`fetch ${url} -> ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  if (!buf.includes("initAliyunCaptcha")) {
    throw new Error(`fetch ${url} -> 返回内容不是验证码 SDK`);
  }
  try {
    fs.mkdirSync(CDN_CACHE_DIR, { recursive: true });
    fs.writeFileSync(cp, buf);
  } catch (e) {
    /* 缓存失败不影响本次 */
  }
  return buf;
}

/** 按 SDK_URLS 顺序取第一个可用 SDK，返回 {buf, url}。 */
async function fetchSdk(opts = {}) {
  const errs = [];
  for (const url of SDK_URLS) {
    try {
      return { buf: await fetchWithCache(url, opts), url };
    } catch (e) {
      errs.push(e && e.message ? e.message : String(e));
    }
  }
  throw new Error(`SDK 全部地址不可用: ${errs.join(" | ")}`);
}

async function primeCookies(window) {
  try {
    const res = await fetch(PRIME_URL, {
      headers: {
        "User-Agent": window.navigator.userAgent || "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      },
    });
    const setCookies = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
    setCookies.forEach((c) => {
      const name = c.split("=")[0].trim();
      const value = c.split(";")[0].split("=").slice(1).join("=");
      if (name) window.document.cookie = `${name}=${value}`;
    });
  } catch (e) {
    /* cookie priming 失败不阻断（SDK 可能不需要） */
  }
}

function installStubs(window) {
  // matchMedia
  window.matchMedia =
    window.matchMedia ||
    function matchMedia() {
      return {
        matches: false,
        media: "",
        onchange: null,
        addListener() {},
        removeListener() {},
        addEventListener() {},
        removeEventListener() {},
        dispatchEvent() {
          return false;
        },
      };
    };
  // OffscreenCanvas
  if (!window.OffscreenCanvas) {
    window.OffscreenCanvas = window.HTMLCanvasElement;
  }
  // WebGL 桩（阿里云 SDK 会探测 webgl；happy-dom 缺省无 getContext）
  const origGetContext = window.HTMLCanvasElement.prototype.getContext;
  window.HTMLCanvasElement.prototype.getContext = function (type, ...args) {
    if (type === "webgl" || type === "experimental-webgl") {
      return {
        getParameter: () => null,
        getExtension: () => null,
        getContextAttributes: () => ({ alpha: true, antialias: true, depth: true }),
        createBuffer: () => ({}),
        createProgram: () => ({}),
        createShader: () => ({}),
        getShaderParameter: () => true,
        getProgramParameter: () => true,
        shaderSource() {},
        compileShader() {},
        attachShader() {},
        linkProgram() {},
        bindBuffer() {},
        bufferData() {},
        getUniformLocation: () => ({}),
        uniform1f() {},
        uniform2f() {},
        viewport() {},
        clearColor() {},
        clear() {},
        drawArrays() {},
        getError: () => 0,
        isContextLost: () => false,
      };
    }
    return origGetContext ? origGetContext.apply(this, [type, ...args]) : null;
  };
  // Worker 桩
  if (!window.Worker) {
    window.Worker = class {
      constructor() {}
      postMessage() {}
      terminate() {}
      addEventListener() {}
      removeEventListener() {}
    };
  }
  // btoa / atob
  if (!window.btoa) {
    window.btoa = (s) => Buffer.from(String(s), "binary").toString("base64");
    window.atob = (s) => Buffer.from(String(s), "base64").toString("binary");
  }
  // requestAnimationFrame 桩
  window.requestAnimationFrame = window.requestAnimationFrame || ((cb) => setTimeout(() => cb(Date.now()), 16));
  window.cancelAnimationFrame = window.cancelAnimationFrame || ((id) => clearTimeout(id));
  // Event.isTrusted（guest 侧校验）
  const origDesc = Object.getOwnPropertyDescriptor(Event.prototype, "isTrusted");
  if (origDesc && !origDesc.get) {
    Object.defineProperty(Event.prototype, "isTrusted", {
      get() {
        return true;
      },
      configurable: true,
    });
  }
}

/** success 回调归一化：不同 verifyType 返回结构不同。
 *  1.0 → CertifyId 字符串；3.0/2.0 → base64(certifyId/sceneId/isSign) 或 {verifyParam,...} 对象。 */
function pickVerifyParam(result) {
  if (result == null) return "";
  if (typeof result === "string") return result.trim();
  if (typeof result === "object") {
    const cand =
      result.verifyParam || result.captchaVerifyParam || result.data || result.param || result.CertifyId;
    return cand ? String(cand).trim() : "";
  }
  return String(result).trim();
}

async function solve() {
  const window = new Window({ url: PRIME_URL });
  installStubs(window);
  await primeCookies(window);

  // SDK 需要挂载点（element/button 选择器必须能在文档里找到），并读取
  // window.AliyunCaptchaConfig 里的 region/prefix。
  window.document.body.innerHTML =
    '<div id="cap"></div><button id="btn" type="button"></button>';
  window.AliyunCaptchaConfig = { region, prefix };

  // 注入 SDK（多地址回退）
  const sdk = await fetchSdk({
    headers: { "User-Agent": window.navigator.userAgent || "" },
  });
  window.eval(sdk.buf.toString("utf-8"));

  const window_ = window;
  if (typeof window_.initAliyunCaptcha !== "function") {
    throw new Error("SDK 加载后未找到 initAliyunCaptcha");
  }

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("solve timeout")), 25000);
    const done = (fn) => (v) => {
      clearTimeout(timer);
      fn(v);
    };
    try {
      window_.initAliyunCaptcha({
        SceneId: scene,
        // 现行 SDK 仅支持 popup / embed（旧版 traceless 已下线）
        mode: "popup",
        region,
        prefix,
        language: "en",
        element: "#cap",
        button: "#btn",
        captchaLogoImg: "",
        showErrorTip: false,
        // 实例通过回调下发（旧版 window.getInstance() 全局已不存在）
        getInstance: (inst) => {
          try {
            const start = inst.startTracelessVerification || inst.show || inst.verify;
            if (typeof start !== "function") {
              done(reject)(new Error("实例无可用触发方法"));
              return;
            }
            start.call(inst);
          } catch (e) {
            done(reject)(new Error(`start: ${e && e.message ? e.message : e}`));
          }
        },
        success: (result) => {
          if (result && typeof result === "object" && result.verifyResult === false) {
            done(reject)(
              new Error(
                `verify rejected: ${JSON.stringify({
                  verifyCode: result.verifyCode,
                  certifyId: result.certifyId,
                })}`,
              ),
            );
            return;
          }
          const vp = pickVerifyParam(result);
          if (!vp) {
            done(reject)(new Error(`success 回调缺少 verifyParam: ${JSON.stringify(result).slice(0, 200)}`));
            return;
          }
          process.stdout.write(`VERIFY_PARAM=${vp}\n`);
          done(resolve)(vp);
        },
        fail: (err) => done(reject)(new Error(`fail: ${JSON.stringify(err || {}).slice(0, 200)}`)),
        onError: (err) => done(reject)(new Error(`onError: ${JSON.stringify(err || {}).slice(0, 200)}`)),
      });
    } catch (e) {
      clearTimeout(timer);
      reject(e);
    }
  });
}

solve().catch((e) => {
  console.error(`[solver-error] ${e && e.message ? e.message : e}`);
  process.exit(1);
});
