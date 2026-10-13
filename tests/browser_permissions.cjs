const {chromium}=require(process.env.PLAYWRIGHT_MODULE || '../tooling/node_modules/playwright');
const fs=require('node:fs'); const path=require('node:path'); const assert=require('node:assert/strict');
(async()=>{
  const root=path.resolve(__dirname,'..'),host=process.env.TEST_HOST || 'http://127.0.0.1:8000';
  const admin=fs.readFileSync(path.join(root,'artifacts/admin-token.txt'),'utf8').replace(/^\uFEFF/,'').trim();
  async function adminPost(url,body) { const r=await fetch(host+url,{method:'POST',headers:{Authorization:'Bearer '+admin,'Content-Type':'application/json'},body:JSON.stringify(body)}); assert.equal(r.status,201); return r.json(); }
  const project=await adminPost('/api/admin/projects',{name:'Isolated browser permission self-test'});
  const reviewer=await adminPost('/api/admin/projects/'+project.id+'/keys',{role:'reviewer'});
  const reader=await adminPost('/api/admin/projects/'+project.id+'/keys',{role:'reader'});
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1200,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(host+'/'); await page.locator('.template').first().waitFor();
    async function connect(token) { await page.locator('#connect').click(); await page.locator('#project-key').fill(token); await page.locator('#project-form button[type=submit]').click(); }
    await connect('deliberately-invalid-self-test-key'); await page.locator('#notice').filter({hasText:'项目密钥无效'}).waitFor();
    await connect(reviewer.token); await page.waitForURL('**/#review'); await page.locator('#context-label').filter({hasText:'盲评专用'}).waitFor();
    assert.equal(await page.locator('nav a:visible').count(),1);
    const denied=await page.evaluate(async()=>{const r=await fetch('/api/public/experiments',{headers:{Authorization:'Bearer '+sessionStorage.getItem('sentinel-project-key')}});return r.status;});
    assert.equal(denied,403);
    await connect(reader.token); await page.goto(host+'/#setup'); await page.locator('.template').first().waitFor();
    await page.locator('[data-action=template][data-id=demo]').click(); await page.locator('main').filter({hasText:'36 道题'}).waitFor();
    assert.equal(await page.locator('[data-action=import]').isDisabled(),true);
    await connect(project.key.token); await page.goto(host+'/#setup'); await page.locator('.template').first().waitFor();
    assert.equal(await page.locator('#dataset-select').count(),0);
    await connect(''); await page.goto(host+'/#setup'); await page.locator('.template').first().waitFor();
    assert.equal(await page.locator('#dataset-select').count(),1);
    assert.deepEqual(errors,[]);console.log('PASS: invalid key recovery, reviewer-only navigation and API denial, reader disabled writes, project-scoped restored state; no human votes or secrets logged.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
