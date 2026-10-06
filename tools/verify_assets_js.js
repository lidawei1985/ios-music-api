/* 端到端验证：assets/<kind>.js 能不能被「端上那套 loadScript」吃到，且 skey 能否命中。
   为什么要在本机验证：端上是 file:// + <script> 注入，这条路在 Mac/手机上试一次很贵，
   本地用真文件 + 真 HTTP + 真 skey 规则跑一遍，能把「格式对不对、键对不对」先钉死。 */
const http = require('http'), fs = require('fs'), path = require('path');
const ROOT = path.join(__dirname, '..', 'data');

/* ① 与内核 app.html 逐字符一致的 norm/skey（唯一契约） */
const norm = s => String(s || "").toLowerCase()
  .replace(/[\s\-_（）()\[\]【】·,.，。'"!！?？~～&]/g, "");
const skey = (t, s) => norm(t).slice(0, 40) + "|" + norm(s).slice(0, 30);

/* ② 极简静态服务，模拟 jsDelivr */
const srv = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "");
  const p = path.join(ROOT, rel);
  if (!p.startsWith(ROOT)) { res.writeHead(403); return res.end(); }
  fs.readFile(p, (e, b) => {
    if (e) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "Content-Type": "application/javascript" });
    res.end(b);
  });
});

/* ③ 模拟内核的 loadScript：注入 <script src> 并等它执行完 */
function makeLoadScript(port, win) {
  return (rel) => new Promise(resolve => {
    const url = `http://127.0.0.1:${port}/${rel}`;
    http.get(url, r => {
      if (r.statusCode !== 200) { r.resume(); return resolve(false); }
      let d = "";
      r.setEncoding("utf8");
      r.on("data", c => d += c);
      r.on("end", () => {
        try { new Function("window", d)(win); resolve(true); }
        catch (e) { resolve(false); }
      });
    }).on("error", () => resolve(false));
  });
}

srv.listen(0, async () => {
  const port = srv.address().port;
  const win = {};
  const loadScript = makeLoadScript(port, win);

  console.log("=== ① 三种索引能否按端上方式加载 ===");
  for (const k of ["av", "kv", "cv"]) {
    const ok = await loadScript(`assets/${k}.js`);
    const v = win["__DWG_ASSETS_" + k.toUpperCase()];
    const n = v && v.map ? Object.keys(v.map).length : "-";
    console.log(`  assets/${k}.js  加载=${ok}  条数=${n}`);
  }

  console.log("=== ② 键构成（端上只认 歌名|歌手 这种）===");
  const cv = (win.__DWG_ASSETS_CV || {}).map || {};
  const ks = Object.keys(cv);
  const isAlias = k => !k.startsWith("u:") && !k.startsWith("mv:");
  console.log(`  总键 ${ks.length} / 别名键(歌名|歌手) ${ks.filter(isAlias).length}`);

  console.log("=== ③ 用真曲库验命中（端上 skey 查得到吗）===");
  const shard = path.join(ROOT, "catalog", "shard-0000.json");
  if (!fs.existsSync(shard)) { console.log("  没有 catalog 分片，跳过"); srv.close(); return; }
  const rows = JSON.parse(fs.readFileSync(shard, "utf8")).slice(0, 800);
  let hit = 0;
  const miss = [];
  for (const s of rows) {
    if (cv[skey(s.n, s.a)]) hit++;
    else if (miss.length < 3) miss.push(`${s.n}|${s.a}`);
  }
  console.log(`  曲库前 ${rows.length} 首命中 ${hit} 首（${(hit * 100 / rows.length).toFixed(1)}%）`);
  if (miss.length) console.log("  未命中示例:", miss.join("  ⌇  "));

  console.log("=== ④ 对照：用 URL 键能命中吗（说明旧索引为何端上用不上）===");
  const firstUrlKey = Object.keys(cv).find(k => k.startsWith("u:"));
  console.log(`  URL 键示例: ${firstUrlKey || "(无)"}  → 端上 skey 永远查不到这种键`);

  srv.close();
});
